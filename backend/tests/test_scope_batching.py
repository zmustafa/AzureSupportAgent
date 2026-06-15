"""Tests for the scope predicate-batching that fixes the 'Query is too long.' failure when a
workload has many individual resources (Resource Graph caps queries at ~8000 chars).

A workload of ~230 resources previously produced a single `id in~ (...)` clause well over the
limit, so every coverage scan (Telemetry / Monitoring / Backup-DR / Performance) failed with an
empty error snapshot. ``scope_predicate_batches`` splits the scope into several predicates each
safely under the limit, and ``query_resources_batched`` runs + merges them (de-duplicated).
"""
from __future__ import annotations

import asyncio
import re

from app.assessments import runner
from app.assessments.runner import (
    _PREDICATE_BUDGET,
    query_resources_batched,
    scope_predicate_batches,
)
from app.exec.command_runner import CaptureResult

_SUB = "c3f6ae08-38a1-466d-abc2-972ad76b8661"
_PROJ = "id, name, type, resourceGroup, subscriptionId, location, properties, sku, tags"


def _full_query_len(pred: str) -> int:
    return len(
        f"Resources | where {pred} | project {_PROJ} "
        f"| order by type asc, name asc | take 1000"
    )


def _ids(n: int) -> list[str]:
    out = []
    for i in range(n):
        if i % 5 == 0:  # longer, nested ids
            out.append(f"/subscriptions/{_SUB}/resourceGroups/rg-{i % 7}/providers/Microsoft.Sql/servers/srv-{i}/databases/db-{i}")
        else:
            out.append(f"/subscriptions/{_SUB}/resourceGroups/rg-{i % 7}/providers/Microsoft.Compute/virtualMachines/vm-prod-{i}")
    return out


def test_empty_scope_yields_no_predicates():
    assert scope_predicate_batches({"subscriptions": [], "rg_pairs": [], "resource_ids": []}) == []


def test_small_scope_is_a_single_predicate():
    scope = {"subscriptions": [], "rg_pairs": [], "resource_ids": _ids(3)}
    preds = scope_predicate_batches(scope)
    assert len(preds) == 1
    assert preds[0].startswith("id in~ (")


def test_large_workload_splits_into_under_limit_batches():
    """The regression: 230 resources must NOT exceed the query-length limit."""
    ids = _ids(230)
    preds = scope_predicate_batches({"subscriptions": [], "rg_pairs": [], "resource_ids": ids})
    assert len(preds) > 1, "230 resources must be split into multiple batches"
    # Every resulting FULL query stays safely under Resource Graph's ~8000-char cap.
    for p in preds:
        assert _full_query_len(p) < 8000, f"batch query too long: {_full_query_len(p)}"
        assert len(p) <= _PREDICATE_BUDGET + 16  # predicate itself within budget (+'id in~ ()')
    # Every id is covered exactly once across all batches.
    covered = [m for p in preds for m in re.findall(r"'([^']+)'", p)]
    assert sorted(covered) == sorted(ids)
    assert len(covered) == len(set(covered))


def test_mixed_scope_packs_subs_rgs_then_id_batches():
    scope = {
        "subscriptions": [_SUB],
        "rg_pairs": [(_SUB, "rg-a"), (_SUB, "rg-b")],
        "resource_ids": _ids(120),
    }
    preds = scope_predicate_batches(scope)
    # First predicate carries the subscription/RG clauses; the rest are id batches.
    assert preds[0].startswith("subscriptionId in~ (")
    assert all("id in~ (" in p for p in preds[1:])
    for p in preds:
        assert _full_query_len(p) < 8000


def test_query_resources_batched_runs_each_and_dedupes(monkeypatch):
    """Each predicate is queried; rows are merged and de-duplicated by id."""
    seen_queries: list[str] = []

    async def _fake_capture(kql, connection, *, output="json", session_config_dir=None):
        seen_queries.append(kql)
        # Return a row whose id is embedded in the predicate's first quoted token, plus a
        # shared duplicate row to prove de-duplication across batches.
        m = re.search(r"'([^']+)'", kql)
        rid = m.group(1) if m else "x"
        import json
        rows = [
            {"id": rid, "name": "a", "type": "t"},
            {"id": "/dup/shared", "name": "dup", "type": "t"},
        ]
        return CaptureResult(ok=True, stdout=json.dumps(rows))

    monkeypatch.setattr(runner, "run_kql_capture", _fake_capture)

    preds = ["id in~ ('/a')", "id in~ ('/b')", "id in~ ('/c')"]
    rows = asyncio.run(query_resources_batched(preds, None, projection=_PROJ))
    assert len(seen_queries) == 3  # one query per predicate
    ids = [r["id"] for r in rows]
    # The shared duplicate appears once; the per-batch ids each appear once.
    assert ids.count("/dup/shared") == 1
    assert {"/a", "/b", "/c"}.issubset(set(ids))


