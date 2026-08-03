"""The IAM cache index survives concurrent writers.

Every index mutation is a read-modify-write over ONE shared JSON file:

    data = _read_index(); data[tenant]["scopes"][scope] = entry; _write_index(data)

Two of those interleaving is a lost update — the second writer's snapshot predates the first
writer's change, so the first scope silently disappears from the index while its sidecar blob
sits on disk orphaned. The reader then reports a scope that WAS collected as never collected,
which is the "silently wrong, not slow" failure this codebase already learned about with
``_write_seq``.

Latent before the refresh fanned out (writes already run on worker threads while the API serves
requests that write baselines and drift); collecting three scopes at once makes it likely.
"""
from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A cache module pointed at a private directory."""
    from app.iam import cache as cache_mod

    monkeypatch.setattr(cache_mod, "_INDEX", tmp_path / "index.json")
    monkeypatch.setattr(cache_mod, "_BLOBS", tmp_path / "blobs")
    monkeypatch.setattr(cache_mod, "_migrate_legacy_paths", lambda: None)
    return cache_mod


def _meta(name: str) -> dict:
    return {"scopeType": "subscription", "displayName": name, "subscriptionId": "s",
            "status": "Succeeded", "collectors": [], "coverage": {}, "demo": False}


def test_concurrent_scope_writes_all_survive(store):
    """Twenty-four threads writing at once; every scope must be in the index afterwards."""
    scopes = [f"/subscriptions/{i:04d}" for i in range(24)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda s: store.write_scope(
                "t-1", s, meta=_meta(s), rows=[{"principalId": "p", "scope": s}]),
            scopes,
        ))

    present = {m["scope"] for m in store.list_scope_meta("t-1")}
    missing = set(scopes) - present
    assert not missing, f"{len(missing)} scope(s) lost to a concurrent write: {sorted(missing)[:5]}"


def test_a_scope_written_concurrently_keeps_its_rows(store):
    """A surviving index entry must still point at readable rows."""
    scopes = [f"/subscriptions/{i:04d}" for i in range(12)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(
            lambda s: store.write_scope("t-1", s, meta=_meta(s), rows=[{"scope": s}] * 3),
            scopes,
        ))

    for s in scopes:
        assert len(store.read_scope_rows("t-1", s)) == 3, f"{s} lost its rows"


def test_writes_across_tenants_do_not_clobber_each_other(store):
    """The index holds every tenant, so cross-tenant interleaving is the same hazard."""
    work = [(f"t-{t}", f"/subscriptions/{i:04d}") for t in range(3) for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda tw: store.write_scope(tw[0], tw[1], meta=_meta(tw[1]), rows=[]), work))

    for t in range(3):
        present = {m["scope"] for m in store.list_scope_meta(f"t-{t}")}
        assert len(present) == 8, f"tenant t-{t} kept only {len(present)} of 8 scopes"


def test_the_write_counter_counts_every_concurrent_write(store):
    """A LOST bump is a stale memo served as fresh — worse than a slow one.

    One ``write_scope`` bumps more than once (the sidecar blob and the index are separate
    writes), so the invariant is not "+1 per scope" — it is that N concurrent writes bump
    exactly N times whatever one write costs. Measuring the per-write cost first keeps this
    honest if the number of internal writes ever changes.
    """
    baseline = store.cache_version()
    store.write_scope("t-probe", "/subscriptions/probe", meta=_meta("probe"), rows=[])
    per_write = store.cache_version() - baseline
    assert per_write >= 1

    before = store.cache_version()
    scopes = [f"/subscriptions/{i:04d}" for i in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda s: store.write_scope("t-1", s, meta=_meta(s), rows=[]), scopes))

    assert store.cache_version() - before == 20 * per_write


def test_the_guard_is_reentrant(store):
    """A guarded function may legitimately call another one; a plain Lock would deadlock."""
    store.write_scope("t-1", "/subscriptions/0001", meta=_meta("a"), rows=[])
    # delete_tenant is guarded and iterates scopes that were written under the same guard.
    store.delete_tenant("t-1")
    assert store.list_scope_meta("t-1") == []


def test_every_index_mutator_is_guarded():
    """Guards the guard: a new mutator added without the decorator reopens the race silently."""
    import inspect
    import re

    from app.iam import cache as cache_mod

    src = inspect.getsource(cache_mod)
    # Comments explaining the hazard quote the very pattern being detected, so strip them first
    # or the detector reports the explanation as a defect.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))

    unguarded: list[str] = []
    for match in re.finditer(r"(?m)^(@_index_guarded\n)?def (\w+)\(", code):
        name = match.group(2)
        if name == "_write_index":
            continue  # the primitive itself; its CALLERS are what must be serialised
        body = code[match.end():]
        nxt = re.search(r"(?m)^(@_index_guarded\n)?def \w+\(", body)
        body = body[: nxt.start()] if nxt else body
        if "_write_index(" in body and not match.group(1):
            unguarded.append(name)
    assert not unguarded, f"index mutators missing @_index_guarded: {unguarded}"
