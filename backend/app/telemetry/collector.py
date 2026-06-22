"""Telemetry Coverage computation.

For each in-scope resource, audits its Azure Monitor diagnostic settings against the
recommended-category reference:
    red     = no diagnostic settings at all
    amber   = present but enabled categories ⊊ recommended, OR ships to a non-approved
              (drift) destination, OR an audit/security category is off
    green   = compliant (all recommended categories enabled + approved destination)

Diagnostic settings are NOT reliably in the Resource Graph Resources table (they're
per-resource extension resources), so they're read one resource at a time — preferring the
Azure MCP monitor tool, falling back to ``az monitor diagnostic-settings list`` (which is
gated by command_execution_enabled). The resource universe + workspace list still come from
Resource Graph. ``compute_coverage`` is a pure function over injected data so it's unit-
testable and powers the demo seed."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.telemetry.reference import load_reference
from app.core.coverage_resources import build_all_resources

log = logging.getLogger("app.telemetry.collector")

STATUS_NONE = "none"           # red — no diagnostic settings
STATUS_PARTIAL = "partial"     # amber — missing categories / drift / audit off
STATUS_COMPLIANT = "compliant" # green

_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    from app.exec.command_runner import parse_kql_rows
    return parse_kql_rows(stdout)


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _norm_workspace(ws: str) -> str:
    return (ws or "").strip().lower()


# --------------------------------------------------------------------------- diag parsing
def _enabled_categories(setting: dict[str, Any]) -> set[str]:
    """Enabled log + metric categories declared on one diagnostic setting."""
    props = setting.get("properties") if isinstance(setting.get("properties"), dict) else setting
    out: set[str] = set()
    for arr_key in ("logs", "metrics"):
        for entry in (props.get(arr_key) or []):
            if isinstance(entry, dict) and entry.get("enabled"):
                cat = entry.get("category") or entry.get("categoryGroup")
                if cat:
                    out.add(str(cat))
                # A categoryGroup of "allLogs"/"audit" implies a bundle; record a marker.
                grp = entry.get("categoryGroup")
                if grp:
                    out.add(f"group:{grp}")
    return out


def _destinations(setting: dict[str, Any]) -> dict[str, Any]:
    props = setting.get("properties") if isinstance(setting.get("properties"), dict) else setting
    return {
        "workspace_id": props.get("workspaceId") or "",
        "storage_account_id": props.get("storageAccountId") or "",
        "event_hub": props.get("eventHubName") or props.get("eventHubAuthorizationRuleId") or "",
        "retention_days": _max_retention(props),
    }


def _max_retention(props: dict[str, Any]) -> int:
    days = 0
    for arr_key in ("logs", "metrics"):
        for entry in (props.get(arr_key) or []):
            rp = entry.get("retentionPolicy") if isinstance(entry, dict) else None
            if isinstance(rp, dict) and rp.get("enabled"):
                try:
                    days = max(days, int(rp.get("days", 0)))
                except (TypeError, ValueError):
                    pass
    return days


# --------------------------------------------------------------------------- ARG queries
async def _query_resources(predicates: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.assessments.runner import query_resources_batched

    return await query_resources_batched(
        predicates,
        connection,
        projection="id, name, type, resourceGroup, subscriptionId, location, tags",
    )


async def list_workspaces(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """All Log Analytics workspaces in the tenant (for the approved-workspace picker)."""
    from app.exec.command_runner import run_kql_capture

    kql = (
        "resources | where type =~ 'microsoft.operationalinsights/workspaces' "
        "| project id, name, resourceGroup, subscriptionId, location | order by name asc | take 500"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    return _parse_rows(cap.stdout) if cap.ok else []


async def _diag_settings_for(resource_id: str, connection: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Read diagnostic settings for one resource. Returns [] if none, None if unreadable.

    Prefers the gated ``az monitor diagnostic-settings list`` path; callers handle a None
    (unreadable) by surfacing a degradation notice."""
    from app.exec.command_runner import run_az_json_capture

    res = await run_az_json_capture(
        ["monitor", "diagnostic-settings", "list", "--resource", resource_id, "-o", "json"],
        connection,
        label="az monitor diagnostic-settings list",
    )
    if not res.ok:
        return None
    parsed = _parse_rows(res.stdout)
    return parsed


