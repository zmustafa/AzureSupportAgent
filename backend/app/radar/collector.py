"""Radar collection + aggregation.

Pulls retirement/breaking-change signals from two Azure Resource Graph tables —
``servicehealthresources`` (planned-maintenance / health-advisory / retirement events) and
``advisorresources`` (Advisor "Service Upgrade and Retirement" recommendations, which carry
resource-level impact) — plus the Azure OpenAI/Foundry deployment inventory for the model
lane. Everything runs on the ungated, read-only KQL path (``run_kql_capture``).

``merge_events`` and ``compute_radar`` are pure functions over already-fetched rows, so
they're unit-testable and power the demo seed. ``collect_radar`` resolves the scope and
gathers the rows from Azure."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from app.radar.builtin_seed import (
    BREAKING_CHANGE,
    RETIREMENT,
    classify_text,
    model_lifecycle_index,
)
from app.radar.reference import load_reference

log = logging.getLogger("app.radar.collector")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    from app.exec.command_runner import parse_kql_rows
    return parse_kql_rows(stdout)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    # Tolerate full ISO timestamps and bare dates.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + (6 if "%H" in fmt else 0)].split(".")[0], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def days_until(target: Any, *, today: date | None = None) -> int | None:
    """Whole days from today (UTC) to the target date. Negative when past."""
    d = _parse_date(target)
    if d is None:
        return None
    base = today or datetime.now(timezone.utc).date()
    return (d - base).days


def severity_for_days(days: int | None) -> str:
    """Countdown color band: red <30d, amber <90d, grey otherwise (incl. unknown/past)."""
    if days is None:
        return "grey"
    if days < 30:
        return "red"
    if days < 90:
        return "amber"
    return "grey"


def _synth_tracking_id(*parts: str) -> str:
    raw = "|".join(p for p in parts if p)
    return "radar-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]  # noqa: S324 - non-crypto id


# --------------------------------------------------------------------- owner mapping
def _owner_from_tags(tags: dict[str, Any] | None) -> str:
    # Delegates to the canonical ownership helper so the tag-owner heuristic lives in ONE
    # place (app.ownership.resolve) and stays consistent across Radar, Inventory, etc.
    from app.ownership.resolve import owner_from_tags

    return owner_from_tags(tags)


def _workload_index() -> dict[str, dict[str, str]]:
    """Lower-cased ARM id → {workload_id, workload_name, owner} from the workload registry."""
    out: dict[str, dict[str, str]] = {}
    try:
        from app.workloads.registry import list_workloads
    except Exception:  # noqa: BLE001
        return out
    for wl in list_workloads():
        owner = ""
        tags = wl.get("tags")
        if isinstance(tags, dict):
            owner = _owner_from_tags(tags)
        for node in wl.get("nodes", []) or []:
            rid = str(node.get("id", "")).lower()
            if node.get("kind") == "resource" and rid:
                out[rid] = {
                    "workload_id": wl.get("id", ""),
                    "workload_name": wl.get("name", ""),
                    "owner": owner,
                }
    return out


def resolve_owners(
    impacted: list[dict[str, Any]],
    wl_index: dict[str, dict[str, str]],
    *,
    tenant_id: str = "",
    own_ctx: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Annotate each impacted resource with workload + owner, flagging unowned ones.

    When ``tenant_id`` is provided, the canonical ownership engine
    (``app.ownership.resolve``) is consulted FIRST so an explicit assignment made in the
    ``/ownership`` UI (direct / inherited-from-RG-or-sub / workload) is reflected here. It
    falls back to the legacy tag→workload-tag heuristic when no explicit owner exists (and
    that legacy path is the only one used when ``tenant_id`` is empty, preserving the pure
    behavior the unit tests rely on)."""
    ctx = own_ctx
    if tenant_id and ctx is None:
        try:
            from app.ownership import resolve as own_resolve

            ctx = own_resolve.build_context(tenant_id)
        except Exception:  # noqa: BLE001
            ctx = None
    out: list[dict[str, Any]] = []
    for r in impacted:
        rid = str(r.get("id", "")).lower()
        wl = wl_index.get(rid, {})
        owner = ""
        owner_source = ""
        if tenant_id and ctx is not None:
            from app.ownership import resolve as own_resolve

            res = own_resolve.resolve_owner(
                tenant_id, "resource", r.get("id", ""), tags=r.get("tags"), ctx=ctx
            )
            if not res["unowned"] and res["owners"]:
                primary = next((o for o in res["owners"] if o["primary"]), res["owners"][0])
                owner = primary.get("display_name") or primary.get("email") or ""
                owner_source = res["source"]
        if not owner:
            owner = _owner_from_tags(r.get("tags")) or wl.get("owner", "")
            owner_source = "tag" if _owner_from_tags(r.get("tags")) else ("workload" if wl.get("owner") else "")
        out.append(
            {
                "id": r.get("id", ""),
                "name": r.get("name", "") or (r.get("id", "").rsplit("/", 1)[-1] if r.get("id") else ""),
                "type": r.get("type", ""),
                "resource_group": r.get("resourceGroup", "") or r.get("resource_group", ""),
                "region": r.get("location", "") or r.get("region", ""),
                "subscription_id": r.get("subscriptionId", "") or r.get("subscription_id", ""),
                "workload_id": wl.get("workload_id", ""),
                "workload_name": wl.get("workload_name", ""),
                "owner": owner,
                "owner_source": owner_source,
                "unowned": not owner,
            }
        )
    return out


