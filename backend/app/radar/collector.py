"""Radar collection + aggregation.

Pulls retirement/breaking-change signals from two Azure Resource Graph tables —
``servicehealthresources`` (planned-maintenance / health-advisory / retirement events) and
``advisorresources`` (Advisor "Service Upgrade and Retirement" recommendations, which carry
resource-level impact) — plus the Azure OpenAI/Foundry deployment inventory for the model
lane. Everything runs on the ungated, read-only paged KQL collection path.

``merge_events`` and ``compute_radar`` are pure functions over already-fetched rows, so
they're unit-testable and power the demo seed. ``collect_radar`` resolves the scope and
gathers the rows from Azure."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.radar.builtin_seed import (
    BREAKING_CHANGE,
    RETIREMENT,
    classify_text,
    model_lifecycle_index,
)
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
    # Some Azure feeds (Service Health / Advisor properties) serialize dates as a .NET
    # DateTime tick count — a big all-digit integer of 100-nanosecond intervals since
    # 0001-01-01 (e.g. 639122472566870000). Also tolerate Unix epoch milliseconds/seconds.
    if s.isdigit():
        try:
            n = int(s)
            if n >= 10**17:  # .NET ticks (100ns since year 1)
                return (datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=n // 10)).date()
            if n >= 10**12:  # Unix epoch milliseconds
                return datetime.fromtimestamp(n / 1000, tz=timezone.utc).date()
            if n >= 10**9:   # Unix epoch seconds
                return datetime.fromtimestamp(n, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    # Parse full ISO timestamps first so an explicit offset is converted to UTC before
    # selecting the calendar date. Stripping the offset first moved near-midnight
    # deadlines by one day and could put them in the wrong severity band.
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None


def _iso_date(value: Any) -> str:
    """Normalize a date-ish value (ISO string, .NET ticks, epoch) to an ``YYYY-MM-DD`` string,
    or "" when it can't be parsed — so the UI never shows a raw tick number."""
    d = _parse_date(value)
    return d.isoformat() if d else ""


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
    return "radar-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _impact_scope(value: Any) -> list[dict[str, Any]]:
    """Normalize Service Health's service/region impact without calling it a resource list.

    The Resource Health event contract exposes affected services, regions and subscriptions,
    but not the concrete ARM resource IDs shown by some Azure portal experiences.  Keeping
    that distinction prevents a missing resource-level list from becoming a false zero.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for service in value:
        if not isinstance(service, dict):
            continue
        regions: set[str] = set()
        subscriptions: set[str] = set()
        for region in service.get("impactedRegions") or service.get("ImpactedRegions") or []:
            if not isinstance(region, dict):
                continue
            name = str(region.get("impactedRegion") or region.get("ImpactedRegion") or "").strip()
            if name:
                regions.add(name)
            for subscription in region.get("impactedSubscriptions") or region.get("ImpactedSubscriptions") or []:
                subscription_value = str(subscription).strip()
                if subscription_value:
                    subscriptions.add(subscription_value)
        name = str(service.get("impactedService") or service.get("ImpactedService") or "").strip()
        if name or regions or subscriptions:
            out.append({
                "service": name,
                "regions": sorted(regions),
                "subscriptions": sorted(subscriptions),
            })
    return out


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
                "impact_scope": [],
                "impact_count_known": False,
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
        existing["impact_count_known"] = bool(
            existing["impact_count_known"] or ev.get("impact_count_known") or impacted
        )
        for scope in ev.get("impact_scope") or []:
            if scope not in existing["impact_scope"]:
                existing["impact_scope"].append(scope)
        for r in impacted:
            rid = str(r.get("id", "")).lower()
            if rid:
                existing["_impacted"][rid] = r

    out: list[dict[str, Any]] = []
    for ev in by_tid.values():
        impacted = resolve_owners(list(ev.pop("_impacted").values()), wl_index, tenant_id=tenant_id, own_ctx=own_ctx)
        owners = [r["owner"] for r in impacted if r["owner"]]
        dominant = max(set(owners), key=owners.count) if owners else ""
        # No resource-level list means ownership is unknown, not "unowned".  Only
        # resolved resources can support an ownership conclusion.
        unowned = any(r["unowned"] for r in impacted)
        d = days_until(ev["retirement_date"], today=today)
        ev.update(
            {
                "impacted_resources": impacted,
                "impacted_count": len(impacted),
                "owner": dominant,
                "unowned": unowned,
                # Normalize to a clean ISO date so the UI never shows a raw .NET tick number.
                "retirement_date": _iso_date(ev.get("retirement_date")),
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
                "retirement_date": _iso_date(retire),
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

    unique_impacted_ids = {
        str(resource.get("id", "")).rstrip("/").lower()
        for event in events
        for resource in event.get("impacted_resources") or []
        if str(resource.get("id", "")).strip()
    }
    counts = {
        "total": len(events),
        "retirement": _count(lambda e: e["change_type"] == RETIREMENT),
        "breaking_change": _count(lambda e: e["change_type"] == BREAKING_CHANGE),
        "red": _count(lambda e: e["severity"] == "red"),
        "amber": _count(lambda e: e["severity"] == "amber"),
        "grey": _count(lambda e: e["severity"] == "grey"),
        "unowned": _count(lambda e: e["unowned"]),
        # Estate KPI: a resource affected by two notices is still one resource.
        "impacted_total": len(unique_impacted_ids),
        "impact_counts_complete": all(e.get("impact_count_known", False) for e in events),
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
        "link = tostring(p.ExternalIncidentId), impact = tostring(p.Impact)"
    )
    res = await run_kql_collect(kql, connection, max_rows=10_000)
    if not res.ok:
        raise RuntimeError(res.error or "Service Health query failed.")
    out: list[dict[str, Any]] = []
    for r in res.rows:
        out.append(
            {
                "source": "service_health",
                "tracking_id": r.get("trackingId", ""),
                "title": r.get("title", ""),
                "summary": r.get("summary", ""),
                "retirement_date": r.get("impactStartTime", ""),
                "change_type": BREAKING_CHANGE if str(r.get("eventSubType", "")).lower() != "retirement" else RETIREMENT,
                "impacted_resources": [],
                # ARG/Resource Health provides service/region/subscription impact here,
                # not concrete ARM resource IDs. Do not report that missing list as zero.
                "impact_scope": _impact_scope(r.get("impact")),
                "impact_count_known": False,
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
        "link = tostring(p.learnMoreLink)"
    )
    res = await run_kql_collect(kql, connection, max_rows=10_000)
    if not res.ok:
        raise RuntimeError(res.error or "Advisor query failed.")
    rows = res.rows
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
                "impact_count_known": True,
            }
            grouped[tid] = ev
        rid = str(r.get("impactedId", ""))
        if rid:
            ev["impacted_resources"].append(meta.get(rid.lower()) or {"id": rid, "type": r.get("impactedType", "")})
    return list(grouped.values())


async def _query_resource_meta(ids: list[str], connection: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    current: list[str] = []
    current_len = 100
    batches: list[list[str]] = []
    for resource_id in sorted(set(ids)):
        added = len(resource_id) + 4
        if current and current_len + added > 6000:
            batches.append(current)
            current, current_len = [], 100
        current.append(resource_id)
        current_len += added
    if current:
        batches.append(current)
    for batch in batches:
        joined = ", ".join(f"'{_esc(i)}'" for i in batch)
        kql = (
            f"Resources | where id in~ ({joined}) "
            "| project id, name, type, resourceGroup, location, subscriptionId, tags"
        )
        res = await run_kql_collect(kql, connection, max_rows=len(batch))
        if not res.ok:
            raise RuntimeError(res.error or "Resource metadata query failed.")
        for r in res.rows:
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
        "location, resourceGroup, subscriptionId"
    )
    res = await run_kql_collect(kql, connection, max_rows=10_000)
    if not res.ok:
        raise RuntimeError(res.error or "Azure OpenAI deployment query failed.")
    out: list[dict[str, Any]] = []
    for r in res.rows:
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


def _subscription_batches(subscriptions: list[str], size: int = 100) -> list[list[str]]:
    values = sorted({str(value).strip() for value in subscriptions if str(value).strip()})
    return [values[index:index + size] for index in range(0, len(values), size)]


def _resource_in_workload(resource: dict[str, Any], scope: dict[str, Any]) -> bool:
    resource_id = str(resource.get("id") or resource.get("impactedId") or "").rstrip("/").lower()
    subscription_id = str(resource.get("subscriptionId") or "").lower()
    if not subscription_id:
        match = re.search(r"/subscriptions/([^/]+)", resource_id, re.IGNORECASE)
        subscription_id = match.group(1).lower() if match else ""
    resource_group = str(resource.get("resourceGroup") or "").lower()
    if not resource_group:
        match = re.search(r"/resourcegroups/([^/]+)", resource_id, re.IGNORECASE)
        resource_group = match.group(1).lower() if match else ""
    memberships = scope.get("memberships") or []
    if memberships:
        for member in memberships:
            kind = member.get("kind")
            included = (
                (kind in {"subscription", "mg"} and subscription_id in member.get("subscriptions", set()))
                or (kind == "resource_group" and subscription_id == member.get("subscription") and resource_group == member.get("resource_group"))
                or (kind == "resource" and resource_id == member.get("resource_id"))
            )
            if not included:
                continue
            excluded = any(
                resource_id == value or resource_id.startswith(value + "/")
                for value in member.get("excludes") or set()
            )
            if not excluded:
                return True
        return False
    whole_subscriptions = {str(value).lower() for value in scope.get("subscriptions") or []}
    resource_groups = {
        (str(subscription).lower(), str(group).lower())
        for subscription, group in scope.get("rg_pairs") or []
    }
    resource_ids = {str(value).rstrip("/").lower() for value in scope.get("resource_ids") or []}
    return (
        subscription_id in whole_subscriptions
        or (subscription_id, resource_group) in resource_groups
        or resource_id in resource_ids
    )


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
    resolved_scope: dict[str, Any] = {}
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        resolved_scope = scope
        predicate = scope.get("predicate") or ""
        subscriptions = list(scope.get("effective_subscriptions") or scope.get("subscriptions") or [])
        if scope.get("error") and not predicate:
            return _empty_snapshot(scope_kind, scope_id, error=scope["error"])
    elif scope_kind == "subscription" and scope_id:
        predicate = f"subscriptionId =~ '{_esc(scope_id)}'"
        subscriptions = [scope_id]
        resolved_scope = {
            "subscriptions": [scope_id], "effective_subscriptions": [scope_id],
            "rg_pairs": [], "resource_ids": [], "predicate": predicate,
        }
    else:
        return _empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    batches = _subscription_batches(subscriptions)
    if not batches:
        return _empty_snapshot(scope_kind, scope_id, error="No visible subscriptions were resolved for this scope.")

    notes: list[str] = []
    raw: list[dict[str, Any]] = []
    deployments: list[dict[str, Any]] = []
    source_status: dict[str, dict[str, Any]] = {}

    async def collect_batches(kind: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for batch in batches:
            sub_predicate = "subscriptionId in~ (" + ", ".join(f"'{_esc(s)}'" for s in batch) + ")"
            if kind == "advisor":
                results.extend(await _query_advisor(sub_predicate, connection))
            elif kind == "service_health":
                results.extend(await _query_service_health(batch, connection))
            else:
                results.extend(await _query_aoai_deployments(sub_predicate, connection))
        return results

    # Independent Azure reads run concurrently; each source batches internally to keep KQL bounded.
    adv_res, sh_res, aoai_res = await asyncio.gather(
        collect_batches("advisor"), collect_batches("service_health"), collect_batches("aoai"),
        return_exceptions=True,
    )
    if isinstance(adv_res, list):
        if scope_kind == "workload":
            filtered_advisor: list[dict[str, Any]] = []
            for event in adv_res:
                impacts = [
                    item for item in event.get("impacted_resources") or []
                    if _resource_in_workload(item, resolved_scope)
                ]
                if impacts:
                    filtered_advisor.append({**event, "impacted_resources": impacts})
            adv_res = filtered_advisor
        raw += adv_res
        source_status["advisor"] = {"status": "ok", "rows": len(adv_res), "batches": len(batches)}
    elif isinstance(adv_res, BaseException):
        notes.append(f"Advisor: {str(adv_res)[:160]}")
        source_status["advisor"] = {"status": "failed", "rows": 0, "batches": len(batches)}
    if isinstance(sh_res, list):
        raw += sh_res
        source_status["service_health"] = {"status": "ok", "rows": len(sh_res), "batches": len(batches)}
    elif isinstance(sh_res, BaseException):
        notes.append(f"Service Health: {str(sh_res)[:160]}")
        source_status["service_health"] = {"status": "failed", "rows": 0, "batches": len(batches)}
    if isinstance(aoai_res, list):
        deployments = (
            [row for row in aoai_res if _resource_in_workload(row, resolved_scope)]
            if scope_kind == "workload" else aoai_res
        )
        source_status["model_lifecycle"] = {"status": "ok", "rows": len(deployments), "batches": len(batches)}
    elif isinstance(aoai_res, BaseException):
        notes.append(f"AOAI deployments: {str(aoai_res)[:160]}")
        source_status["model_lifecycle"] = {"status": "failed", "rows": 0, "batches": len(batches)}

    # Optional Azure Updates public feed (the only net-new external fetch).
    try:
        from app.core.app_settings import load_settings

        s = load_settings()
        if s.get("radar_azure_updates_feed_enabled"):
            from app.radar.feed import fetch_azure_updates

            feed_rows = await fetch_azure_updates(s.get("radar_azure_updates_feed_url", ""))
            raw += feed_rows
            notes.append("Azure Updates feed included (may lag announcements ~2 weeks).")
            source_status["azure_updates_feed"] = {"status": "ok", "rows": len(feed_rows), "optional": True}
        else:
            source_status["azure_updates_feed"] = {"status": "disabled", "rows": 0, "optional": True}
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Azure Updates feed: {str(exc)[:120]}")
        source_status["azure_updates_feed"] = {"status": "failed", "rows": 0, "optional": True}

    wl_index = _workload_index()
    events = merge_events(raw, wl_index=wl_index, tenant_id=tenant_id)
    model_items = build_model_items(deployments)
    snap = compute_radar(events, model_items)
    required = ["advisor", "service_health", "model_lifecycle"]
    failed_required = [name for name in required if source_status.get(name, {}).get("status") == "failed"]
    collection_failed = len(failed_required) == len(required)
    partial = bool(failed_required)
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph",
            "demo": False,
            "partial": partial,
            "collection_failed": collection_failed,
            "source_status": source_status,
            "warnings": notes,
            "error": "; ".join(notes) if collection_failed else "",
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
from app.exec.command_runner import run_kql_collect  # noqa: E402
