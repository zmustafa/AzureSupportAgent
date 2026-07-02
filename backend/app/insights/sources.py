"""Gather stage — source adapters that pull structured data for a pack run.

Each adapter is keyed by a source id (``change_explorer``, ...) and returns a normalized
bundle the reason stage can interpret:

    {
      "source": "change_explorer",
      "ok": bool,
      "note": str,                 # human note (errors / demo / no-connection)
      "events": [ {time, workload, change, risk, risk_rank, category, operation,
                   actor, resource, resource_type, flags:[code], ...} ],
      "flag_codes": set[str],      # deterministic security flag codes present (for the gate floor)
      "counts": {total, critical, high, medium, low},
    }

Adapters are deterministic and read-only — the LLM never queries Azure here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("app.insights.sources")

# Sources selectable in a pack (id -> label + which scopes it supports). Each adapter reads
# the feature's most recent cached snapshot (dashboard-style) — it never triggers a fresh,
# expensive Azure scan — and normalizes items into the shared event shape.
SOURCE_CATALOG: list[dict[str, Any]] = [
    {"id": "change_explorer", "label": "Change Explorer", "icon": "🕵️",
     "description": "Resource, network, RBAC and security changes (Azure Resource Graph + Activity Log)."},
    {"id": "radar", "label": "Radar (retirements)", "icon": "📡",
     "description": "Upcoming service retirements and breaking changes impacting this scope."},
    {"id": "cost", "label": "Cost cleanup", "icon": "💰",
     "description": "Idle / orphaned resources and their estimated monthly waste (from inventory + cost)."},
    {"id": "rbac", "label": "Access (RBAC)", "icon": "🛂",
     "description": "Privileged and PIM-eligible role assignments across the tenant."},
    {"id": "assessments", "label": "Assessments", "icon": "🛡️",
     "description": "Failing critical/high findings from the latest Well-Architected assessment run."},
    {"id": "backup", "label": "Backup & DR", "icon": "💾",
     "description": "Unprotected resources and unhealthy disaster-recovery pairs."},
    {"id": "identity", "label": "Identity risk", "icon": "🔑",
     "description": "Expiring secrets/certs, privileged users without MFA, and ownerless apps."},
    {"id": "policy", "label": "Policy compliance", "icon": "📏",
     "description": "Non-compliant resources and policy exemptions from the latest snapshot."},
]

_RISK_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "informational": 0}


def _rank(risk: str) -> int:
    return _RISK_RANK.get(str(risk or "low").lower(), 0)


def source_label(source_id: str) -> str:
    for s in SOURCE_CATALOG:
        if s["id"] == source_id:
            return s["label"]
    return source_id


def scope_label(scope: dict[str, Any]) -> str:
    """A human label for the resolved scope, used to fill the ``{{scope_label}}`` placeholder."""
    mode = (scope or {}).get("mode", "workload")
    if mode == "tenant":
        return "the whole tenant"
    if mode == "subscription":
        return scope.get("subscription_name") or f"subscription {str(scope.get('subscription_id', ''))[:8]}…"
    wids = scope.get("workload_ids") or ([scope["workload_id"]] if scope.get("workload_id") else [])
    if mode == "workload_dependencies":
        return f"{len(wids)} workload(s) + dependencies" if len(wids) != 1 else "the workload + its dependencies"
    return f"{len(wids)} workload(s)" if len(wids) != 1 else "the workload"


def _iso_window(lookback_hours: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(1, lookback_hours))
    return start.isoformat(), end.isoformat()


async def gather(sources: list[str], scope: dict[str, Any], *, tenant_id: str,
                 lookback_hours: int, filters: dict[str, Any], pack_id: str = "") -> list[dict[str, Any]]:
    """Run every selected, supported source adapter and return their bundles.

    When ``pack_id`` is set, each bundle's events are diffed against the pack's previous run
    on this scope (day-over-day): new events are tagged ``new=True`` and the per-source note
    records how many are new since last time. The fresh fingerprint is then persisted.
    """
    from app.insights import snapshots

    adapters = {
        "change_explorer": _gather_change_explorer,
        "radar": _gather_radar,
        "cost": _gather_cost,
        "rbac": _gather_rbac,
        "assessments": _gather_assessments,
        "backup": _gather_backup,
        "identity": _gather_identity,
        "policy": _gather_policy,
    }
    bundles: list[dict[str, Any]] = []
    for sid in sources:
        fn = adapters.get(sid)
        if fn is None:  # unknown id — report as an empty, non-fatal bundle
            bundles.append({"source": sid, "ok": False, "note": f"Source '{sid}' is not available yet.",
                            "events": [], "flag_codes": set(), "counts": {}})
            continue
        try:
            bundles.append(await fn(scope, tenant_id=tenant_id, lookback_hours=lookback_hours, filters=filters))
        except Exception as exc:  # noqa: BLE001 — one source must never crash the run
            log.warning("Source '%s' gather failed: %s", sid, exc)
            bundles.append({"source": sid, "ok": False, "note": f"{sid} unavailable: {exc}"[:300],
                            "events": [], "flag_codes": set(), "counts": {}})

    _apply_day_over_day(bundles, tenant_id=tenant_id, pack_id=pack_id, scope=scope, snapshots=snapshots)
    return bundles


def _apply_day_over_day(bundles: list[dict[str, Any]], *, tenant_id: str, pack_id: str,
                        scope: dict[str, Any], snapshots: Any) -> None:
    """Tag events new-since-last-run and persist the fresh per-source id fingerprint."""
    if not pack_id:
        return
    prior = snapshots.load(tenant_id, pack_id, scope)
    current: dict[str, list[str]] = {}
    for b in bundles:
        if not b.get("ok"):
            continue
        src = b.get("source", "?")
        seen_before = set(prior.get(src) or [])
        ids: list[str] = []
        new_count = 0
        for e in b.get("events") or []:
            eid = str(e.get("_id") or "")
            if eid:
                ids.append(eid)
                if seen_before and eid not in seen_before:
                    e["new"] = True
                    new_count += 1
        current[src] = ids
        if seen_before and new_count:
            extra = f"{new_count} new since last run"
            b["note"] = f"{b['note']}; {extra}" if b.get("note") else extra
        b.setdefault("counts", {})["new"] = new_count
    snapshots.save(tenant_id, pack_id, scope, current)


def _resolve_scope(scope: dict[str, Any]):
    """Resolve a pack scope to (workload_dict, connection, change_scope_mode, force_demo)."""
    from app.changeexplorer import demo

    mode = (scope or {}).get("mode", "workload")
    if mode == "subscription":
        from app.core.azure_connections import get_default_connection, resolve_connection

        sub_id = scope.get("subscription_id", "")
        conn = resolve_connection(scope.get("connection_id")) if scope.get("connection_id") else get_default_connection()
        wl = {"id": f"sub:{sub_id}", "name": scope.get("subscription_name") or f"Subscription {sub_id[:8]}…",
              "nodes": [{"kind": "subscription", "id": sub_id, "subscription_id": sub_id}]}
        return wl, conn, "tenant", False
    # workload / workload_dependencies / tenant all resolve against the first workload id.
    wids = scope.get("workload_ids") or ([scope["workload_id"]] if scope.get("workload_id") else [])
    wid = wids[0] if wids else ""
    if demo.is_demo(wid):
        return demo.demo_workload(), None, ("workload_dependencies" if mode == "workload_dependencies" else "workload"), True
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.workloads.registry import get_workload

    wl = get_workload(wid)
    if wl is None:
        return None, None, "workload", False
    conn = resolve_connection(scope.get("connection_id")) if scope.get("connection_id") else connection_for_workload(wl)
    cmode = "tenant" if mode == "tenant" else ("workload_dependencies" if mode == "workload_dependencies" else "workload")
    return wl, conn, cmode, False


def _passes_filters(ev: dict[str, Any], filters: dict[str, Any]) -> bool:
    cats = filters.get("categories") or []
    if cats and ev.get("category") not in cats:
        return False
    ops = filters.get("operations") or []
    if ops:
        op = str(ev.get("operation", "")).lower()
        if not any(o.lower() in op for o in ops):
            return False
    min_risk = filters.get("min_risk", "low")
    if _RISK_RANK.get(str(ev.get("risk", "low")).lower(), 0) < _RISK_RANK.get(min_risk, 0):
        return False
    return True


def _passes_risk(ev: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Lighter filter for non-change sources: honour only the ``min_risk`` floor (the
    category/operation filters are change-log concepts that don't apply here)."""
    min_risk = filters.get("min_risk", "low")
    return _rank(ev.get("risk", "low")) >= _RISK_RANK.get(min_risk, 0)


