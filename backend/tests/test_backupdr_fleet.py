from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api import backupdr
from app.backupdr import cache
from app.core.security import Principal

_PRINCIPAL = Principal(
    subject="fleet-test", email="fleet@example.com", tenant_id="backupdr-fleet-tenant", role="admin"
)


def _generated_at(*, seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


async def test_backupdr_fleet_is_tenant_scoped_cached_only_and_worst_first(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_PATH", tmp_path / "backupdr-cache.json")
    monkeypatch.setattr(backupdr, "_settings", lambda: (60, 180, 24, 200))
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [
        {
            "id": "w-high",
            "name": "Zulu",
            "connection_id": "connection-high",
            "criticality": "high",
            "environment": "production",
        },
        {
            "id": "w-low-less",
            "name": "Beta",
            "connection_id": "connection-low-less",
            "criticality": "medium",
            "environment": "staging",
        },
        {
            "id": "w-never",
            "name": "Never scanned",
            "connection_id": "connection-never",
            "criticality": "low",
            "environment": "development",
        },
        {
            "id": "w-low-most",
            "name": "Alpha",
            "connection_id": "connection-low-most",
            "criticality": "critical",
            "environment": "production",
        },
    ])

    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-low-most", {
        "generated_at": _generated_at(seconds_ago=120),
        "scorecard": {
            "pct_protected": 25,
            "total": 12,
            "protected": 3,
            "pct_offsite": 40,
            "pct_recent_job": 50,
            "dr_pairs": 4,
            "dr_pairs_stale": 2,
            "dr_pairs_unhealthy": 1,
        },
        "gaps": [{"id": 1}, {"id": 2}, {"id": 3}],
        "demo": True,
        "error": "partial read",
    })
    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-low-less", {
        "generated_at": _generated_at(),
        "scorecard": {"pct_protected": 25, "total": 4, "protected": 3},
        "gaps": [{}],
    })
    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-high", {
        "generated_at": _generated_at(),
        "scorecard": {"pct_protected": 100, "total": 0, "protected": 0},
        "gaps": [],
    })
    cache.write_snapshot("another-tenant", "workload", "w-never", {
        "generated_at": _generated_at(), "scorecard": {"pct_protected": 1}, "gaps": [{}],
    })
    monkeypatch.setattr(backupdr, "collect_coverage", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("fleet must not collect Azure backup/DR coverage")
    ))

    result = await backupdr.fleet(_PRINCIPAL)

    assert result == {
        "workloads": result["workloads"], "ttl_s": 60, "total": 4, "scanned": 3,
    }
    assert [row["workload_id"] for row in result["workloads"]] == [
        "w-low-most", "w-low-less", "w-high", "w-never",
    ]
    row = result["workloads"][0]
    assert row["connection_id"] == "connection-low-most"
    assert row["criticality"] == "critical"
    assert row["environment"] == "production"
    assert row["pct_protected"] == 25
    assert row["total"] == 12
    assert row["protected"] == 3
    assert row["unprotected"] == 9
    assert row["pct_offsite"] == 40
    assert row["pct_recent_job"] == 50
    assert row["dr_pairs"] == 4
    assert row["dr_pairs_stale"] == 2
    assert row["dr_pairs_unhealthy"] == 1
    assert row["gaps"] == 3
    assert row["demo"] is True
    assert row["stale"] is True
    assert row["error"] == "partial read"
    assert isinstance(row["age_seconds"], int)

    never = result["workloads"][-1]
    assert never["has_scan"] is False
    assert never["pct_protected"] is None
    assert never["age_seconds"] is None
    assert never["stale"] is True
    zero_resource = next(row for row in result["workloads"] if row["workload_id"] == "w-high")
    assert zero_resource["has_scan"] is True
    assert zero_resource["pct_protected"] is None