# --------------------------------------------------------------------- classification
def classify_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge a source rule match into a raw event, choosing the most specific signal for
    change_type / recommended replacement / migration link / planned date."""
    text = " ".join(
        str(raw.get(k, "")) for k in ("service", "feature", "title", "summary", "impacted_field")
    )
    rule = classify_text(text)
    change_type = raw.get("change_type") or rule.get("change_type") or RETIREMENT
    if change_type not in (RETIREMENT, BREAKING_CHANGE):
        change_type = RETIREMENT
    return {
        "change_type": change_type,
        "service": raw.get("service") or rule.get("service") or "",
        "recommended_replacement": raw.get("recommended_replacement") or rule.get("replacement") or "",
        "migration_url": raw.get("migration_url") or rule.get("migration_url") or "",
        "planned_date": raw.get("retirement_date") or rule.get("planned_date") or "",
        "rule_id": rule.get("id", ""),
    }


def merge_events(
    raw_events: list[dict[str, Any]],
    *,
    wl_index: dict[str, dict[str, str]] | None = None,
    today: date | None = None,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Dedupe raw events by tracking ID, classify, compute days-until, resolve owners, and
    roll up a dominant owner / unowned flag. ``raw_events`` come from any source
    (service_health / advisor / azure_updates / aoai); same trackingId rows are merged and
    their impacted-resource lists unioned. ``tenant_id`` (when set) lets ``resolve_owners``
    consult explicit ownership assignments."""
    wl_index = wl_index or {}
    own_ctx = None
    if tenant_id:
        try:
            from app.ownership import resolve as own_resolve

            own_ctx = own_resolve.build_context(tenant_id)
        except Exception:  # noqa: BLE001
            own_ctx = None
    by_tid: dict[str, dict[str, Any]] = {}

    for ev in raw_events:
        tid = str(ev.get("tracking_id") or "").strip()
        if not tid:
            tid = _synth_tracking_id(ev.get("service", ""), ev.get("title", ""), ev.get("retirement_date", ""))
        cls = classify_event(ev)
        impacted = ev.get("impacted_resources") or []

        existing = by_tid.get(tid)
        if existing is None:
            existing = {
                "id": tid,
                "tracking_id": tid,
                "sources": [],
                "title": ev.get("title", "") or cls["service"] or "Azure lifecycle event",
                "summary": ev.get("summary", ""),
                "service": cls["service"],
                "feature": ev.get("feature", ""),
                "change_type": cls["change_type"],
                "retirement_date": cls["planned_date"],
                "recommended_replacement": cls["recommended_replacement"],
                "migration_url": cls["migration_url"],
                "rule_id": cls["rule_id"],
                "_impacted": {},
            }
            by_tid[tid] = existing
        src = ev.get("source", "")
        if src and src not in existing["sources"]:
            existing["sources"].append(src)
        # Prefer a concrete planned date / replacement / link when a later source has one.
        if not existing["retirement_date"] and cls["planned_date"]:
            existing["retirement_date"] = cls["planned_date"]
        if not existing["recommended_replacement"] and cls["recommended_replacement"]:
            existing["recommended_replacement"] = cls["recommended_replacement"]
        if not existing["migration_url"] and cls["migration_url"]:
            existing["migration_url"] = cls["migration_url"]
        if not existing["summary"] and ev.get("summary"):
            existing["summary"] = ev["summary"]
        for r in impacted:
            rid = str(r.get("id", "")).lower()
            if rid:
                existing["_impacted"][rid] = r

    out: list[dict[str, Any]] = []
    for ev in by_tid.values():
        impacted = resolve_owners(list(ev.pop("_impacted").values()), wl_index, tenant_id=tenant_id, own_ctx=own_ctx)
        owners = [r["owner"] for r in impacted if r["owner"]]
        dominant = max(set(owners), key=owners.count) if owners else ""
        unowned = any(r["unowned"] for r in impacted) or (not impacted)
        d = days_until(ev["retirement_date"], today=today)
        ev.update(
            {
                "impacted_resources": impacted,
                "impacted_count": len(impacted),
                "owner": dominant,
                "unowned": unowned,
                "days_until": d,
                "severity": severity_for_days(d),
            }
        )
        out.append(ev)

    out.sort(key=lambda e: (e["days_until"] is None, e["days_until"] if e["days_until"] is not None else 1 << 30))
    return out