def _mk(events: list[dict[str, Any]], *, source: str, ok: bool = True, note: str = "",
        flag_codes: set[str] | None = None) -> dict[str, Any]:
    """Assemble a normalized bundle with derived counts, sorted by risk then recency."""
    events.sort(key=lambda x: (-x.get("risk_rank", 0), x.get("time", "")))
    counts = {"total": len(events), "critical": 0, "high": 0, "medium": 0, "low": 0}
    for e in events:
        r = str(e.get("risk", "low")).lower()
        if r in counts:
            counts[r] += 1
    return {"source": source, "ok": ok, "note": note[:300], "events": events,
            "flag_codes": flag_codes or set(), "counts": counts}


def _resolve_target(scope: dict[str, Any]):
    """Resolve a pack scope to (workload, connection, scope_kind, scope_id, force_demo).

    ``scope_kind``/``scope_id`` follow the coverage-collector convention ("workload"/id or
    "subscription"/id). Tenant scope anchors to the first workload (mirroring Change Explorer).
    """
    from app.changeexplorer import demo

    mode = (scope or {}).get("mode", "workload")
    if mode == "subscription":
        from app.core.azure_connections import get_default_connection, resolve_connection

        sub_id = scope.get("subscription_id", "")
        conn = resolve_connection(scope.get("connection_id")) if scope.get("connection_id") else get_default_connection()
        return None, conn, "subscription", sub_id, False
    wids = scope.get("workload_ids") or ([scope["workload_id"]] if scope.get("workload_id") else [])
    wid = wids[0] if wids else ""
    if demo.is_demo(wid):
        wl = demo.demo_workload()
        return wl, None, "workload", wl.get("id", wid), True
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.workloads.registry import get_workload

    wl = get_workload(wid)
    if wl is None:
        return None, None, "workload", wid, False
    conn = resolve_connection(scope.get("connection_id")) if scope.get("connection_id") else connection_for_workload(wl)
    return wl, conn, "workload", wid, False


