"""The analysis orchestrator: collect, join, derive, target, roll up.

The only place that reads Azure. Everything downstream of it is pure, which is why demo mode
can run the identical path with synthetic inputs.

Each source degrades independently and says so in ``provenance``. A missing input must never
fail the whole analysis — this module is a composer, and a composer that fails whole when one
of five inputs is unreadable is useless on a real tenant.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from app.backup_manager import service
from app.resiliency import collect, join, model, reference, rollup, rto, snapshot, targets

log = logging.getLogger("app.resiliency.analyze")

Progress = Callable[[str, str], Awaitable[None]] | None


async def _say(progress: Progress, level: str, message: str) -> None:
    if progress is not None:
        try:
            await progress(level, message)
        except Exception:  # noqa: BLE001 - progress must never break the analysis
            pass


def _provenance(source: str, *, collected_at: str = "", unreadable: bool = False,
                reason: str = "", truncated: bool = False) -> dict[str, Any]:
    return {"source": source, "collected_at": collected_at, "unreadable": unreadable,
            "reason": reason, "truncated": truncated}


async def _backup_facts(
    connection: dict[str, Any], tenant_id: str, scope_kind: str, scope_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, str]:
    """Protection + replication from Backup Manager's snapshot, if one exists.

    Returns ``(instances, replication, known, reason)``. ``known=False`` means the estate
    was never read for this scope — every protection fact then reports `unknown`, and NOT
    `unprotected`. Conflating those produces a full-estate false alarm."""
    from app.backup_manager import snapshot as bm_snapshot

    try:
        snap = bm_snapshot.read_snapshot(
            tenant_id, str(connection.get("id") or ""), scope_kind, scope_id)
    except Exception as exc:  # noqa: BLE001
        return [], [], False, f"The backup estate could not be read: {exc}"
    if not snap or not snap.get("report_exists"):
        return [], [], False, (
            "Backup Manager has not analyzed this scope, so protection could not be "
            "determined. This is not a statement that these resources are unprotected.")
    instances = (snap.get("inventory") or {}).get("rows") or []
    replication = (snap.get("dr") or {}).get("items") or []
    return instances, replication, True, ""


async def _advisor(
    connection: dict[str, Any], subscriptions: list[str],
) -> tuple[list[dict[str, Any]], str]:
    """Azure Advisor reliability recommendations — the same feed the portal surfaces."""
    query = (
        "advisorresources\n"
        "| where type =~ 'microsoft.advisor/recommendations'\n"
        "| extend p = parse_json(tostring(properties))\n"
        "| where tolower(tostring(p.category)) == 'highavailability'\n"
        "| project resource_id = tolower(tostring(p.resourceMetadata.resourceId)),\n"
        "          category = tostring(p.category),\n"
        "          impact = tostring(p.impact),\n"
        "          problem = tostring(p.shortDescription.problem),\n"
        "          solution = tostring(p.shortDescription.solution)\n"
    )
    rows, error = await service.arg_safe(connection, query, subscriptions, max_rows=2000)
    return rows, error


async def analyze(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    subscriptions: list[str] | None = None,
    workload_id: str = "",
    progress: Progress = None,
) -> dict[str, Any]:
    """One full sweep for one scope."""
    from app.demo_catalog import is_demo_workload

    generated_at = service.now_iso()
    demo = bool(workload_id) and is_demo_workload(workload_id)
    provenance: dict[str, Any] = {}

    # Resource Graph filters by SUBSCRIPTION only. On a workload scope that returns every
    # resource in the workload's subscriptions, and the loop below then stamps
    # `workload_id` on all of them — the whole subscription, labeled as the workload.
    members: set[str] | None = None
    member_error = ""
    if not demo and scope_kind == "workload":
        workload, members, _subs = service.workload_context(workload_id or scope_id)
        if workload is None:
            members, member_error = set(), (
                "The selected workload could not be read, so its members are unknown. "
                "Nothing here is a statement about the estate.")

    # --- configuration -----------------------------------------------------------
    await _say(progress, "info", "Reading redundancy and backup configuration…")
    if demo:
        config, meta = collect.collect_demo(workload_id)
        provenance["configuration"] = _provenance("Demo estate", collected_at=generated_at)
    else:
        config, meta = await collect.collect(
            connection or {}, subscriptions or [], member_ids=members)
        provenance["configuration"] = _provenance(
            "Resource Graph", collected_at=generated_at,
            unreadable=bool(member_error) or (bool(meta.get("error")) and not config),
            reason=member_error or str(meta.get("error") or ""),
            truncated=bool(meta.get("partial")))
    await _say(progress, "ok", f"{len(config):,} resource(s) in scope")

    # --- protection ----------------------------------------------------------------
    await _say(progress, "info", "Joining the backup estate…")
    if demo:
        from app import demo_catalog

        instances = demo_catalog.resiliency_backup_for(workload_id)
        replication = demo_catalog.resiliency_asr_for(workload_id)
        backup_known, backup_reason = True, ""
    else:
        instances, replication, backup_known, backup_reason = await _backup_facts(
            connection or {}, tenant_id, scope_kind, scope_id)
    provenance["protection"] = _provenance(
        "Backup Manager", collected_at=generated_at,
        unreadable=not backup_known, reason=backup_reason)

    # --- advisor -------------------------------------------------------------------
    advisor_rows: list[dict[str, Any]] = []
    advisor_error = ""
    if not demo and connection:
        await _say(progress, "info", "Reading Azure Advisor reliability recommendations…")
        advisor_rows, advisor_error = await _advisor(connection, subscriptions or [])
    provenance["advisor"] = _provenance(
        "Azure Advisor", collected_at=generated_at,
        unreadable=bool(advisor_error), reason=advisor_error)

    # --- management locks ----------------------------------------------------------
    # ARM, not Resource Graph: locks are absent from both `Resources` and
    # `authorizationresources`, verified against a live tenant. One call per subscription.
    locks: list[dict[str, Any]] = []
    lock_error = ""
    if demo:
        from app import demo_catalog

        locks = getattr(demo_catalog, "resiliency_locks_for", lambda _w: [])(workload_id)
    elif connection:
        await _say(progress, "info", "Reading management locks…")
        locks, lock_error = await collect.collect_locks(connection, subscriptions or [])
    provenance["locks"] = _provenance(
        "Azure Resource Manager", collected_at=generated_at,
        unreadable=bool(lock_error), reason=lock_error)

    # --- derive --------------------------------------------------------------------
    await _say(progress, "info", "Deriving recovery verdicts…")
    rows = join.build_rows(
        config, backup=instances, asr=replication, advisor=advisor_rows, locks=locks,
        backup_known=backup_known, backup_reason=backup_reason,
    )

    doc = reference.load()
    for row in rows:
        row["workload_id"] = workload_id
        verdict_objects = {
            scenario: model.Verdict(
                scenario=scenario,
                rpo_minutes=v["rpo_minutes"], rpo_state=v["rpo_state"],
                rto_class=v["rto_class"],
                basis=tuple(model.Evidence(**e) for e in v.get("basis", [])),
                confidence=v["confidence"], applicable=v["applicable"],
                # Rebuilt explicitly: this round-trip through dicts drops anything not named
                # here, and a silently discarded caveat is indistinguishable from no caveat.
                caveats=tuple(model.Caveat(**c) for c in v.get("caveats", [])),
            )
            for scenario, v in row["verdicts"].items()
        }
        banded = rto.apply_bands(
            verdict_objects, resource_type=row["type"], size_gb=row.get("size_gb"), doc=doc)
        row["verdicts"] = {s: v.as_dict() for s, v in banded.items()}

    # --- targets -------------------------------------------------------------------
    tier_id = _tier_for_scope(demo, workload_id)
    for row in rows:
        row["tier"] = tier_id
        row["tier_source"] = _tier_source(demo, workload_id)
    targets.apply_targets(rows, doc=doc)

    # --- roll up -------------------------------------------------------------------
    names = {workload_id: _workload_name(demo, workload_id, scope_id)}
    workloads = rollup.group_by_workload(rows, names=names, tiers={workload_id: tier_id})

    snap = {
        "schema_version": snapshot.SCHEMA_VERSION,
        "report_exists": True,
        "generated_at": generated_at,
        "demo": demo,
        "reason": "",
        "scope": {"scope_kind": scope_kind, "scope_id": scope_id,
                  # Reports lead with this. Without it they print the raw id at 30px.
                  "scope_name": _workload_name(demo, workload_id, scope_id),
                  "subscriptions": subscriptions or []},
        "summary": join.summarize(rows),
        "resources": rows,
        "breaches": targets.breaches(rows),
        "breach_summary": targets.summarize_breaches(rows),
        "workloads": workloads,
        "provenance": provenance,
        "targets_acknowledged": bool(doc.get("targets_acknowledged")),
        "truncation": {},
    }
    await _say(progress, "ok", f"Analysis complete — {len(rows):,} resource(s)")
    return snapshot.bound(snap)


def _tier_for_scope(demo: bool, workload_id: str) -> str:
    if demo and workload_id:
        from app import demo_catalog

        return demo_catalog.criticality_for(workload_id)
    if workload_id:
        try:
            from app.workloads.registry import get_workload

            workload = get_workload(workload_id) or {}
            return reference.tier_for_criticality(workload.get("criticality", ""))
        except Exception:  # noqa: BLE001
            pass
    return reference.DEFAULT_TIER


def _tier_source(demo: bool, workload_id: str) -> str:
    if demo:
        return "demo workload criticality"
    return "workload criticality" if workload_id else "default tier"


def _workload_name(demo: bool, workload_id: str, scope_id: str) -> str:
    if demo and workload_id:
        from app import demo_catalog

        return demo_catalog.name_for(workload_id)
    if workload_id:
        try:
            from app.workloads.registry import get_workload

            return (get_workload(workload_id) or {}).get("name", workload_id)
        except Exception:  # noqa: BLE001
            return workload_id
    return scope_id or "Scope"


__all__ = ["analyze"]