# --------------------------------------------------------------------------- classification
def classify_resource(
    res: dict[str, Any],
    settings: list[dict[str, Any]],
    rec_categories: list[dict[str, Any]],
    approved_workspaces: set[str],
) -> dict[str, Any]:
    """Classify one resource's telemetry coverage into a normalized row."""
    recommended_keys = [c["key"] for c in rec_categories]
    audit_keys = {c["key"] for c in rec_categories if c.get("group") in ("audit", "security")}

    enabled: set[str] = set()
    destinations: list[dict[str, Any]] = []
    for s in settings:
        enabled |= _enabled_categories(s)
        destinations.append(_destinations(s))

    # Treat a categoryGroup of allLogs/audit as covering all (or audit) recommended logs.
    has_all_logs = "group:allLogs" in enabled or "group:all" in enabled
    if has_all_logs:
        enabled |= {k for k in recommended_keys}

    missing = [c for c in rec_categories if c["key"] not in enabled]
    missing_keys = [c["key"] for c in missing]
    missing_audit = [c for c in missing if c["key"] in audit_keys]

    # Destination drift: any non-empty workspace not on the approved list (when an
    # approved list is configured).
    dest_workspaces = [_norm_workspace(d["workspace_id"]) for d in destinations if d["workspace_id"]]
    drift = []
    if approved_workspaces:
        drift = [w for w in dest_workspaces if w not in approved_workspaces]

    if not settings:
        status = STATUS_NONE
    elif missing or drift:
        status = STATUS_PARTIAL
    else:
        status = STATUS_COMPLIANT

    return {
        "resource_id": res.get("id", ""),
        "resource_name": res.get("name", ""),
        "resource_type": str(res.get("type", "")).lower(),
        "resource_group": res.get("resourceGroup", ""),
        "subscription_id": res.get("subscriptionId", ""),
        "location": res.get("location", ""),
        "tags": res.get("tags") or {},
        "status": status,
        "settings_count": len(settings),
        "enabled_categories": sorted(c for c in enabled if not c.startswith("group:")),
        "recommended_categories": recommended_keys,
        "missing_categories": missing_keys,
        "missing_audit_categories": [c["key"] for c in missing_audit],
        "destinations": destinations,
        "drift_workspaces": drift,
        "has_drift": bool(drift),
    }


def _gap_severity(row: dict[str, Any]) -> str:
    if row["status"] == STATUS_NONE:
        return "error"
    if row["missing_audit_categories"] or row["has_drift"]:
        return "warning"
    return "info"


# --------------------------------------------------------------------------- public API
def compute_coverage(
    resources: list[dict[str, Any]],
    diag_by_resource: dict[str, list[dict[str, Any]]],
    *,
    approved_workspaces: list[str],
    reference: dict[str, Any] | None = None,
    unreadable: set[str] | None = None,
) -> dict[str, Any]:
    """Pure coverage computation over fetched resources + their diagnostic settings.

    ``diag_by_resource`` maps lowercased resource id → list of diagnostic settings.
    ``unreadable`` is the set of resource ids whose settings couldn't be read (degraded)."""
    ref = reference if reference is not None else load_reference()
    ref_types: dict[str, Any] = ref.get("types", {})
    approved = {_norm_workspace(w) for w in (approved_workspaces or []) if w}
    unreadable = unreadable or set()

    groups: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    n_total = n_with_any = n_all_cats = n_to_approved = n_unknown_dest = 0
    n_unreadable = 0

    for res in resources:
        rtype = str(res.get("type", "")).lower()
        spec = ref_types.get(rtype)
        if not spec:
            continue
        rid = str(res.get("id", "")).lower()
        if rid in unreadable:
            n_unreadable += 1
        settings = diag_by_resource.get(rid, [])
        rec_categories = spec.get("categories", []) or []
        row = classify_resource(res, settings, rec_categories, approved)

        n_total += 1
        if row["settings_count"] > 0:
            n_with_any += 1
        if not row["missing_categories"]:
            n_all_cats += 1
        dest_ws = [d for d in row["destinations"] if d["workspace_id"]]
        if approved and dest_ws:
            if all(_norm_workspace(d["workspace_id"]) in approved for d in dest_ws):
                n_to_approved += 1
        if row["has_drift"]:
            n_unknown_dest += 1

        g = groups.setdefault(
            rtype,
            {
                "resource_type": rtype,
                "display": spec.get("display", rtype),
                "note": spec.get("note", ""),
                "recommended_categories": rec_categories,
                "rows": [],
                "none": 0,
                "partial": 0,
                "compliant": 0,
            },
        )
        g["rows"].append(row)
        g[row["status"]] += 1

        if row["status"] != STATUS_COMPLIANT:
            gaps.append(
                {
                    "resource_id": row["resource_id"],
                    "resource_name": row["resource_name"],
                    "resource_type": rtype,
                    "resource_group": row["resource_group"],
                    "subscription_id": row["subscription_id"],
                    "location": row["location"],
                    "status": row["status"],
                    "missing_categories": row["missing_categories"],
                    "missing_audit_categories": row["missing_audit_categories"],
                    "has_drift": row["has_drift"],
                    "drift_workspaces": row["drift_workspaces"],
                    "severity": _gap_severity(row),
                }
            )

    def _pct(num: int, denom: int) -> int:
        return round(100 * num / denom) if denom else 100

    group_list = sorted(groups.values(), key=lambda g: g["display"].lower())
    for g in group_list:
        denom = len(g["rows"])
        g["coverage_pct"] = _pct(g["compliant"], denom)

    gaps.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 3), x["resource_type"], x["resource_name"]))

    return {
        "generated_at": _now_iso(),
        "coverage_pct": _pct(n_all_cats, n_total),
        "kpis": {
            "total_resources_in_reference": n_total,
            "with_any_diag": n_with_any,
            "pct_with_any_diag": _pct(n_with_any, n_total),
            "with_all_categories": n_all_cats,
            "pct_with_all_categories": _pct(n_all_cats, n_total),
            "to_approved_workspace": n_to_approved,
            "pct_to_approved": _pct(n_to_approved, n_total),
            "unknown_destinations": n_unknown_dest,
            "unreadable": n_unreadable,
        },
        "groups": group_list,
        "gaps": gaps,
        "all_resources": build_all_resources(resources, ref_types),
    }


