"""Tests for the PIM / JIT lifecycle review — demo synthesis, classification, cache, degrade."""
from __future__ import annotations

import asyncio

from app.identity import pim, pim_cache


def test_demo_snapshot_shape_and_kpis():
    snap = pim.build_demo_snapshot(tenant_id="t1")
    assert set(snap["groups"].keys()) == set(pim.GROUP_KEYS)
    # Every finding carries the common PIM shape.
    required = {"id", "kind", "severity", "subject", "role", "assignment_type", "remediation"}
    for items in snap["groups"].values():
        for f in items:
            assert required <= set(f.keys())
    k = snap["kpis"]
    assert k["standing_access"] == len(snap["groups"]["standing_access"])
    assert k["stale_eligible"] == len(snap["groups"]["stale_eligible"])
    assert k["activations"] == len(snap["groups"]["activation_review"])
    assert snap["meta"]["source"] == "demo"


def test_drift_flags_global_admin_standing_as_critical():
    snap = pim.build_demo_snapshot(tenant_id="t1")
    standing = snap["groups"]["standing_access"]
    ga = [f for f in standing if f["role"] == "Global Administrator"]
    assert ga and ga[0]["severity"] == "critical"
    assert all(f["assignment_type"] == "active" for f in standing)
    # high_priv_standing counts only tier-0 roles.
    assert snap["kpis"]["high_priv_standing"] == sum(1 for f in standing if f["role_tier"] == "tier0")
    assert snap["group_severity"]["standing_access"] == "critical"


def test_stale_eligible_includes_never_activated():
    snap = pim.build_demo_snapshot(tenant_id="t1")
    elig = snap["groups"]["stale_eligible"]
    never = [f for f in elig if f["last_activated_at"] is None]
    assert never, "expected at least one never-activated eligible assignment"
    # Idle assignments past the threshold are surfaced with a non-info severity.
    assert any((f.get("days_idle") or 0) >= pim._STALE_ELIGIBLE_DAYS for f in elig)


def test_activation_review_flags_long_or_tier0():
    snap = pim.build_demo_snapshot(tenant_id="t1")
    acts = snap["groups"]["activation_review"]
    assert acts
    # A currently-active activation has positive days_left and an expiry.
    active_now = [f for f in acts if (f.get("days_left") is not None and f["expires_at"])]
    assert active_now
    # Long (>8h) or tier-0 activations are warnings; routine ones are info.
    for f in acts:
        long_or_tier0 = f["role_tier"] == "tier0" or "for " in f["title"]
        assert f["severity"] == ("warning" if long_or_tier0 else "info")


def test_cache_roundtrip_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(pim_cache, "_PATH", tmp_path / "pim.json")
    assert pim_cache.read_snapshot("t1") is None
    snap = pim.build_demo_snapshot(tenant_id="t1")
    pim_cache.write_snapshot("t1", snap)
    again = pim_cache.read_snapshot("t1")
    assert again is not None and again["kpis"] == snap["kpis"]
    pim_cache.delete_snapshot("t1")
    assert pim_cache.read_snapshot("t1") is None


def test_collect_pim_degrades_when_no_service_principal(monkeypatch):
    # When the connection can't drive Graph (no SP), every group records the config error and
    # nothing raises — the schedule-only groups always carry the "not available" note.
    import app.mcp.client as mcp_client
    monkeypatch.setattr(mcp_client, "entra_graph_config_error", lambda conn: "needs a service principal")

    snap = asyncio.run(pim.collect_pim({"id": "c1"}, tenant_id="t1"))
    assert snap["connection_configured"] is True
    assert all(snap["group_severity"][g] == "ok" for g in pim.GROUP_KEYS)
    assert set(snap["errors"].keys()) == set(pim.GROUP_KEYS)
