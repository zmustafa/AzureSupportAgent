"""AMBA Monitoring Coverage computation.

Detects, per resource, which recommended baseline alerts are present (✓), missing (✗),
or misconfigured (⚠ — disabled, no action group, or threshold outside tolerance). Runs
entirely on the read-only Azure Resource Graph path: both the resource universe AND the
alert rules / action groups are ARM resources queryable via KQL, so no gated
command-execution or data-plane access is needed.

``compute_coverage`` accepts optional pre-fetched ``resources`` and ``alerts`` lists; when
omitted it queries Azure Resource Graph. The injection path makes the logic unit-testable
and powers the demo/dummy-data seed (demo.py)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.amba.reference import load_reference
from app.core.coverage_resources import build_all_resources

log = logging.getLogger("app.amba.collector")

STATUS_PRESENT = "present"
STATUS_MISCONFIGURED = "misconfigured"
STATUS_MISSING = "missing"

_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


# --------------------------------------------------------------------------- alert index
def _alert_metric_names(props: dict[str, Any]) -> set[str]:
    """Metric names referenced by a metric alert's criteria (lowercased)."""
    names: set[str] = set()
    crit = props.get("criteria") or {}
    if isinstance(crit, dict):
        for clause in (crit.get("allOf") or []):
            if isinstance(clause, dict):
                mn = clause.get("metricName") or clause.get("metricname")
                if mn:
                    names.add(str(mn).lower())
    return names


def _alert_has_action_group(props: dict[str, Any]) -> bool:
    """True when the rule wires at least one action group."""
    actions = props.get("actions")
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and (a.get("actionGroupId") or a.get("actiongroupid")):
                return True
            if isinstance(a, str) and a:
                return True
    if isinstance(actions, dict):
        ags = actions.get("actionGroups") or actions.get("actiongroups")
        if isinstance(ags, list) and ags:
            return True
    return False


def _alert_thresholds(props: dict[str, Any]) -> list[float]:
    """Numeric thresholds declared in a metric alert's criteria."""
    out: list[float] = []
    crit = props.get("criteria") or {}
    if isinstance(crit, dict):
        for clause in (crit.get("allOf") or []):
            if isinstance(clause, dict) and clause.get("threshold") is not None:
                try:
                    out.append(float(clause["threshold"]))
                except (TypeError, ValueError):
                    pass
    return out


