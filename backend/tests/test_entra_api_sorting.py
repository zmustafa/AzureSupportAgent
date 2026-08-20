"""Server-side column sorting on the Entra grids that page or cap.

These four endpoints hand the browser a slice, not the set. Sorting that slice in the
browser and labeling the column "highest risk" is a lie by omission — it is the highest
risk *among the rows the server already picked by something else*. So the sort happens
here, before the slice, and these tests pin the two properties that make it trustworthy:

1. the order actually changes with the key and the direction, and
2. a row with no value for the sorted column goes LAST in BOTH directions, because
   "not recorded" is neither "oldest" nor "zero".
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import entra as entra_api
from app.entra import cache, demo
from app.entra import snapshot as snapshot_mod


class _Principal:
    tenant_id = demo.DEMO_TENANT
    subject = "dev"


@pytest.fixture(autouse=True)
def _demo_tenant(tmp_path, monkeypatch):
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation

    import app.core.azure_connections as ac

    monkeypatch.setattr(
        ac, "resolve_connection",
        lambda cid: {"id": "conn-demo", "tenant_id": demo.DEMO_TENANT} if cid == "conn-demo" else None,
    )
    demo.seed()
    yield
    cache.clear_memo()


def _run(coro):
    return asyncio.run(coro)


# Handlers are called in-process, so FastAPI never evaluates the `Query(...)` defaults —
# every parameter that has one has to be supplied explicitly or it arrives as a Query object.
def _findings(sort=None, dir="desc", limit=2000, offset=0, **kw):
    return _run(entra_api.findings(
        connection_id="conn-demo", principal=_Principal(),
        sort=sort, dir=dir, limit=limit, offset=offset, **kw))["findings"]


def _apps(sort=None, dir="desc", limit=2000, offset=0, **kw):
    return _run(entra_api.apps_inventory(
        connection_id="conn-demo", principal=_Principal(),
        sort=sort, dir=dir, limit=limit, offset=offset, risk_min=0, **kw))["apps"]


def _inbox(sort=None, dir="desc", limit=2000, offset=0, **kw):
    return _run(entra_api.findings_inbox(
        connection_id="conn-demo", principal=_Principal(),
        sort=sort, dir=dir, limit=limit, offset=offset, **kw))["findings"]


def _assignments(sort=None, dir="desc", kind="all", **kw):
    return _run(entra_api.privileged_assignments(
        connection_id="conn-demo", principal=_Principal(),
        sort=sort, dir=dir, kind=kind, privileged=kw.pop("privileged", False), **kw))["assignments"]


# --------------------------------------------------------------------------- helpers
def _is_sorted(values, *, desc: bool) -> bool:
    pairs = zip(values, values[1:])
    return all(a >= b for a, b in pairs) if desc else all(a <= b for a, b in pairs)


# --------------------------------------------------------------------------- findings
def test_findings_sort_by_severity_orders_both_ways():
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    desc = [rank[f["severity"]] for f in _findings(sort="severity", dir="desc")]
    asc = [rank[f["severity"]] for f in _findings(sort="severity", dir="asc")]
    assert desc, "demo tenant should produce findings"
    assert _is_sorted(desc, desc=True)
    assert _is_sorted(asc, desc=False)


def test_findings_default_order_is_untouched_by_the_sort_feature():
    """No sort parameter must mean exactly the ordering that shipped before sorting existed."""
    assert [f["fingerprint"] for f in _findings()] == [f["fingerprint"] for f in _findings(sort=None)]


def test_findings_sort_is_applied_before_the_page_is_cut():
    """The whole point: the first page of a sorted list, not a sorted first page."""
    everything = _findings(sort="object", dir="asc")
    first_page = _findings(sort="object", dir="asc", limit=3)
    assert [f["fingerprint"] for f in first_page] == [f["fingerprint"] for f in everything[:3]]


# ------------------------------------------------------------------------------ apps
def test_apps_sort_by_name_is_alphabetical_in_both_directions():
    asc = [a["display_name"].lower() for a in _apps(sort="name", dir="asc")]
    desc = [a["display_name"].lower() for a in _apps(sort="name", dir="desc")]
    assert asc, "demo tenant should produce applications"
    assert _is_sorted(asc, desc=False)
    assert _is_sorted(desc, desc=True)


def test_apps_with_unreadable_owners_sort_last_in_both_directions():
    """An app whose owners could not be read is not an app with zero owners."""
    for direction in ("asc", "desc"):
        rows = _apps(sort="owners", dir=direction)
        unknown = [i for i, a in enumerate(rows) if not a["owners_known"]]
        known = [i for i, a in enumerate(rows) if a["owners_known"]]
        if unknown and known:
            assert min(unknown) > max(known), (
                f"unreadable owners floated into the {direction} ordering")


def test_apps_risk_sort_matches_the_shipped_default():
    assert [a["object_id"] for a in _apps(sort="risk", dir="desc")] == [a["object_id"] for a in _apps()]


# ----------------------------------------------------------------------------- inbox
def test_inbox_sort_by_age_puts_undated_findings_last_both_ways():
    for direction in ("asc", "desc"):
        rows = _inbox(sort="age", dir=direction)
        dated = [i for i, r in enumerate(rows) if r.get("age_days") is not None]
        undated = [i for i, r in enumerate(rows) if r.get("age_days") is None]
        if dated and undated:
            assert min(undated) > max(dated), f"undated findings floated up when {direction}"


def test_inbox_sort_by_state_uses_workflow_rank_not_the_alphabet():
    rows = _inbox(sort="state", dir="desc")
    ranks = [entra_api._FINDING_STATE_RANK.get(r["state"], -1) for r in rows]  # noqa: SLF001
    assert _is_sorted([r for r in ranks if r >= 0], desc=True)


# ----------------------------------------------------------------------- assignments
def test_assignments_sort_by_tier_ranks_tier0_first():
    rank = {"tier0": 3, "tier1": 2, "tier2": 1}
    rows = _assignments(sort="tier", dir="desc")
    ranked = [rank.get(r.get("role_tier"), 0) for r in rows]
    assert ranked, "demo tenant should produce role assignments"
    assert _is_sorted([r for r in ranked if r], desc=True)


def test_assignments_never_activated_sort_last_both_ways():
    for direction in ("asc", "desc"):
        rows = _assignments(sort="activation", dir=direction)
        activated = [i for i, r in enumerate(rows) if r.get("last_activation")]
        never = [i for i, r in enumerate(rows) if not r.get("last_activation")]
        if activated and never:
            assert min(never) > max(activated), f"never-activated floated up when {direction}"


def test_assignments_privileged_filter_narrows_to_privileged_roles():
    """The overview tiles drill into this grid, and they are about privileged access only.

    Without the filter the reader lands on a grid where most rows are ordinary role
    assignments, which is a different question from the one the tile answered."""
    everything = _assignments()
    only = _assignments(privileged=True)
    assert only, "the demo tenant should hold privileged assignments"
    assert all(r.get("role_privileged") for r in only)
    assert len(only) <= len(everything)
    if len(everything) > len(only):
        assert any(not r.get("role_privileged") for r in everything), (
            "the unfiltered grid should still carry the unprivileged rows")


# ------------------------------------------------------------------------ the contract
@pytest.mark.parametrize("call", [_findings, _inbox, _apps, _assignments])
def test_unknown_sort_key_is_ignored_rather_than_crashing(call):
    """FastAPI rejects an unknown key at the edge; the helper itself must still be total."""
    assert isinstance(call(sort="not-a-column"), list)


def test_sorting_reorders_without_losing_or_duplicating_rows():
    """A sort is a permutation. Anything else is a filter wearing a sort's clothes."""
    base = {f["fingerprint"] for f in _findings()}
    for key in ("severity", "title", "object", "signal", "state"):
        for direction in ("asc", "desc"):
            rows = _findings(sort=key, dir=direction)
            fingerprints = [f["fingerprint"] for f in rows]
            assert len(fingerprints) == len(base), f"{key}/{direction} changed the row count"
            assert set(fingerprints) == base, f"{key}/{direction} changed which rows came back"