async def collect_coverage(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    approved_workspaces: list[str],
    scan_cap: int,
) -> dict[str, Any]:
    """Resolve scope, query ARG for resources, fan out per-resource diag-settings, compute."""
    from app.assessments.runner import _resolve_scope, scope_predicate_batches

    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        if scope.get("error") and not predicate:
            return _empty_snapshot(scope_kind, scope_id, error=scope["error"])
        predicates = scope_predicate_batches(scope)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{_esc(scope_id)}'"]
    else:
        return _empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    try:
        resources = await _query_resources(predicates, connection)
    except RuntimeError as exc:
        return _empty_snapshot(scope_kind, scope_id, error=str(exc)[:300])

    # Only resources whose type is in the reference need a diag-settings read.
    ref_types = load_reference().get("types", {})
    targets = [r for r in resources if str(r.get("type", "")).lower() in ref_types][:scan_cap]

    diag_by_resource: dict[str, list[dict[str, Any]]] = {}
    unreadable: set[str] = set()
    sem = asyncio.Semaphore(6)

    async def _one(res: dict[str, Any]) -> None:
        rid = res.get("id", "")
        async with sem:
            settings = await _diag_settings_for(rid, connection)
        if settings is None:
            unreadable.add(rid.lower())
            diag_by_resource[rid.lower()] = []
        else:
            diag_by_resource[rid.lower()] = settings

    await asyncio.gather(*[_one(r) for r in targets])

    snap = compute_coverage(
        resources, diag_by_resource, approved_workspaces=approved_workspaces, unreadable=unreadable
    )
    note = ""
    if unreadable:
        note = (
            f"{len(unreadable)} resource(s) had unreadable diagnostic settings — enable command "
            "execution (Admin → General) or grant the connection Monitoring Reader."
        )
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph+monitor",
            "demo": False,
            "error": note,
        }
    )
    return snap


def _empty_snapshot(scope_kind: str, scope_id: str, *, error: str) -> dict[str, Any]:
    return {
        "generated_at": _now_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_id,
        "connection_configured": False,
        "source": "azure_resource_graph+monitor",
        "demo": False,
        "coverage_pct": 0,
        "kpis": {
            "total_resources_in_reference": 0,
            "with_any_diag": 0,
            "pct_with_any_diag": 100,
            "with_all_categories": 0,
            "pct_with_all_categories": 100,
            "to_approved_workspace": 0,
            "pct_to_approved": 100,
            "unknown_destinations": 0,
            "unreadable": 0,
        },
        "groups": [],
        "gaps": [],
        "all_resources": [],
        "error": error,
    }
