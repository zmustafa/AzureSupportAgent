from __future__ import annotations

import json

import pytest

from app.alert_analysis import cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "alert_analysis_cache.json"
    monkeypatch.setattr(cache, "_PATH", path)
    monkeypatch.setattr(cache, "_legacy_cached", None)
    return path


def test_sidecars_are_scope_isolated_and_return_defensive_copies(isolated_cache) -> None:
    first = {"generated_at": "2026-01-01T00:00:00+00:00", "rules": [{"name": "one"}]}
    second = {"generated_at": "2026-01-02T00:00:00+00:00", "rules": [{"name": "two"}]}
    cache.write_snapshot("tenant", "connection", "subscription", "sub-1", first)
    cache.write_snapshot("tenant", "connection", "subscription", "sub-2", second)

    loaded = cache.read_snapshot("tenant", "connection", "subscription", "sub-1")
    assert loaded == first
    assert loaded is not first
    loaded["rules"][0]["name"] = "changed"
    assert cache.read_snapshot("tenant", "connection", "subscription", "sub-1") == first
    assert cache.read_snapshot("tenant", "connection", "subscription", "sub-2") == second
    assert len(list(isolated_cache.with_suffix("").glob("*/*.json"))) == 2


def test_legacy_snapshot_is_lazily_migrated_to_a_sidecar(isolated_cache) -> None:
    snapshot = {"generated_at": "2026-01-01T00:00:00+00:00", "rules": [{"name": "legacy"}]}
    isolated_cache.write_text(
        json.dumps({"tenant": {"connection:workload:workload-1": snapshot}}),
        encoding="utf-8",
    )

    assert cache.read_snapshot("tenant", "connection", "workload", "workload-1") == snapshot
    sidecar = cache._sidecar_path("tenant", "connection", "workload", "workload-1")
    assert sidecar.exists()
    isolated_cache.unlink()
    assert cache.read_snapshot("tenant", "connection", "workload", "workload-1") == snapshot


def test_sidecar_retention_is_bounded(isolated_cache, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_MAX_SIDECARS", 3)
    for index in range(6):
        cache.write_snapshot("tenant", "connection", "subscription", f"sub-{index}", {"index": index})
    assert len(list(isolated_cache.with_suffix("").glob("*/*.json"))) == 3
    assert cache.read_snapshot("tenant", "connection", "subscription", "sub-5") == {"index": 5}


def test_delete_removes_sidecar_and_legacy_entry(isolated_cache) -> None:
    legacy = {"tenant": {"connection:subscription:legacy": {"value": "old"}}}
    isolated_cache.write_text(json.dumps(legacy), encoding="utf-8")
    cache.write_snapshot("tenant", "connection", "subscription", "current", {"value": "new"})

    assert cache.delete_snapshot("tenant", "connection", "subscription", "current") is True
    assert cache.read_snapshot("tenant", "connection", "subscription", "current") is None
    assert cache.delete_snapshot("tenant", "connection", "subscription", "legacy") is True
    assert cache.read_snapshot("tenant", "connection", "subscription", "legacy") is None