# --------------------------------------------------------------------- model lane
def build_model_items(
    deployments: list[dict[str, Any]], *, today: date | None = None
) -> list[dict[str, Any]]:
    """Match live AOAI/Foundry deployments to the lifecycle table → per-deployment
    countdown. Deployments with no lifecycle match are surfaced as 'unknown' (no date)."""
    idx = model_lifecycle_index()
    out: list[dict[str, Any]] = []
    for dep in deployments:
        model = str(dep.get("model", "")).lower()
        version = str(dep.get("model_version", "") or dep.get("version", "")).lower()
        life = idx.get((model, version)) or idx.get((model, ""))
        # Fall back to the latest entry for the model family if version doesn't match.
        if life is None:
            fam = [e for e in idx.values() if e["model"].lower() == model]
            life = sorted(fam, key=lambda e: e.get("retirement_date", ""))[0] if fam else None
        retire = life.get("retirement_date", "") if life else ""
        d = days_until(retire, today=today)
        out.append(
            {
                "id": dep.get("id", "") or _synth_tracking_id("aoai", dep.get("account", ""), dep.get("deployment", "")),
                "account": dep.get("account", ""),
                "deployment": dep.get("deployment", ""),
                "model": dep.get("model", ""),
                "model_version": dep.get("model_version", "") or dep.get("version", ""),
                "region": dep.get("region", ""),
                "resource_group": dep.get("resource_group", ""),
                "subscription_id": dep.get("subscription_id", ""),
                "stage": life.get("stage", "unknown") if life else "unknown",
                "ga_date": life.get("ga_date", "") if life else "",
                "deprecation_date": life.get("deprecation_date", "") if life else "",
                "retirement_date": retire,
                "replacement": life.get("replacement", "") if life else "",
                "days_until": d,
                "severity": severity_for_days(d),
                "matched": life is not None,
            }
        )
    out.sort(key=lambda m: (m["days_until"] is None, m["days_until"] if m["days_until"] is not None else 1 << 30))
    return out


# --------------------------------------------------------------------- compute
def compute_radar(
    events: list[dict[str, Any]],
    model_items: list[dict[str, Any]],
    *,
    rail_limit: int = 6,
) -> dict[str, Any]:
    """Pure: assemble the snapshot (rail + counts) from already-merged events + model lane."""
    rail = [
        {
            "id": e["id"],
            "title": e["title"] or e["service"],
            "service": e["service"],
            "change_type": e["change_type"],
            "days_until": e["days_until"],
            "impacted_count": e["impacted_count"],
            "severity": e["severity"],
        }
        for e in events
        if e["days_until"] is None or e["days_until"] >= 0
    ][:rail_limit]

    def _count(pred) -> int:
        return sum(1 for e in events if pred(e))

    counts = {
        "total": len(events),
        "retirement": _count(lambda e: e["change_type"] == RETIREMENT),
        "breaking_change": _count(lambda e: e["change_type"] == BREAKING_CHANGE),
        "red": _count(lambda e: e["severity"] == "red"),
        "amber": _count(lambda e: e["severity"] == "amber"),
        "grey": _count(lambda e: e["severity"] == "grey"),
        "unowned": _count(lambda e: e["unowned"]),
        "impacted_total": sum(e["impacted_count"] for e in events),
        "models": len(model_items),
        "models_at_risk": sum(1 for m in model_items if m["severity"] in ("red", "amber")),
    }
    return {
        "generated_at": _now_iso(),
        "rail": rail,
        "events": events,
        "model_items": model_items,
        "counts": counts,
    }


