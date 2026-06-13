"""Assessment runner — the hybrid engine.

For a selected workload + pillars:
  1. Resolve the workload's scope (subscriptions / resource-group / resource ids) and the
     set of ARM resource types it actually contains.
  2. For each applicable check, run its deterministic Resource Graph (KQL) control scoped
     to the workload; flagged resources ⇒ fail, none ⇒ pass, no matching types ⇒ N/A.
  3. Compute 0-100 per-pillar scores (weighted by severity) and an overall score.
  4. AI layer (hybrid): one LLM call produces an executive summary + a short rationale
     per failed finding. Degrades gracefully when the LLM is unavailable.
  5. Persist an AssessmentRun (history) and diff against the previous run (drift).

Streams progress as events for the SSE endpoint.
"""
from __future__ import annotations

import json
import logging
import re
import time as _time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import desc, select

from app.assessments import catalog
from app.core.db import SessionLocal
from app.exec.command_runner import run_kql_capture
from app.models import AssessmentRun
from app.workloads import discovery
from app.workloads.registry import get_workload

logger = logging.getLogger("app.assessments.runner")

_SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_RANK_SEV = {v: k for k, v in _SEV_RANK.items()}
_FLAGGED_SAMPLE = 25  # max flagged resources stored per finding
_SCAN_SAMPLE = 1000  # max in-scope resources captured for the report's Resources tab


def _real_remediation(template: str, resource: dict[str, Any]) -> str:
    """Fill a remediation-command template's placeholders with a flagged resource's real
    values, so the operator gets a ready-to-run command instead of <name>/<rg> stubs."""
    if not template:
        return ""
    name = str(resource.get("name", "") or "")
    rg = str(resource.get("resource_group", "") or "")
    sub = str(resource.get("subscription_id", "") or "")
    repl = {
        "<name>": name,
        "<n>": name,
        "<rg>": rg,
        "<resource-group>": rg,
        "<resource_group>": rg,
        "<resourcegroup>": rg,
        "<subscription>": sub,
        "<subscription-id>": sub,
        "<subscription_id>": sub,
        "<sub>": sub,
    }
    out = template
    for ph, val in repl.items():
        if val:
            # Case-insensitive placeholder replacement.
            out = re.sub(re.escape(ph), val, out, flags=re.IGNORECASE)
    # Prepend a subscription scope so the command targets the right subscription even
    # when the operator's default differs, but only if the command doesn't already set it.
    if sub and out.startswith("az ") and "--subscription" not in out:
        out = f"{out} --subscription {sub}"
    return out


# Run ids for which a cancellation has been requested (cooperative cancellation: the
# runner checks this between checks and stops gracefully, marking the run 'cancelled').
_CANCEL_REQUESTS: set[str] = set()


def request_cancel(run_id: str) -> None:
    """Flag a queued/running assessment run for cooperative cancellation."""
    _CANCEL_REQUESTS.add(run_id)


def _is_cancelled(run_id: str | None) -> bool:
    return bool(run_id) and run_id in _CANCEL_REQUESTS


def _clear_cancel(run_id: str | None) -> None:
    if run_id:
        _CANCEL_REQUESTS.discard(run_id)



async def create_queued_run(
    *,
    workload_id: str,
    workload_name: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None,
    actor: str,
    trigger: str,
) -> str:
    """Insert a placeholder AssessmentRun (status='queued') and return its id.

    Used by background/batch enqueue so the run is immediately visible in history
    with a live status while the work happens asynchronously."""
    run = AssessmentRun(
        workload_id=workload_id,
        workload_name=workload_name,
        tenant_id=tenant_id,
        connection_id=connection_id or None,
        pillars=[p for p in pillars if p in catalog.PILLARS] or list(catalog.PILLARS),
        status="queued",
        triggered_by=actor,
        trigger=trigger,
    )
    async with SessionLocal() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def _set_run_status(run_id: str, status: str, *, error: str | None = None) -> None:
    """Update just the status (and optional error) of an existing run row."""
    async with SessionLocal() as db:
        run = await db.get(AssessmentRun, run_id)
        if run is None:
            return
        run.status = status
        if error is not None:
            run.error = error[:2000]
        if status in ("succeeded", "failed"):
            run.ended_at = _now()
        await db.commit()