def _subscription_ids(workload: dict[str, Any] | None) -> set[str]:
    """Distinct subscription ids referenced by a workload's nodes (for tenant-wide filtering)."""
    out: set[str] = set()
    for n in (workload or {}).get("nodes", []) or []:
        sid = str(n.get("subscription_id") or n.get("subscriptionId") or "").lower()
        if sid:
            out.add(sid)
    return out



async def _gather_change_explorer(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                                  filters: dict[str, Any]) -> dict[str, Any]:
    from app.changeexplorer import security as ce_security
    from app.changeexplorer import service as ce_service

    workload, conn, cmode, force_demo = _resolve_scope(scope)
    if workload is None:
        return {"source": "change_explorer", "ok": False, "note": "Scope could not be resolved (workload not found).",
                "events": [], "flag_codes": set(), "counts": {}}
    start_iso, end_iso = _iso_window(lookback_hours)
    try:
        run = await ce_service.analyze(
            tenant_id=tenant_id, workload=workload, connection=conn,
            start_iso=start_iso, end_iso=end_iso, scope_mode=cmode,
            requested_by="insight-pack", force_demo=force_demo, run_ai=False,
        )
    except Exception as exc:  # noqa: BLE001 — a source failure must not crash the run
        log.warning("Change Explorer gather failed: %s", exc)
        return {"source": "change_explorer", "ok": False, "note": f"Change Explorer query failed: {exc}"[:300],
                "events": [], "flag_codes": set(), "counts": {}}

    raw_events = run.get("events") or []
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    counts = {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0}
    wl_name = workload.get("name", "workload")
    for e in raw_events:
        risk = str(e.get("riskLabel", "") or "low").lower()
        norm = {
            "time": e.get("eventTime", ""),
            "workload": wl_name,
            "change": (e.get("summary") or e.get("operation") or "").strip() or e.get("resourceName", ""),
            "risk": risk,
            "risk_rank": _RISK_RANK.get(risk, 0),
            "category": e.get("category", ""),
            "operation": e.get("operation", ""),
            "actor": e.get("actor", "") or "unknown",
            "resource": e.get("resourceName", ""),
            "resource_type": e.get("resourceType", ""),
        }
        if not _passes_filters(norm, filters):
            continue
        flags = ce_security.flag_event(e)
        norm["flags"] = [f["code"] for f in flags]
        norm["_id"] = f"{norm['time']}|{norm['resource']}|{norm['operation']}"
        flag_codes.update(norm["flags"])
        events.append(norm)
        counts["total"] += 1
        if risk in counts:
            counts[risk] += 1
    events.sort(key=lambda x: (-x["risk_rank"], x.get("time", "")))
    notes = run.get("notes") or []
    note = "; ".join(n for n in notes if n) if not conn and not force_demo else ("Demo data." if force_demo else "")
    return {"source": "change_explorer", "ok": True, "note": note[:300],
            "events": events, "flag_codes": flag_codes, "counts": counts}


