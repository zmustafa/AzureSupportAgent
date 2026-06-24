"""Workload-scope resource-group enumeration guardrail for `reverse.dump_resources`.

The bug class: tag operations scoped to a *workload* (via `dump_resources`) only queried the
`Resources` table, so resource GROUPS (which live in `resourcecontainers`) were never returned
and their RG-level tags could not be applied / removed / reverted. These tests pin that a
workload that includes a resource group (or whole subscription) now yields the RG entry so the
downstream tag remediation set covers RG-level tags alongside their resources.
"""
import json

import pytest

import app.architectures.reverse as reverse
from app.exec.command_runner import CaptureResult


def _cap(rows: list[dict]) -> CaptureResult:
    return CaptureResult(ok=True, stdout=json.dumps(rows))


@pytest.mark.asyncio
async def test_dump_resources_includes_resource_group(monkeypatch):
    """A workload with a resource_group node yields the RG itself (resourcecontainers row)."""
    sub = "11111111-1111-1111-1111-111111111111"
    workload = {
        "nodes": [
            {"kind": "resource_group", "subscription_id": sub, "resource_group": "rg-azsupagent"},
        ]
    }

    async def fake_open(_conn):
        return ("/tmp/cfg", None)

    def fake_close(_cfg):
        return None

    async def fake_kql(kql, _conn, **_kw):
        if "resourcecontainers" in kql:
            return _cap([
                {
                    "id": f"/subscriptions/{sub}/resourceGroups/rg-azsupagent",
                    "name": "rg-azsupagent",
                    "type": "microsoft.resources/subscriptions/resourcegroups",
                    "location": "southcentralus",
                    "subscriptionId": sub,
                    "tags": {"env": "demo"},
                }
            ])
        if kql.startswith("Resources") and "properties" in kql and "where id in~" in kql:
            return _cap([])  # pass 2: no extra properties
        # pass 1: a single member VM in the RG
        return _cap([
            {
                "id": f"/subscriptions/{sub}/resourceGroups/rg-azsupagent/providers/Microsoft.Compute/virtualMachines/vm1",
                "name": "vm1",
                "type": "microsoft.compute/virtualmachines",
                "location": "southcentralus",
                "resourceGroup": "rg-azsupagent",
                "subscriptionId": sub,
                "tags": {},
            }
        ])

    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", fake_close)
    monkeypatch.setattr(reverse, "run_kql_capture", fake_kql)

    out = await reverse.dump_resources(workload, {"id": "c"})

    types = {r["type"] for r in out["resources"]}
    assert "microsoft.resources/subscriptions/resourcegroups" in types
    rg = next(r for r in out["resources"] if r["type"].endswith("resourcegroups"))
    assert rg["name"] == "rg-azsupagent"
    assert rg["tags"] == {"env": "demo"}
    # The member resource is still present alongside the RG.
    assert any(r["type"] == "microsoft.compute/virtualmachines" for r in out["resources"])


@pytest.mark.asyncio
async def test_dump_resources_rg_query_skipped_when_no_scope(monkeypatch):
    """No resourcecontainers query is issued when there are no RG targets (defensive)."""
    workload = {"nodes": []}

    async def fake_open(_conn):
        return ("/tmp/cfg", None)

    def fake_close(_cfg):
        return None

    seen = {"rc": False}

    async def fake_kql(kql, _conn, **_kw):
        if "resourcecontainers" in kql:
            seen["rc"] = True
        return _cap([])

    monkeypatch.setattr(reverse, "open_sp_session", fake_open)
    monkeypatch.setattr(reverse, "close_sp_session", fake_close)
    monkeypatch.setattr(reverse, "run_kql_capture", fake_kql)

    out = await reverse.dump_resources(workload, {"id": "c"})
    # Empty workload resolves to an error before any query; no RG query issued.
    assert out["error"]
    assert seen["rc"] is False
