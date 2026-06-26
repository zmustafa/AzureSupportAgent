"""Synthetic Telemetry Coverage data for review/demo without a live Azure tenant.

Drives the demo from the shared per-workload catalog (``app.demo_catalog``) so each demo
workload gets its own resources and a realistic diagnostic-settings spread derived from
each resource's health tier:
    green → compliant (all logs to the workload's approved Log Analytics workspace)
    amber → drift (ships to an unapproved workspace) or partial (metrics only, logs missing)
    red   → no diagnostic settings at all

Marked demo everywhere; the API serves this instead of querying Azure for the demo scope."""
from __future__ import annotations

from typing import Any

from app.demo_catalog import (
    CONTOSO_ID,
    DEMO_SUB,
    approved_workspace_id,
    bucket,
    resources_for,
)
from app.telemetry.collector import compute_coverage

DEMO_WORKLOAD_ID = CONTOSO_ID  # default demo scope used by the API when none is supplied

# A non-approved workspace used to demonstrate destination drift.
_DRIFT_WS = (
    f"/subscriptions/{DEMO_SUB}/resourceGroups/rg-shared-misc/providers/"
    "microsoft.operationalinsights/workspaces/sandbox-law"
)


def demo_approved_workspaces(scope_id: str = CONTOSO_ID) -> list[str]:
    return [approved_workspace_id(scope_id)]


def _setting(workspace_id: str, *, all_logs: bool = False, metrics: bool = True, retention: int = 30) -> dict[str, Any]:
    logs: list[dict[str, Any]] = []
    if all_logs:
        logs = [{"category": "allLogs", "categoryGroup": "allLogs", "enabled": True,
                 "retentionPolicy": {"enabled": True, "days": retention}}]
    return {
        "name": "diag",
        "properties": {
            "workspaceId": workspace_id,
            "logs": logs,
            "metrics": ([{"category": "AllMetrics", "enabled": True}] if metrics else []),
        },
    }


def demo_diag_by_resource(scope_id: str = CONTOSO_ID) -> dict[str, list[dict[str, Any]]]:
    """Diagnostic settings keyed by lowercased resource id, exercising every state."""
    approved = approved_workspace_id(scope_id)
    out: dict[str, list[dict[str, Any]]] = {}
    for res in resources_for(scope_id):
        tier = res["tier"]
        rid = res["id"].lower()
        if tier == "red":
            continue  # no diagnostic settings → status "none"
        if tier == "green":
            out[rid] = [_setting(approved, all_logs=True)]
        else:  # amber: alternate drift vs. partial (metrics only, logs missing)
            if bucket(res["id"], 2) == 0:
                out[rid] = [_setting(_DRIFT_WS, all_logs=True)]      # drift destination
            else:
                out[rid] = [_setting(approved, all_logs=False)]     # partial: no log categories
    return out


def build_demo_snapshot(*, scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    from app.amba.demo import demo_scope_name

    snap = compute_coverage(
        resources_for(scope_id),
        demo_diag_by_resource(scope_id),
        approved_workspaces=demo_approved_workspaces(scope_id),
        unreadable=set(),
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


def seed_demo(*, tenant_id: str = "default", scope_id: str = CONTOSO_ID, scope_name: str | None = None) -> dict[str, Any]:
    # Cache the demo snapshot only — do NOT auto-register the demo workload (explicit Demo Data
    # load handles that), so viewing a demo coverage page never creates a phantom workload.
    from app.telemetry import cache

    snap = build_demo_snapshot(scope_id=scope_id, scope_name=scope_name)
    cache.write_snapshot(tenant_id, "workload", scope_id, snap)
    return snap


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    from app.amba.demo import is_demo_scope as _is

    return _is(scope_kind, scope_id)