def _index_alerts(alerts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map a lowercased target resource id → list of normalized alert descriptors."""
    index: dict[str, list[dict[str, Any]]] = {}
    for raw in alerts:
        props = raw.get("properties") or {}
        if not isinstance(props, dict):
            continue
        scopes = props.get("scopes") or []
        if isinstance(scopes, str):
            scopes = [scopes]
        enabled = props.get("enabled")
        enabled = True if enabled is None else bool(enabled)
        descriptor = {
            "id": raw.get("id", ""),
            "name": raw.get("name", ""),
            "type": str(raw.get("type", "")).lower(),
            "enabled": enabled,
            "metric_names": _alert_metric_names(props),
            "thresholds": _alert_thresholds(props),
            "has_action_group": _alert_has_action_group(props),
        }
        for sc in scopes:
            if sc:
                index.setdefault(str(sc).lower(), []).append(descriptor)
    return index


def _match_status(
    rec: dict[str, Any], targeting: list[dict[str, Any]], tolerance_pct: float
) -> tuple[str, dict[str, Any]]:
    """Classify a recommended alert against the rules targeting a resource.

    Returns (status, observed) where observed carries the matched rule's facts for the
    UI drawer (name, enabled, action group, observed threshold)."""
    metric = (rec.get("metric") or "").lower()
    # Candidate rules: those referencing the recommended metric (metric signal), else any.
    if metric:
        candidates = [a for a in targeting if metric in a["metric_names"]]
    else:
        candidates = list(targeting)

    if not candidates:
        return STATUS_MISSING, {}

    # Prefer an enabled, action-group-wired candidate for the "best" observed state.
    best = None
    for a in candidates:
        if a["enabled"] and a["has_action_group"]:
            best = a
            break
    best = best or candidates[0]

    observed = {
        "rule_id": best["id"],
        "rule_name": best["name"],
        "enabled": best["enabled"],
        "has_action_group": best["has_action_group"],
        "observed_thresholds": best["thresholds"],
    }

    issues: list[str] = []
    if not best["enabled"]:
        issues.append("disabled")
    if rec.get("requires_action_group", True) and not best["has_action_group"]:
        issues.append("no action group")
    rec_threshold = rec.get("threshold")
    if rec_threshold is not None and best["thresholds"]:
        tol = abs(float(rec_threshold)) * (tolerance_pct / 100.0)
        within = any(abs(t - float(rec_threshold)) <= tol for t in best["thresholds"])
        if not within:
            issues.append("threshold differs from baseline")
    observed["issues"] = issues
    return (STATUS_MISCONFIGURED if issues else STATUS_PRESENT), observed


# --------------------------------------------------------------------------- ARG queries
async def _query_resources(predicate: str, connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.exec.command_runner import run_kql_capture

    kql = (
        f"Resources | where {predicate} "
        "| project id, name, type, resourceGroup, subscriptionId, location, tags "
        "| order by type asc, name asc | take 1000"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        raise RuntimeError(cap.error or "Resource query failed.")
    return _parse_rows(cap.stdout)


async def _query_alerts(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """All alert rules across the in-scope subscriptions (with full properties)."""
    from app.exec.command_runner import run_kql_capture

    if not subscriptions:
        return []
    joined = ", ".join(f"'{_esc(s)}'" for s in subscriptions)
    kql = (
        "resources "
        "| where type in~ ('microsoft.insights/metricalerts', "
        "'microsoft.insights/scheduledqueryrules', 'microsoft.insights/activitylogalerts') "
        f"| where subscriptionId in~ ({joined}) "
        "| project id, name, type, properties | take 2000"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        raise RuntimeError(cap.error or "Alert-rule query failed.")
    return _parse_rows(cap.stdout)


# --------------------------------------------------------------------------- public API
def compute_coverage(
    resources: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    misconfig_counts_as_gap: bool,
    tolerance_pct: float,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure coverage computation over already-fetched resources + alert rules.

    ``resources``: [{id,name,type,resourceGroup,subscriptionId,location,tags}]
    ``alerts``: ARG rows [{id,name,type,properties}].
    Returns the full snapshot (kpis, per-type groups, rows with per-alert cells, gaps)."""
    ref = reference if reference is not None else load_reference()
    ref_types: dict[str, Any] = ref.get("types", {})
    alert_index = _index_alerts(alerts)

    groups: dict[str, dict[str, Any]] = {}
    total_present = total_missing = total_misconfig = 0
    covered_units = 0.0
    total_units = 0
    gaps: list[dict[str, Any]] = []

    for res in resources:
        rtype = str(res.get("type", "")).lower()
        spec = ref_types.get(rtype)
        if not spec:
            continue  # type not in the baseline reference — not scored
        rid = str(res.get("id", "")).lower()
        targeting = alert_index.get(rid, [])
        rec_alerts = spec.get("alerts", []) or []

        cells: list[dict[str, Any]] = []
        for rec in rec_alerts:
            status, observed = _match_status(rec, targeting, tolerance_pct)
            total_units += 1
            if status == STATUS_PRESENT:
                total_present += 1
                covered_units += 1.0
            elif status == STATUS_MISCONFIGURED:
                total_misconfig += 1
                covered_units += 0.0 if misconfig_counts_as_gap else 0.5
            else:
                total_missing += 1

            cell = {
                "alert_key": rec["key"],
                "alert_name": rec["name"],
                "amba_category": rec["amba_category"],
                "severity": rec["severity"],
                "status": status,
                "recommended": {
                    "metric": rec.get("metric", ""),
                    "operator": rec.get("operator", ""),
                    "threshold": rec.get("threshold"),
                    "unit": rec.get("unit", ""),
                    "window": rec.get("window", ""),
                    "requires_action_group": rec.get("requires_action_group", True),
                },
                "observed": observed,
                "why": rec.get("why", ""),
            }
            cells.append(cell)
            if status != STATUS_PRESENT:
                gaps.append(
                    {
                        "resource_id": res.get("id", ""),
                        "resource_name": res.get("name", ""),
                        "resource_type": rtype,
                        "resource_group": res.get("resourceGroup", ""),
                        "subscription_id": res.get("subscriptionId", ""),
                        "location": res.get("location", ""),
                        "alert_key": rec["key"],
                        "alert_name": rec["name"],
                        "amba_category": rec["amba_category"],
                        "severity": rec["severity"],
                        "status": status,
                        "recommended": cell["recommended"],
                        "observed": observed,
                        "why": rec.get("why", ""),
                    }
                )

        g = groups.setdefault(
            rtype,
            {
                "resource_type": rtype,
                "display": spec.get("display", rtype),
                "category": spec.get("category", "other"),
                "recommended_alerts": [
                    {"key": a["key"], "name": a["name"], "amba_category": a["amba_category"], "severity": a["severity"]}
                    for a in rec_alerts
                ],
                "rows": [],
                "present": 0,
                "missing": 0,
                "misconfigured": 0,
            },
        )
        row_present = sum(1 for c in cells if c["status"] == STATUS_PRESENT)
        row_missing = sum(1 for c in cells if c["status"] == STATUS_MISSING)
        row_misconfig = sum(1 for c in cells if c["status"] == STATUS_MISCONFIGURED)
        g["present"] += row_present
        g["missing"] += row_missing
        g["misconfigured"] += row_misconfig
        g["rows"].append(
            {
                "resource_id": res.get("id", ""),
                "resource_name": res.get("name", ""),
                "resource_group": res.get("resourceGroup", ""),
                "subscription_id": res.get("subscriptionId", ""),
                "location": res.get("location", ""),
                "tags": res.get("tags") or {},
                "cells": cells,
            }
        )

    def _grp_pct(g: dict[str, Any]) -> int:
        denom = g["present"] + g["missing"] + g["misconfigured"]
        if denom == 0:
            return 100
        cov = g["present"] + (0.0 if misconfig_counts_as_gap else 0.5) * g["misconfigured"]
        return round(100 * cov / denom)

    group_list = sorted(groups.values(), key=lambda g: g["display"].lower())
    for g in group_list:
        g["coverage_pct"] = _grp_pct(g)

    coverage_pct = round(100 * covered_units / total_units) if total_units else 100
    gaps.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 3), x["resource_type"], x["resource_name"]))

    return {
        "generated_at": _now_iso(),
        "coverage_pct": coverage_pct,
        "kpis": {
            "total_resources_in_baseline": sum(len(g["rows"]) for g in group_list),
            "alerts_present": total_present,
            "alerts_missing": total_missing,
            "alerts_misconfigured": total_misconfig,
            "recommended_total": total_units,
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
    misconfig_counts_as_gap: bool,
    tolerance_pct: float,
) -> dict[str, Any]:
    """Resolve the scope, query ARG for resources + alert rules, and compute coverage."""
    from app.assessments.runner import _resolve_scope  # reuse the proven scope resolver

    subscriptions: list[str] = []
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

    try:
        resources = await _query_resources(predicate, connection)
        # Alert rules can live in any RG within the in-scope subscriptions; collect by sub.
        sub_guids = subscriptions or sorted(
            {str(r.get("subscriptionId", "")) for r in resources if r.get("subscriptionId")}
        )
        alerts = await _query_alerts(sub_guids, connection)
    except RuntimeError as exc:
        return _empty_snapshot(scope_kind, scope_id, error=str(exc)[:300])

    snap = compute_coverage(
        resources,
        alerts,
        misconfig_counts_as_gap=misconfig_counts_as_gap,
        tolerance_pct=tolerance_pct,
    )
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph",
            "demo": False,
            "error": "",
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
        "source": "azure_resource_graph",
        "demo": False,
        "coverage_pct": 0,
        "kpis": {
            "total_resources_in_baseline": 0,
            "alerts_present": 0,
            "alerts_missing": 0,
            "alerts_misconfigured": 0,
            "recommended_total": 0,
        },
        "groups": [],
        "gaps": [],
        "all_resources": [],
        "error": error,
    }
