"""Synthetic AMBA Monitoring Coverage data for review/demo without a live Azure tenant.

Drives the demo from the shared per-workload catalog (``app.demo_catalog``) so each demo
workload (Contoso Hotels, Zava Shoes Website, Zava Shoes CRM) gets its *own* resources and
a believable coverage spread derived from each resource's health tier:
    green → all baseline alerts present
    amber → one alert misconfigured (disabled), the rest present
    red   → no alerts at all (every recommended alert missing)

The API treats every demo workload specially: it serves/regenerates this dummy data instead
of querying Azure, so the full UI (donut, matrix, drawer, gaps, IaC) is reviewable
end-to-end. Marked ``demo: True`` everywhere so it's clearly distinguishable."""
from __future__ import annotations

from typing import Any

from app.amba.collector import compute_coverage
from app.amba.reference import load_reference
from app.demo_catalog import (
    CONTOSO_ID,
    DEMO_SUB,
    DEMO_WORKLOAD_IDS,
    bucket,
    name_for,
    resources_for,
    rg_for,
)

DEMO_WORKLOAD_ID = CONTOSO_ID
DEMO_WORKLOAD_NAME = "Contoso Hotels"
# Backward-compatible module constants (Contoso defaults) used by sibling demo modules.
_SUB = DEMO_SUB
_RG = rg_for(CONTOSO_ID)


def demo_resources(scope_id: str = DEMO_WORKLOAD_ID) -> list[dict[str, Any]]:
    """This workload's resource universe (collector shape)."""
    return resources_for(scope_id)


def _baseline_metric_alerts(rtype: str) -> list[dict[str, Any]]:
    ref = load_reference()
    spec = ref.get("types", {}).get(rtype) or {}
    return [a for a in (spec.get("alerts") or []) if str(a.get("signal", "metric")) == "metric" and a.get("metric")]


def _metric_alert(
    rg: str, name: str, target_id: str, metric: str, threshold: float | None, *,
    enabled: bool = True, action_group: bool = True,
) -> dict[str, Any]:
    rid = f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.insights/metricalerts/{name}"
    ag_id = f"/subscriptions/{DEMO_SUB}/resourceGroups/{rg}/providers/microsoft.insights/actiongroups/oncall"
    clause: dict[str, Any] = {"name": "c1", "metricName": metric, "operator": "GreaterThan"}
    if threshold is not None:
        clause["threshold"] = threshold
    return {
        "id": rid,
        "name": name,
        "type": "microsoft.insights/metricAlerts",
        "properties": {
            "enabled": enabled,
            "scopes": [target_id],
            "actions": [{"actionGroupId": ag_id}] if action_group else [],
            "criteria": {"allOf": [clause]},
        },
    }


def demo_alerts(scope_id: str = DEMO_WORKLOAD_ID) -> list[dict[str, Any]]:
    """Synthesize alert rules so each resource lands on its tier's coverage state."""
    rg = rg_for(scope_id)
    out: list[dict[str, Any]] = []
    for res in resources_for(scope_id):
        tier = res["tier"]
        if tier == "red":
            continue  # no alerts → every recommended alert is MISSING
        alerts = _baseline_metric_alerts(res["type"])
        if not alerts:
            continue
        # amber → exactly one alert misconfigured (disabled); green → all clean.
        misconfig_idx = bucket(res["id"], len(alerts)) if tier == "amber" else -1
        short = res["name"].replace("/", "-")
        for i, a in enumerate(alerts):
            out.append(
                _metric_alert(
                    rg,
                    f"{short}-{a['key']}",
                    res["id"],
                    a["metric"],
                    a.get("threshold"),
                    enabled=(i != misconfig_idx),
                )
            )
    return out


def build_demo_snapshot(*, misconfig_counts_as_gap: bool, tolerance_pct: float,
                        scope_id: str = DEMO_WORKLOAD_ID, scope_name: str | None = None) -> dict[str, Any]:
    snap = compute_coverage(
        demo_resources(scope_id),
        demo_alerts(scope_id),
        misconfig_counts_as_gap=misconfig_counts_as_gap,
        tolerance_pct=tolerance_pct,
    )
    snap.update(
        {
            "scope_kind": "workload",
            "scope_id": scope_id,
            "scope_name": scope_name or demo_scope_name(scope_id),
            "connection_configured": False,
            "source": "demo_dummy_data",
            "demo": True,
            "error": "",
        }
    )
    return snap


def ensure_demo_workload(scope_id: str = DEMO_WORKLOAD_ID) -> dict[str, Any]:
    """Create/refresh a demo workload so it shows up in the scope picker, using the catalog's
    per-workload resource set as its nodes."""
    from app.demo_catalog import nodes_for, workload_meta
    from app.workloads.registry import get_workload, upsert_workload

    meta = workload_meta(scope_id)
    nodes = nodes_for(scope_id)
    existing = get_workload(scope_id)
    if existing:
        return upsert_workload(
            {**existing, "name": meta["name"], "nodes": nodes, "description": meta["description"]}
        )
    return upsert_workload(
        {
            "id": scope_id,
            "name": meta["name"],
            "description": meta["description"],
            "nodes": nodes,
            "tags": list(meta.get("tags", [])),
            "created_by": "system-demo",
        }
    )


def seed_demo(*, misconfig_counts_as_gap: bool = True, tolerance_pct: float = 10.0, tenant_id: str = "default",
              scope_id: str = DEMO_WORKLOAD_ID, scope_name: str | None = None) -> dict[str, Any]:
    """Ensure the demo workload exists and cache a fresh demo coverage snapshot."""
    from app.amba import cache

    ensure_demo_workload(scope_id)
    snap = build_demo_snapshot(misconfig_counts_as_gap=misconfig_counts_as_gap, tolerance_pct=tolerance_pct,
                               scope_id=scope_id, scope_name=scope_name)
    cache.write_snapshot(tenant_id, "workload", scope_id, snap)
    return snap


def demo_scope_name(scope_id: str) -> str:
    """Display name for a demo workload id (resolves names from the catalog/registry)."""
    if scope_id in DEMO_WORKLOAD_IDS:
        return name_for(scope_id)
    try:
        from app.workloads.registry import get_workload

        w = get_workload(scope_id)
        if w:
            return w.get("name") or scope_id
    except Exception:  # noqa: BLE001 - name is cosmetic
        pass
    return scope_id


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    return scope_kind == "workload" and scope_id in DEMO_WORKLOAD_IDS