def _unavailable(source: str, note: str) -> dict[str, Any]:
    return {"source": source, "ok": False, "note": note[:300], "events": [],
            "flag_codes": set(), "counts": {}}


# --------------------------------------------------------------------------- radar
async def _gather_radar(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                        filters: dict[str, Any]) -> dict[str, Any]:
    from app.radar import cache as radar_cache
    from app.radar import demo as radar_demo

    wl, conn, scope_kind, scope_id, force_demo = _resolve_target(scope)
    if not scope_id:
        return _unavailable("radar", "Scope has no workload/subscription to read radar for.")
    snap = radar_cache.read_snapshot(tenant_id, scope_kind, scope_id)
    if snap is None and (force_demo or radar_demo.is_demo_scope(scope_kind, scope_id)):
        snap = radar_demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
    if not snap:
        return _unavailable("radar", "No radar snapshot yet — open Radar to scan this scope.")

    sev_risk = {"red": "high", "amber": "medium", "grey": "low"}
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    scope_name = snap.get("scope_name", "") or (wl or {}).get("name", "")
    for ev in snap.get("events", []) or []:
        sev = str(ev.get("severity", "grey")).lower()
        risk = sev_risk.get(sev, "low")
        ctype = str(ev.get("change_type", "") or "")
        du = ev.get("days_until")
        change = (ev.get("title") or ev.get("summary") or "").strip()
        if du is not None:
            change = f"{change} (in {du}d)"
        norm = {
            "time": ev.get("retirement_date") or snap.get("generated_at", ""),
            "workload": scope_name,
            "change": change,
            "risk": risk, "risk_rank": _rank(risk),
            "category": "retirement" if ctype == "retirement" else "breaking_change",
            "operation": ctype or "advisory",
            "actor": ev.get("source", "") or "azure",
            "resource": ev.get("service", "") or ev.get("title", ""),
            "resource_type": "Azure service",
            "_id": str(ev.get("id") or ev.get("tracking_id") or ev.get("title", "")),
        }
        if not _passes_risk(norm, filters):
            continue
        codes: list[str] = []
        if ctype == "retirement" and sev in ("red", "amber"):
            codes.append("retirement_soon")
        if ctype == "breaking_change":
            codes.append("breaking_change")
        norm["flags"] = codes
        flag_codes.update(codes)
        events.append(norm)
    note = "Demo data." if snap.get("demo") else ""
    return _mk(events, source="radar", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- cost
async def _gather_cost(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                       filters: dict[str, Any]) -> dict[str, Any]:
    from app.inventory import cache as inv_cache
    from app.inventory import cost as inv_cost
    from app.inventory import optimization

    wl, conn, scope_kind, scope_id, force_demo = _resolve_target(scope)
    cid = str((conn or {}).get("id") or (conn or {}).get("connection_id") or "")
    sub_scope = scope_id if scope_kind == "subscription" else ""
    hit = inv_cache.get(tenant_id, cid, scope=sub_scope) if cid else None
    if not hit:
        return _unavailable("cost", "No inventory cache — open Inventory to scan this scope.")
    payload = hit.get("payload") or {}
    resources = payload.get("resources") or payload.get("items") or []
    cost_payload = inv_cost.peek_cost(tenant_id, cid, scope=sub_scope) if cid else None
    report = optimization.analyze_resources(resources, cost_payload)

    mode = (scope or {}).get("mode", "workload")
    sub_filter = _subscription_ids(wl) if mode in ("workload", "workload_dependencies") else None
    currency = report.get("currency", "USD")
    sev_risk = {"high": "high", "medium": "medium", "low": "low"}
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    for it in report.get("items", []) or []:
        sid = str(it.get("subscription_id") or "").lower()
        if sub_filter and sid and sid not in sub_filter:
            continue
        risk = sev_risk.get(str(it.get("severity", "low")).lower(), "low")
        monthly = it.get("monthly_cost") or 0
        cost_txt = f" · ~{monthly:.0f} {currency}/mo" if monthly else ""
        norm = {
            "time": hit.get("fetched_at", ""),
            "workload": ", ".join(it.get("workloads") or []) or (wl or {}).get("name", ""),
            "change": f"{it.get('reason') or it.get('category_label') or 'Cleanup candidate'}{cost_txt}",
            "risk": risk, "risk_rank": _rank(risk),
            "category": "cost",
            "operation": it.get("category", "") or "cleanup",
            "actor": "n/a",
            "resource": it.get("name", ""),
            "resource_type": it.get("type", ""),
            "_id": str(it.get("id") or it.get("name", "")),
        }
        if not _passes_risk(norm, filters):
            continue
        codes = ["idle_or_orphaned"]
        norm["flags"] = codes
        flag_codes.update(codes)
        events.append(norm)
    total_cost = report.get("total_monthly_cost") or 0
    note = f"~{total_cost:.0f} {currency}/mo potential savings" if total_cost else ""
    if not report.get("cost_available"):
        note = (note + "; cost data not loaded").strip("; ")
    return _mk(events, source="cost", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- rbac
async def _gather_rbac(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                       filters: dict[str, Any]) -> dict[str, Any]:
    from app.rbac.compose import build_master_rows, compute_overview

    overview = compute_overview(tenant_id)
    if overview.get("never_loaded"):
        return _unavailable("rbac", "RBAC not loaded — open RBAC and refresh a scope.")
    rows = build_master_rows(tenant_id)

    wl, conn, scope_kind, scope_id, force_demo = _resolve_target(scope)
    mode = (scope or {}).get("mode", "workload")
    if mode == "subscription":
        sub_filter: set[str] | None = {str(scope_id).lower()}
    elif mode in ("workload", "workload_dependencies"):
        sub_filter = _subscription_ids(wl) or None
    else:
        sub_filter = None

    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    for r in rows:
        privileged = bool(r.get("roleIsPrivileged"))
        data_plane = bool(r.get("roleHasDataActions"))
        eligible = str(r.get("assignmentState", "")).lower() == "eligible"
        role = str(r.get("roleName", ""))
        is_owner = role.strip().lower() == "owner"
        if not (privileged or data_plane or eligible or is_owner):
            continue
        sid = str(r.get("subscriptionId") or "").lower()
        if sub_filter is not None and sid and sid not in sub_filter:
            continue
        risk = "high" if (privileged and (data_plane or is_owner)) else "medium" if privileged else "low"
        principal = r.get("principalDisplayName") or r.get("effectivePrincipalName") or "unknown"
        norm = {
            "time": overview.get("generated_at", ""),
            "workload": r.get("subscriptionName") or r.get("scopeDisplayName") or "",
            "change": f"{'Eligible ' if eligible else ''}{role} on {r.get('scopeDisplayName') or r.get('scope', '')}",
            "risk": risk, "risk_rank": _rank(risk),
            "category": "access",
            "operation": r.get("accessPath", "") or "assignment",
            "actor": principal,
            "resource": principal,
            "resource_type": str(r.get("effectivePrincipalType", "") or "principal"),
            "_id": f"{r.get('surface','')}|{principal}|{role}|{r.get('scope','')}|{'E' if eligible else 'A'}",
        }
        if not _passes_risk(norm, filters):
            continue
        codes: list[str] = []
        if privileged:
            codes.append("rbac_grant")
        if is_owner:
            codes.append("owner_grant")
        if eligible:
            codes.append("eligible_grant")
        norm["flags"] = codes
        flag_codes.update(codes)
        events.append(norm)
        if len(events) >= 500:
            break
    kpis = overview.get("kpis", {})
    note = (f"{kpis.get('privileged', 0)} privileged · {kpis.get('eligible', 0)} eligible"
            if kpis else "")
    if overview.get("demo"):
        note = (note + "; demo data").strip("; ")
    return _mk(events, source="rbac", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- assessments
async def _gather_assessments(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                              filters: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import AssessmentRun

    wl, conn, scope_kind, scope_id, force_demo = _resolve_target(scope)
    if scope_kind != "workload" or not scope_id:
        return _unavailable("assessments", "Assessment insights need a workload scope.")
    async with SessionLocal() as db:
        run = (
            await db.execute(
                select(AssessmentRun).where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == scope_id,
                    AssessmentRun.status == "succeeded",
                ).order_by(AssessmentRun.started_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if run is None:
        return _unavailable("assessments", "No completed assessment for this workload yet.")

    sev_risk = {"critical": "critical", "error": "high", "high": "high",
                "warning": "medium", "info": "low"}
    ended = run.ended_at.isoformat() if run.ended_at else ""
    wl_name = run.workload_name or (wl or {}).get("name", "")
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    for f in run.findings_json or []:
        if str(f.get("status", "")).lower() != "fail":
            continue
        risk = sev_risk.get(str(f.get("severity", "info")).lower(), "low")
        norm = {
            "time": ended,
            "workload": wl_name,
            "change": f"{f.get('title', '')} — {f.get('flagged_count', 0)} resource(s)",
            "risk": risk, "risk_rank": _rank(risk),
            "category": str(f.get("pillar", "") or "assessment"),
            "operation": "finding",
            "actor": "assessment",
            "resource": (f.get("flagged_resources") or [{}])[0].get("name", "") or f.get("check_id", ""),
            "resource_type": ", ".join(f.get("resource_types") or []) or "check",
            "_id": str(f.get("check_id", "")),
        }
        if not _passes_risk(norm, filters):
            continue
        codes: list[str] = []
        if risk == "critical":
            codes.append("assessment_critical")
        norm["flags"] = codes
        flag_codes.update(codes)
        events.append(norm)
    score = run.overall_score
    note = f"Latest score {score}/100" if score is not None else f"Run {ended[:10]}"
    return _mk(events, source="assessments", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- backup & DR
async def _gather_backup(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                         filters: dict[str, Any]) -> dict[str, Any]:
    from app.backupdr import cache as bk_cache
    from app.backupdr import demo as bk_demo

    wl, conn, scope_kind, scope_id, force_demo = _resolve_target(scope)
    if not scope_id:
        return _unavailable("backup", "Scope has no workload/subscription to read backup for.")
    snap = bk_cache.read_snapshot(tenant_id, scope_kind, scope_id)
    if snap is None and (force_demo or bk_demo.is_demo_scope(scope_kind, scope_id)):
        try:
            snap = bk_demo.seed_demo(tenant_id=tenant_id, scope_id=scope_id)
        except TypeError:
            snap = bk_demo.seed_demo()
    if not snap:
        return _unavailable("backup", "No backup snapshot yet — open Backup & DR to scan this scope.")

    tier_risk = {"red": "high", "amber": "medium", "green": "low"}
    scope_name = snap.get("scope_name", "") or (wl or {}).get("name", "")
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    for grp in snap.get("groups", []) or []:
        tier = str(grp.get("tier", "green")).lower()
        if tier == "green":
            continue
        risk = tier_risk.get(tier, "low")
        for it in grp.get("items", []) or []:
            norm = {
                "time": snap.get("generated_at", ""),
                "workload": scope_name,
                "change": ("Not protected" if not it.get("backup_enabled")
                           else f"At risk (last job: {it.get('last_job_status', 'unknown')})"),
                "risk": risk, "risk_rank": _rank(risk),
                "category": "backup",
                "operation": "coverage",
                "actor": "n/a",
                "resource": it.get("name", ""),
                "resource_type": it.get("type", ""),
                "_id": str(it.get("id") or it.get("name", "")),
            }
            if not _passes_risk(norm, filters):
                continue
            codes = ["backup_unprotected"] if not it.get("backup_enabled") else []
            norm["flags"] = codes
            flag_codes.update(codes)
            events.append(norm)
    for dr in snap.get("dr_pairs", []) or []:
        if str(dr.get("replication_health", "")).lower() in ("healthy", ""):
            continue
        norm = {
            "time": snap.get("generated_at", ""),
            "workload": scope_name,
            "change": f"DR pair unhealthy ({dr.get('replication_health', '')})",
            "risk": "high", "risk_rank": _rank("high"),
            "category": "dr", "operation": "replication", "actor": "n/a",
            "resource": dr.get("name", ""), "resource_type": "ASR pair",
            "_id": f"dr:{dr.get('name', '')}",
            "flags": ["dr_unhealthy"],
        }
        if _passes_risk(norm, filters):
            flag_codes.add("dr_unhealthy")
            events.append(norm)
    sc = snap.get("scorecard", {}) or {}
    note = (f"{sc.get('pct_protected', 0)}% protected" if sc else "")
    if snap.get("demo"):
        note = (note + "; demo data").strip("; ")
    return _mk(events, source="backup", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- identity
async def _gather_identity(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                           filters: dict[str, Any]) -> dict[str, Any]:
    from app.identity import cache as id_cache

    snap = None
    for days in (30, 60, 90):
        snap = id_cache.read_snapshot(tenant_id, days)
        if snap:
            break
    if not snap:
        return _unavailable("identity", "No identity snapshot yet — open Identity and refresh.")

    groups = snap.get("groups", {}) or {}
    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()

    def _cred_risk(du: Any) -> str:
        try:
            d = int(du)
        except (TypeError, ValueError):
            return "medium"
        return "high" if d <= 7 else "medium" if d <= 30 else "low"

    for c in (groups.get("expiring_credentials") or []) + (groups.get("keyvault_expiry") or []):
        du = c.get("days_until")
        risk = _cred_risk(du)
        norm = {
            "time": c.get("expires_at", ""),
            "workload": c.get("workload_name", "") or c.get("owner", ""),
            "change": f"{c.get('kind', 'credential')} expires in {du}d",
            "risk": risk, "risk_rank": _rank(risk),
            "category": "identity", "operation": "credential_expiry",
            "actor": c.get("owner", "") or "unknown",
            "resource": c.get("name", ""), "resource_type": str(c.get("kind", "credential")),
            "_id": f"cred:{c.get('id', '')}:{c.get('name', '')}",
        }
        if not _passes_risk(norm, filters):
            continue
        norm["flags"] = ["cred_expiring"]
        flag_codes.add("cred_expiring")
        events.append(norm)

    for u in groups.get("users_without_mfa") or []:
        norm = {
            "time": snap.get("generated_at", ""),
            "workload": "Entra ID",
            "change": f"Privileged user without MFA: {', '.join(u.get('admin_roles') or []) or 'admin'}",
            "risk": "high", "risk_rank": _rank("high"),
            "category": "identity", "operation": "mfa_gap",
            "actor": u.get("user_principal_name", "") or "unknown",
            "resource": u.get("display_name", "") or u.get("user_principal_name", ""),
            "resource_type": "user",
            "_id": f"mfa:{u.get('id', '')}",
            "flags": ["mfa_gap"],
        }
        if _passes_risk(norm, filters):
            flag_codes.add("mfa_gap")
            events.append(norm)

    for a in groups.get("ownerless_apps") or []:
        norm = {
            "time": snap.get("generated_at", ""),
            "workload": "Entra ID",
            "change": "App registration has no owner",
            "risk": "medium", "risk_rank": _rank("medium"),
            "category": "identity", "operation": "ownerless_app",
            "actor": "n/a",
            "resource": a.get("name", ""), "resource_type": "app registration",
            "_id": f"app:{a.get('id', '')}",
            "flags": ["ownerless_app"],
        }
        if _passes_risk(norm, filters):
            flag_codes.add("ownerless_app")
            events.append(norm)

    kpis = snap.get("kpis", {}) or {}
    note = (f"{kpis.get('expiring_secrets', 0)} secrets · {kpis.get('expiring_certs', 0)} certs · "
            f"{kpis.get('users_without_mfa', 0)} no-MFA" if kpis else "")
    return _mk(events, source="identity", note=note, flag_codes=flag_codes)


# --------------------------------------------------------------------------- policy
async def _gather_policy(scope: dict[str, Any], *, tenant_id: str, lookback_hours: int,
                         filters: dict[str, Any]) -> dict[str, Any]:
    from app.policy import registry as policy_registry

    snap = policy_registry.latest_snapshot(tenant_id)
    if not snap:
        return _unavailable("policy", "No policy snapshot yet — open Policy to take one.")
    summary = snap.get("summary", {}) or {}
    counts = summary.get("counts", {}) or {}
    compliance = summary.get("compliance", {}) or {}
    created = snap.get("created_at", "")

    events: list[dict[str, Any]] = []
    flag_codes: set[str] = set()
    non_compliant = int(compliance.get("non_compliant", 0) or 0)
    if non_compliant:
        risk = "high" if non_compliant >= 25 else "medium"
        events.append({
            "time": created, "workload": "Policy",
            "change": f"{non_compliant} non-compliant resource(s) across the tenant",
            "risk": risk, "risk_rank": _rank(risk),
            "category": "policy", "operation": "compliance", "actor": "azure policy",
            "resource": "Tenant compliance", "resource_type": "compliance",
            "_id": "policy:non_compliant", "flags": ["non_compliant"],
        })
        flag_codes.add("non_compliant")
    exemptions = int(counts.get("exemptions", 0) or 0)
    if exemptions:
        events.append({
            "time": created, "workload": "Policy",
            "change": f"{exemptions} policy exemption(s) active",
            "risk": "medium", "risk_rank": _rank("medium"),
            "category": "policy", "operation": "exemption", "actor": "azure policy",
            "resource": "Policy exemptions", "resource_type": "exemption",
            "_id": "policy:exemptions", "flags": ["policy_exemption"],
        })
        flag_codes.add("policy_exemption")
    events = [e for e in events if _passes_risk(e, filters)]
    note = f"{counts.get('assignments', 0)} assignments · {counts.get('definitions', 0)} definitions"
    return _mk(events, source="policy", note=note, flag_codes=flag_codes)

