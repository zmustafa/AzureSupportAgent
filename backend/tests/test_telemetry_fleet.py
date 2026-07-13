from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api import telemetry
from app.core.security import Principal
from app.telemetry import cache

_PRINCIPAL = Principal(
    subject="fleet-test", email="fleet@example.com", tenant_id="telemetry-fleet-tenant", role="admin"
)


def _generated_at(*, seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


async def test_telemetry_fleet_is_tenant_scoped_cached_only_and_worst_first(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_PATH", tmp_path / "telemetry-cache.json")
    monkeypatch.setattr(telemetry, "_settings", lambda: (60, [], 200))
    monkeypatch.setattr("app.workloads.registry.list_workloads", lambda: [
        {
            "id": "w-high",
            "name": "Zulu",
            "connection_id": "connection-high",
            "criticality": "high",
            "environment": "production",
        },
        {
            "id": "w-low-b",
            "name": "Beta",
            "connection_id": "connection-low-b",
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
            "id": "w-low-a",
            "name": "Alpha",
            "connection_id": "connection-low-a",
            "criticality": "critical",
            "environment": "production",
        },
    ])

    low_snapshot = {
        "generated_at": _generated_at(seconds_ago=120),
        "coverage_pct": 25,
        "kpis": {
            "total_resources_in_reference": 12,
            "with_any_diag": 7,
            "with_all_categories": 3,
            "unknown_destinations": 2,
            "unreadable": 1,
        },
        "gaps": [{"id": 1}, {"id": 2}],
        "demo": True,
        "error": "partial read",
    }
    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-low-a", low_snapshot)
    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-low-b", low_snapshot)
    cache.write_snapshot(_PRINCIPAL.tenant_id, "workload", "w-high", {
        "generated_at": _generated_at(),
        "coverage_pct": 100,
        "kpis": {"total_resources_in_reference": 0, "with_any_diag": 0, "with_all_categories": 0},
        "gaps": [],
    })
    cache.write_snapshot("another-tenant", "workload", "w-never", {
        "generated_at": _generated_at(), "coverage_pct": 1, "kpis": {}, "gaps": [{}],
    })
    monkeypatch.setattr(telemetry, "collect_coverage", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("fleet must not collect Azure telemetry")
    ))

    result = await telemetry.fleet(_PRINCIPAL)

    assert result == {
        "workloads": result["workloads"], "ttl_s": 60, "total": 4, "scanned": 3,
    }
    assert [row["workload_id"] for row in result["workloads"]] == [
        "w-low-a", "w-low-b", "w-high", "w-never",
    ]
    row = result["workloads"][0]
    assert row["connection_id"] == "connection-low-a"
    assert row["criticality"] == "critical"
    assert row["environment"] == "production"
    assert row["coverage_pct"] == 25
    assert row["resources"] == 12
    assert row["with_any_diag"] == 7
    assert row["with_all_categories"] == 3
    assert row["unknown_destinations"] == 2
    assert row["unreadable"] == 1
    assert row["gaps"] == 2
    assert row["demo"] is True
    assert row["stale"] is True
    assert row["error"] == "partial read"
    assert isinstance(row["age_seconds"], int)

    never = result["workloads"][-1]
    assert never["has_scan"] is False
    assert never["coverage_pct"] is None
    assert never["age_seconds"] is None
    assert never["stale"] is True
    zero_resource = next(row for row in result["workloads"] if row["workload_id"] == "w-high")
    assert zero_resource["has_scan"] is True
    assert zero_resource["coverage_pct"] is None