def test_query_resources_batched_fail_closed(monkeypatch):
    """A failed query raises (so the caller surfaces 'error', never a misleading empty pass)."""
    async def _fail(kql, connection, *, output="json", session_config_dir=None):
        return CaptureResult(ok=False, error="boom")

    monkeypatch.setattr(runner, "run_kql_capture", _fail)
    try:
        asyncio.run(query_resources_batched(["id in~ ('/a')"], None, projection=_PROJ))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_heavy_projection_bisects_on_truncation(monkeypatch):
    """A heavy (properties) projection adaptively halves a truncating id batch until each
    sub-query fits, so all resources are returned despite the 256 KB output cap."""
    import json

    # Simulate: any query with >2 ids "truncates"; <=2 ids succeeds.
    calls = {"n": 0}

    async def _capture(kql, connection, *, output="json", session_config_dir=None):
        calls["n"] += 1
        toks = re.findall(r"'([^']+)'", kql)
        if len(toks) > 2:
            return CaptureResult(ok=False, error="Output truncated at 256 KB.")
        rows = [{"id": t, "name": "n", "type": "t"} for t in toks]
        return CaptureResult(ok=True, stdout=json.dumps(rows))

    monkeypatch.setattr(runner, "run_kql_capture", _capture)
    # One predicate carrying 7 ids, heavy projection (contains 'properties').
    ids = [f"'/r/{i}'" for i in range(7)]
    pred = "id in~ (" + ", ".join(ids) + ")"
    rows = asyncio.run(query_resources_batched([pred], None, projection=_PROJ))
    got = sorted(r["id"] for r in rows)
    assert got == sorted(f"/r/{i}" for i in range(7)), got  # every id returned despite truncation
    assert calls["n"] > 1  # it actually had to split


def test_heavy_projection_skips_single_oversized_resource(monkeypatch):
    """A single resource whose properties exceed the cap is skipped (logged), not fatal."""
    import json

    async def _capture(kql, connection, *, output="json", session_config_dir=None):
        toks = re.findall(r"'([^']+)'", kql)
        if "/r/huge" in toks:
            # Even alone, this one truncates.
            if len(toks) == 1:
                return CaptureResult(ok=False, error="Output truncated at 256 KB.")
            return CaptureResult(ok=False, error="Output truncated at 256 KB.")
        return CaptureResult(ok=True, stdout=json.dumps([{"id": t} for t in toks]))

    monkeypatch.setattr(runner, "run_kql_capture", _capture)
    pred = "id in~ ('/r/a', '/r/huge', '/r/b')"
    rows = asyncio.run(query_resources_batched([pred], None, projection=_PROJ))
    ids = sorted(r["id"] for r in rows)
    # The two normal resources come through; the oversized one is silently skipped.
    assert ids == ["/r/a", "/r/b"], ids


def test_light_projection_does_not_chunk(monkeypatch):
    """A light projection (no properties) runs one query per predicate — no id re-chunking."""
    import json

    seen: list[str] = []

    async def _capture(kql, connection, *, output="json", session_config_dir=None):
        seen.append(kql)
        toks = re.findall(r"'([^']+)'", kql)
        return CaptureResult(ok=True, stdout=json.dumps([{"id": t} for t in toks]))

    monkeypatch.setattr(runner, "run_kql_capture", _capture)
    ids = [f"'/r/{i}'" for i in range(50)]
    pred = "id in~ (" + ", ".join(ids) + ")"
    light = "id, name, type, resourceGroup, subscriptionId, location, tags"
    rows = asyncio.run(query_resources_batched([pred], None, projection=light))
    assert len(seen) == 1  # single query, no chunking for the light projection
    assert len(rows) == 50