# --------------------------------------------------------------------- live queries
async def _query_service_health(subs: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Service Health retirement / health-advisory events from ARG."""
    sub_clause = ""
    if subs:
        joined = ", ".join(f"'{_esc(s)}'" for s in subs)
        sub_clause = f"| where subscriptionId in~ ({joined})"
    kql = (
        "servicehealthresources "
        "| where type =~ 'microsoft.resourcehealth/events' "
        f"{sub_clause} "
        "| extend p = parse_json(properties) "
        "| where tostring(p.EventSubType) =~ 'Retirement' or tostring(p.EventType) in~ ('HealthAdvisory','PlannedMaintenance') "
        "| project trackingId = tostring(p.TrackingId), title = tostring(p.Title), "
        "summary = tostring(p.Summary), impactStartTime = tostring(p.ImpactStartTime), "
        "eventType = tostring(p.EventType), eventSubType = tostring(p.EventSubType), "
        "link = tostring(p.ExternalIncidentId) "
        "| take 200"
    )
    res = await run_kql_capture(kql, connection, output="json")
    if not res.ok:
        raise RuntimeError(res.error or "Service Health query failed.")
    out: list[dict[str, Any]] = []
    for r in _parse_rows(res.stdout):
        out.append(
            {
                "source": "service_health",
                "tracking_id": r.get("trackingId", ""),
                "title": r.get("title", ""),
                "summary": r.get("summary", ""),
                "retirement_date": r.get("impactStartTime", ""),
                "change_type": BREAKING_CHANGE if str(r.get("eventSubType", "")).lower() != "retirement" else RETIREMENT,
                "impacted_resources": [],
            }
        )
    return out


async def _query_advisor(predicate: str, connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Advisor service-upgrade/retirement recommendations + their impacted resources.

    ``predicate`` scopes by subscription/rg/resource (the workload scope predicate, applied
    against the impacted resource id)."""
    where = f"| where {predicate}" if predicate else ""
    kql = (
        "advisorresources "
        "| where type =~ 'microsoft.advisor/recommendations' "
        "| extend p = parse_json(properties) "
        "| where tostring(p.category) =~ 'HighAvailability' "
        "| where tostring(p.shortDescription.problem) has_any ('retire','retirement','upgrade','deprecat','end of','end-of') "
        "| extend impactedId = tostring(p.resourceMetadata.resourceId) "
        f"{where} "
        "| project trackingId = tostring(p.recommendationTypeId), "
        "problem = tostring(p.shortDescription.problem), "
        "solution = tostring(p.shortDescription.solution), "
        "impactedId, impactedType = tostring(p.impactedField), "
        "link = tostring(p.learnMoreLink) "
        "| take 500"
    )
    res = await run_kql_capture(kql, connection, output="json")
    if not res.ok:
        raise RuntimeError(res.error or "Advisor query failed.")
    rows = _parse_rows(res.stdout)
    if not rows:
        return []

    # Hydrate impacted-resource metadata (name/rg/region/tags) in one ARG pass.
    ids = sorted({str(r.get("impactedId", "")) for r in rows if r.get("impactedId")})
    meta = await _query_resource_meta(ids, connection)

    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        tid = r.get("trackingId", "") or _synth_tracking_id("advisor", r.get("problem", ""))
        ev = grouped.get(tid)
        if ev is None:
            ev = {
                "source": "advisor",
                "tracking_id": tid,
                "title": r.get("problem", "") or "Service upgrade / retirement",
                "summary": r.get("problem", ""),
                "recommended_replacement": r.get("solution", ""),
                "migration_url": r.get("link", ""),
                "impacted_resources": [],
            }
            grouped[tid] = ev
        rid = str(r.get("impactedId", ""))
        if rid:
            ev["impacted_resources"].append(meta.get(rid.lower()) or {"id": rid, "type": r.get("impactedType", "")})
    return list(grouped.values())


async def _query_resource_meta(ids: list[str], connection: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    joined = ", ".join(f"'{_esc(i)}'" for i in ids[:1000])
    kql = (
        f"Resources | where id in~ ({joined}) "
        "| project id, name, type, resourceGroup, location, subscriptionId, tags | take 1000"
    )
    res = await run_kql_capture(kql, connection, output="json")
    out: dict[str, dict[str, Any]] = {}
    if res.ok:
        for r in _parse_rows(res.stdout):
            out[str(r.get("id", "")).lower()] = r
    return out


async def _query_aoai_deployments(predicate: str, connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Azure OpenAI / Foundry deployments via ARG (the model lane)."""
    where = f"| where {predicate}" if predicate else ""
    kql = (
        "Resources "
        "| where type =~ 'microsoft.cognitiveservices/accounts/deployments' "
        f"{where} "
        "| extend p = parse_json(properties) "
        "| project id, accountName = tostring(split(id,'/')[8]), deployment = name, "
        "model = tostring(p.model.name), modelVersion = tostring(p.model.version), "
        "location, resourceGroup, subscriptionId | take 500"
    )
    res = await run_kql_capture(kql, connection, output="json")
    if not res.ok:
        return []
    out: list[dict[str, Any]] = []
    for r in _parse_rows(res.stdout):
        out.append(
            {
                "id": r.get("id", ""),
                "account": r.get("accountName", ""),
                "deployment": r.get("deployment", ""),
                "model": r.get("model", ""),
                "model_version": r.get("modelVersion", ""),
                "region": r.get("location", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
            }
        )
    return out


async def collect_radar(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    tenant_id: str = "",
) -> dict[str, Any]:
    from app.assessments.runner import _resolve_scope

    subscriptions: list[str] = []
    predicate = ""
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        subscriptions = list(scope.get("subscriptions") or [])
        for sub, _rg in scope.get("rg_pairs") or []:
            if sub not in subscriptions:
                subscriptions.append(sub)
        if scope.get("error") and not predicate:
            return _empty_snapshot(scope_kind, scope_id, error=scope["error"])
    elif scope_kind == "subscription" and scope_id:
        predicate = f"subscriptionId =~ '{_esc(scope_id)}'"
        subscriptions = [scope_id]
    else:
        return _empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    notes: list[str] = []
    raw: list[dict[str, Any]] = []
    try:
        raw += await _query_advisor(predicate, connection)
    except RuntimeError as exc:
        notes.append(f"Advisor: {str(exc)[:160]}")
    try:
        raw += await _query_service_health(subscriptions, connection)
    except RuntimeError as exc:
        notes.append(f"Service Health: {str(exc)[:160]}")

    # Optional Azure Updates public feed (the only net-new external fetch).
    try:
        from app.core.app_settings import load_settings

        s = load_settings()
        if s.get("radar_azure_updates_feed_enabled"):
            from app.radar.feed import fetch_azure_updates

            raw += await fetch_azure_updates(s.get("radar_azure_updates_feed_url", ""))
            notes.append("Azure Updates feed included (may lag announcements ~2 weeks).")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Azure Updates feed: {str(exc)[:120]}")

    deployments: list[dict[str, Any]] = []
    try:
        deployments = await _query_aoai_deployments(predicate, connection)
    except RuntimeError as exc:
        notes.append(f"AOAI deployments: {str(exc)[:160]}")

    wl_index = _workload_index()
    events = merge_events(raw, wl_index=wl_index, tenant_id=tenant_id)
    model_items = build_model_items(deployments)
    snap = compute_radar(events, model_items)
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph",
            "demo": False,
            "error": "; ".join(notes),
        }
    )
    return snap


def _empty_snapshot(scope_kind: str, scope_id: str, *, error: str) -> dict[str, Any]:
    snap = compute_radar([], [])
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": scope_id,
            "connection_configured": False,
            "source": "azure_resource_graph",
            "demo": False,
            "error": error,
        }
    )
    return snap


# Imported late to avoid a heavy import at module load (mirrors the coverage collectors).
from app.exec.command_runner import run_kql_capture  # noqa: E402