async def run_assessment_to_completion(
    *,
    run_id: str,
    workload_id: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None = None,
    actor: str = "",
    trigger: str = "manual",
    use_ai: bool = True,
) -> None:
    """Drive an enqueued run to completion, updating its existing row's status.

    Drains the streaming runner; on an ``error`` event or unexpected exception the
    run row is marked ``failed`` so the failure is visible in history."""
    try:
        async for ev in run_assessment(
            existing_run_id=run_id,
            workload_id=workload_id,
            pillars=pillars,
            tenant_id=tenant_id,
            connection_id=connection_id,
            actor=actor,
            trigger=trigger,
            use_ai=use_ai,
        ):
            if ev.get("type") == "cancelled":
                return  # status already set to 'cancelled' by the runner
            if ev.get("type") == "error":
                await _set_run_status(run_id, "failed", error=str(ev.get("message", "Assessment failed.")))
                return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background assessment %s failed", run_id)
        await _set_run_status(run_id, "failed", error=str(exc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(type_: str, **data: Any) -> dict[str, Any]:
    return {"type": type_, **data}


def _sub_guid(value: str) -> str:
    """Extract the bare subscription GUID from an ARM id or pass through a GUID."""
    if not value:
        return ""
    m = re.search(r"/subscriptions/([0-9a-fA-F-]{36})", value)
    if m:
        return m.group(1)
    return value


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


async def _resolve_scope(workload: dict[str, Any], connection: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a workload's nodes into a KQL scope predicate.

    Returns {predicate, subscriptions, rg_pairs, resource_ids, error}. mg nodes are
    expanded to their subscriptions; subscription nodes scope the whole subscription;
    resource-group and resource nodes scope precisely."""
    subs: set[str] = set()
    rg_pairs: set[tuple[str, str]] = set()
    resource_ids: set[str] = set()

    for node in workload.get("nodes", []):
        kind = node.get("kind")
        if kind == "subscription":
            guid = _sub_guid(node.get("id", "")) or _sub_guid(node.get("subscription_id", ""))
            if guid:
                subs.add(guid)
        elif kind == "mg":
            mg_id = node.get("id", "")
            for s in await discovery.subscriptions_under_mg(connection, mg_id):
                subs.add(_sub_guid(s))
        elif kind == "resource_group":
            guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(node.get("id", ""))
            rg = node.get("resource_group") or node.get("name", "")
            if guid and rg:
                rg_pairs.add((guid, rg))
        elif kind == "resource":
            rid = node.get("id", "")
            if rid:
                resource_ids.add(rid)

    clauses: list[str] = []
    if subs:
        joined = ", ".join(f"'{_esc(s)}'" for s in sorted(subs))
        clauses.append(f"subscriptionId in~ ({joined})")
    for guid, rg in sorted(rg_pairs):
        clauses.append(f"(subscriptionId =~ '{_esc(guid)}' and resourceGroup =~ '{_esc(rg)}')")
    if resource_ids:
        joined = ", ".join(f"'{_esc(r)}'" for r in sorted(resource_ids))
        clauses.append(f"id in~ ({joined})")

    predicate = " or ".join(clauses) if clauses else ""
    return {
        "predicate": predicate,
        "subscriptions": sorted(subs),
        "rg_pairs": sorted(rg_pairs),
        "resource_ids": sorted(resource_ids),
        "error": "" if predicate else "Workload has no resolvable scope (empty membership).",
    }


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


async def _scan_scope(predicate: str, connection: dict[str, Any] | None) -> dict[str, Any]:
    """Inventory the workload scope in one pass: the ARM types present (drives per-check
    applicability), the exact total resource count, and a capped sample of resources
    (id/name/type/rg/subscription/location) for the report's Resources tab."""
    types: set[str] = set()
    total = 0
    by_type = await run_kql_capture(
        f"Resources | where {predicate} | summarize n=count() by type", connection, output="json"
    )
    if by_type.ok:
        for r in _parse_rows(by_type.stdout):
            t = str(r.get("type", "")).lower()
            if t:
                types.add(t)
            # ARG names count() as the alias we gave ("n"); fall back defensively.
            total += int(r.get("n", r.get("count_", r.get("count", 0))) or 0)

    resources: list[dict[str, Any]] = []
    proj = await run_kql_capture(
        f"Resources | where {predicate} "
        "| project name, type, location, id, resourceGroup, subscriptionId "
        f"| order by type asc, name asc | take {_SCAN_SAMPLE}",
        connection,
        output="json",
    )
    if proj.ok:
        resources = [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
                "location": r.get("location", ""),
            }
            for r in _parse_rows(proj.stdout)
        ]
    return {"types": types, "count": total, "resources": resources}


def _scored(checks: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute 0-100 per-pillar scores + totals from finished findings."""
    from app.core.app_settings import assessment_weights

    weights = assessment_weights()
    by_pillar: dict[str, dict[str, Any]] = {}
    by_severity: dict[str, int] = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    passed = failed = na = waived = 0

    for f in findings:
        pillar = f["pillar"]
        p = by_pillar.setdefault(
            pillar, {"weight_total": 0, "weight_passed": 0, "passed": 0, "failed": 0, "na": 0, "waived": 0, "errored": 0}
        )
        status = f["status"]
        if status == "not_applicable":
            p["na"] += 1
            na += 1
            continue
        if status == "waived":
            # Suppressed by a risk acceptance — excluded from scoring entirely.
            p["waived"] += 1
            waived += 1
            continue
        if status == "error":
            p["errored"] += 1
            continue
        # Score weight is derived from severity at run time so admin-tuned weights apply.
        w = weights.get(f.get("severity", "warning"), f.get("weight", 3))
        p["weight_total"] += w
        if status == "pass":
            p["weight_passed"] += w
            p["passed"] += 1
            passed += 1
        elif status == "fail":
            p["failed"] += 1
            failed += 1
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    scores: dict[str, Any] = {}
    pillar_score_values: list[int] = []
    for pillar, p in by_pillar.items():
        wt = p["weight_total"]
        score = round(100 * p["weight_passed"] / wt) if wt > 0 else None
        if score is not None:
            pillar_score_values.append(score)
        scores[pillar] = {
            "score": score,
            "passed": p["passed"],
            "failed": p["failed"],
            "na": p["na"],
            "waived": p["waived"],
            "errored": p["errored"],
            "total": p["passed"] + p["failed"],
        }
    overall = round(sum(pillar_score_values) / len(pillar_score_values)) if pillar_score_values else None

    # Worst failing severity for the run-level badge.
    worst = "info"
    for sev in ("critical", "error", "warning"):
        if by_severity.get(sev, 0) > 0:
            worst = sev
            break

    return {
        "overall_score": overall,
        "scores": scores,
        "totals": {"passed": passed, "failed": failed, "na": na, "waived": waived, "by_severity": by_severity},
        "severity": worst,
    }


async def _ai_enrich(workload_name: str, findings: list[dict[str, Any]], scores: dict[str, Any]) -> tuple[str, bool]:
    """One LLM call: executive summary + per-failed-finding rationale (hybrid layer).

    Mutates ``findings`` in place to add ``ai_rationale`` to failed checks. Returns
    (summary_markdown, used_ai)."""
    failed = [f for f in findings if f["status"] == "fail"]
    if not failed:
        # Nothing failed — produce a short deterministic summary, skip the LLM.
        return ("No failed controls. All applicable checks passed for this workload.", False)

    from app.agent.factory import build_provider_for

    lines = []
    for f in failed:
        lines.append(
            f"- [{f['check_id']}] ({f['pillar']}/{f['severity']}) {f['title']} — "
            f"{f['flagged_count']} flagged resource(s)"
        )
    score_line = ", ".join(
        f"{p}: {v.get('score')}" for p, v in (scores or {}).items()
    )
    sys = (
        "You are an Azure Well-Architected reviewer. You are given the FAILED controls from a "
        "workload assessment (already evaluated deterministically). Write a concise executive "
        "summary and a one-sentence business-impact rationale for each failed control. Be "
        "specific and prioritize by severity. Reply with ONLY JSON (no code fence): "
        '{"summary": "<2-4 sentence markdown exec summary>", '
        '"rationales": {"<check_id>": "<one sentence impact/why it matters>"}}'
    )
    user = (
        f"Workload: {workload_name}\nPillar scores (0-100): {score_line}\n\n"
        f"Failed controls:\n" + "\n".join(lines)
    )

    provider = build_provider_for(None, None)
    parts: list[str] = []
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}], None
        ):
            if ev.type == "token":
                parts.append(ev.text)
    except Exception as exc:  # noqa: BLE001 - AI is best-effort
        logger.warning("Assessment AI enrichment failed: %s", exc)
        return ("", False)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    text = "".join(parts).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return (text[:1200], bool(text))
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (text[:1200], bool(text))
    rationales = data.get("rationales") or {}
    if isinstance(rationales, dict):
        for f in failed:
            r = rationales.get(f["check_id"])
            if isinstance(r, str) and r.strip():
                f["ai_rationale"] = r.strip()
    return (str(data.get("summary", "")).strip(), True)


async def _active_waivers(workload_id: str, tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    """Active (non-revoked, non-expired) waivers for a workload, keyed by check id."""
    from app.models import AssessmentWaiver

    now = _now()
    out: dict[str, list[dict[str, Any]]] = {}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AssessmentWaiver).where(
                    AssessmentWaiver.tenant_id == tenant_id,
                    AssessmentWaiver.workload_id == workload_id,
                    AssessmentWaiver.status == "active",
                )
            )
        ).scalars().all()
    for w in rows:
        if w.expires_at is not None:
            exp = w.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue  # expired
        out.setdefault(w.check_id, []).append(
            {"resource_id": w.resource_id, "justification": w.justification, "approver": w.approver, "id": w.id}
        )
    return out


def _apply_waivers(finding: dict[str, Any], waivers: list[dict[str, Any]]) -> None:
    """Mutate a FAILED finding per its waivers: whole-check waiver ⇒ status 'waived';
    per-resource waivers remove those resources (and pass if all are cleared)."""
    if not waivers or finding["status"] != "fail":
        return
    whole = [w for w in waivers if not w.get("resource_id")]
    if whole:
        finding["status"] = "waived"
        finding["waiver"] = {"justification": whole[0]["justification"], "approver": whole[0]["approver"]}
        return
    waived_ids = {w["resource_id"].lower() for w in waivers if w.get("resource_id")}
    if waived_ids:
        kept = [r for r in finding["flagged_resources"] if r.get("id", "").lower() not in waived_ids]
        removed = len(finding["flagged_resources"]) - len(kept)
        finding["flagged_resources"] = kept
        finding["flagged_count"] = max(0, finding["flagged_count"] - removed)
        if finding["flagged_count"] == 0:
            finding["status"] = "waived"
            finding["waiver"] = {"justification": "All flagged resources waived", "approver": ""}


async def _previous_run(tenant_id: str, workload_id: str, pillars: list[str]) -> AssessmentRun | None:
    async with SessionLocal() as db:
        # Prefer an admin-pinned baseline run; else the most recent succeeded run.
        baseline = (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.is_baseline.is_(True),
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if baseline is not None:
            return baseline
        return (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()


def _compute_diff(prev: AssessmentRun | None, findings: list[dict[str, Any]], scores: dict[str, Any]) -> dict[str, Any] | None:
    if prev is None:
        return None
    prev_by_id = {f.get("check_id"): f for f in (prev.findings_json or [])}
    new_failures: list[dict[str, Any]] = []
    resolved: list[str] = []
    new_criticals = 0
    for f in findings:
        cid = f["check_id"]
        before = (prev_by_id.get(cid) or {}).get("status")
        if f["status"] == "fail" and before in ("pass", "waived", None):
            new_failures.append({"title": f["title"], "severity": f["severity"]})
            if f["severity"] == "critical":
                new_criticals += 1
        if f["status"] in ("pass", "waived") and before == "fail":
            resolved.append(f["title"])
    score_delta: dict[str, Any] = {}
    prev_scores = (prev.scores_json or {})
    for pillar, v in (scores or {}).items():
        before = (prev_scores.get(pillar) or {}).get("score")
        score_delta[pillar] = {"before": before, "after": v.get("score")}
    return {
        "baseline_run_id": prev.id,
        "baseline_is_pinned": bool(getattr(prev, "is_baseline", False)),
        "new_failures": new_failures,
        "new_criticals": new_criticals,
        "resolved": resolved,
        "score_delta": score_delta,
    }


async def run_assessment(
    *,
    workload_id: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None = None,
    actor: str = "",
    trigger: str = "manual",
    use_ai: bool = True,
    existing_run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run an assessment end-to-end, yielding progress events and a final ``done`` event
    carrying the persisted run id.

    When ``existing_run_id`` is given, that pre-created (queued) run row is updated in
    place instead of inserting a new one — enabling background/batch execution with a
    live status visible in history."""
    started = _time.perf_counter()
    if existing_run_id:
        await _set_run_status(existing_run_id, "running")
    if _is_cancelled(existing_run_id):
        _clear_cancel(existing_run_id)
        await _set_run_status(existing_run_id, "cancelled")
        yield _event("cancelled", run_id=existing_run_id or "")
        return
    workload = get_workload(workload_id)
    if workload is None:
        yield _event("error", message="Workload not found.")
        return

    pillars = [p for p in pillars if p in catalog.PILLARS] or list(catalog.PILLARS)
    from app.core.azure_connections import resolve_connection

    conn_id = connection_id or workload.get("connection_id") or ""
    connection = resolve_connection(conn_id or None)
    wl_name = workload.get("name", "workload")

    yield _event("status", phase="scope", message=f"Resolving scope for '{wl_name}'…")
    scope = await _resolve_scope(workload, connection)
    if scope["error"]:
        yield _event("error", message=scope["error"])
        return
    predicate = scope["predicate"]

    yield _event("status", phase="inventory", message="Enumerating resources in scope…")
    scan = await _scan_scope(predicate, connection)
    present = scan["types"]
    resource_count = scan["count"]
    scanned_resources = scan["resources"]
    yield _event(
        "status",
        phase="inventory",
        message=f"{resource_count} resource(s) across {len(present)} type(s) in scope.",
        subscriptions=len(scope["subscriptions"]),
        resources=resource_count,
    )

    checks = catalog.checks_for(pillars)
    findings: list[dict[str, Any]] = []
    total = len(checks)
    waivers = await _active_waivers(workload_id, tenant_id)

    for i, check in enumerate(checks, start=1):
        if _is_cancelled(existing_run_id):
            _clear_cancel(existing_run_id)
            await _set_run_status(existing_run_id, "cancelled")
            yield _event("cancelled", run_id=existing_run_id or "")
            return
        applicable = any(t in present for t in check["resource_types"])
        base = {
            "check_id": check["id"],
            "pillar": check["pillar"],
            "title": check["title"],
            "description": check["description"],
            "severity": check["severity"],
            "weight": check["weight"],
            "frameworks": check["frameworks"],
            "remediation": check["remediation"],
            "remediation_command": check["remediation_command"],
            "resource_types": check["resource_types"],
            "flagged_count": 0,
            "flagged_resources": [],
            "ai_rationale": "",
        }
        yield _event(
            "check_start",
            index=i,
            total=total,
            check_id=check["id"],
            title=check["title"],
            pillar=check["pillar"],
        )

        if not applicable:
            base["status"] = "not_applicable"
            findings.append(base)
            yield _event("check_result", **{k: base[k] for k in ("check_id", "title", "pillar", "severity")}, status="not_applicable")
            continue

        kql = f"Resources | where {predicate}\n{check['kql']}"
        cap = await run_kql_capture(kql, connection, output="json")
        if not cap.ok:
            base["status"] = "error"
            base["error"] = (cap.error or "Query failed.")[:300]
            findings.append(base)
            yield _event("check_result", check_id=check["id"], title=check["title"], pillar=check["pillar"], severity=check["severity"], status="error")
            continue

        rows = _parse_rows(cap.stdout)
        if rows:
            base["status"] = "fail"
            base["flagged_count"] = len(rows)
            template = check.get("remediation_command", "")
            base["flagged_resources"] = [
                {
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "type": r.get("type", ""),
                    "resource_group": r.get("resourceGroup", ""),
                    "subscription_id": r.get("subscriptionId", ""),
                    "remediation_command": _real_remediation(
                        template,
                        {
                            "name": r.get("name", ""),
                            "resource_group": r.get("resourceGroup", ""),
                            "subscription_id": r.get("subscriptionId", ""),
                        },
                    ),
                }
                for r in rows[:_FLAGGED_SAMPLE]
            ]
        else:
            base["status"] = "pass"
        # Apply any active waivers (whole-check or per-resource risk acceptances).
        _apply_waivers(base, waivers.get(check["id"], []))
        findings.append(base)
        yield _event(
            "check_result",
            check_id=check["id"],
            title=check["title"],
            pillar=check["pillar"],
            severity=check["severity"],
            status=base["status"],
            flagged_count=base["flagged_count"],
        )

    # --- Score ---------------------------------------------------------------
    yield _event("status", phase="scoring", message="Scoring results…")
    sc = _scored(checks, findings)

    # --- AI hybrid layer -----------------------------------------------------
    summary = ""
    used_ai = False
    if use_ai:
        yield _event("status", phase="ai", message="Generating executive summary…")
        try:
            summary, used_ai = await _ai_enrich(wl_name, findings, sc["scores"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI enrich error: %s", exc)
    if not summary:
        t = sc["totals"]
        summary = (
            f"Assessed {t['passed'] + t['failed']} applicable controls: "
            f"{t['passed']} passed, {t['failed']} failed. Overall score {sc['overall_score']}/100."
        )

    # --- Diff vs previous ----------------------------------------------------
    prev = await _previous_run(tenant_id, workload_id, pillars)
    diff = _compute_diff(prev, findings, sc["scores"])

    # --- Persist -------------------------------------------------------------
    duration_ms = int((_time.perf_counter() - started) * 1000)
    fields = dict(
        workload_id=workload_id,
        workload_name=wl_name,
        tenant_id=tenant_id,
        connection_id=conn_id or None,
        pillars=pillars,
        status="succeeded",
        overall_score=sc["overall_score"],
        scores_json=sc["scores"],
        totals_json=sc["totals"],
        severity=sc["severity"],
        findings_json=findings,
        resource_count=resource_count,
        resources_json=scanned_resources,
        summary=summary,
        used_ai=used_ai,
        baseline_run_id=(prev.id if prev else None),
        diff_json=diff,
        triggered_by=actor,
        trigger=trigger,
        ended_at=_now(),
        duration_ms=duration_ms,
    )
    async with SessionLocal() as db:
        run = None
        if existing_run_id:
            run = await db.get(AssessmentRun, existing_run_id)
        if run is not None:
            for k, v in fields.items():
                setattr(run, k, v)
        else:
            run = AssessmentRun(started_at=_now(), **fields)
            db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    # Publish a notification event (best-effort) for routing / alerts.
    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=tenant_id,
            type="assessment.completed",
            source="assessment",
            severity=sc["severity"] if sc["totals"]["failed"] else "info",
            title=f"Assessment: {wl_name} scored {sc['overall_score']}/100",
            body=summary[:1000],
            facts={
                "workload_id": workload_id,
                "overall_score": sc["overall_score"],
                "failed": sc["totals"]["failed"],
            },
            links={"run_id": run_id},
            fingerprint=f"assessment:{workload_id}",
        )
    except Exception:  # noqa: BLE001 - notifications optional
        pass

    yield _event(
        "done",
        run_id=run_id,
        overall_score=sc["overall_score"],
        scores=sc["scores"],
        totals=sc["totals"],
        severity=sc["severity"],
        used_ai=used_ai,
        summary=summary,
        diff=diff,
        duration_ms=duration_ms,
    )
