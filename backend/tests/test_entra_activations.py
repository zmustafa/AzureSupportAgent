"""Privileged activation sessions: collection, the ledger, and action attribution.

These cover the things that were actually wrong during the build, not just the happy path —
each of the regression tests below corresponds to a bug this feature shipped with once.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.entra import activation_actions, activations_ledger, cache
from app.entra.collectors import activations as coll


# --------------------------------------------------------------------------- time parsing
@pytest.mark.parametrize("text,expected_hour", [
    ("2026-07-24T14:56:43Z", 14),
    ("2026-07-24T14:56:43.972395+00:00", 14),
    # ARM emits seven fractional digits, which datetime.fromisoformat rejects outright. The
    # Azure PIM payload is full of these, so a naive parser silently dropped every window.
    ("2026-07-24T14:56:43.9723951+00:00", 14),
    ("2026-06-15T14:03:24.6169385+00:00", 14),
])
def test_arm_and_graph_timestamps_both_parse(text, expected_hour):
    parsed = coll.parse_time(text)
    assert parsed is not None
    assert parsed.hour == expected_hour
    assert parsed.tzinfo is not None


def test_unparseable_time_is_none_not_an_exception():
    assert coll.parse_time("") is None
    assert coll.parse_time("not a date") is None


# --------------------------------------------------------------------------- session shape
def test_a_session_derives_its_length_and_tier():
    row = coll.session(
        sid="x", plane="entra", source="entra_request", principal_id="p1",
        role_id="r1", role_name="Global Administrator",
        start="2026-07-31T04:00:00Z", end="2026-07-31T12:00:00Z")
    assert row["granted_hours"] == 8.0
    assert row["tier"] == "tier0"


def test_self_service_is_only_true_when_the_requestor_is_the_subject():
    same = coll.session(sid="a", plane="azure", source="azure_request", principal_id="p1",
                        role_id="r", requestor_id="p1")
    other = coll.session(sid="b", plane="azure", source="azure_request", principal_id="p1",
                         role_id="r", requestor_id="p2")
    none = coll.session(sid="c", plane="azure", source="azure_request", principal_id="p1",
                        role_id="r")
    assert same["self_service"] is True
    assert other["self_service"] is False
    # No requestor recorded is not evidence of a third party.
    assert none["self_service"] is False


@pytest.mark.parametrize("status,granted", [
    ("Provisioned", True), ("Granted", True), ("", True),
    ("Failed", False), ("Denied", False), ("PendingApproval", False), ("Canceled", False),
])
def test_only_provisioned_requests_count_as_privilege_granted(status, granted):
    """A failed request granted nothing. Counting it as an elevation would accuse someone of
    holding access they were refused."""
    assert coll.is_granted(status) is granted


@pytest.mark.parametrize("scope,kind", [
    ("/subscriptions/abc", "subscription"),
    ("/subscriptions/abc/resourceGroups/rg1", "resourceGroup"),
    ("/subscriptions/abc/resourceGroups/rg1/providers/Microsoft.Storage/x/y", "resource"),
    ("/providers/Microsoft.Management/managementGroups/mg1", "managementGroup"),
    ("", "unknown"),
])
def test_azure_scope_breadth_is_classified(scope, kind):
    """Breadth is what makes an Azure elevation dangerous — a management group covers every
    subscription beneath it."""
    assert coll._scope_kind(scope) == kind


# --------------------------------------------------------------------------- role names
def test_role_names_backfill_the_tier():
    """Schedule instances carry only a roleDefinitionId. Without resolving it, tier_of("")
    graded Global Administrator as tier-2 and every tier-0 signal stayed silent."""
    rows = [coll.session(sid="s", plane="entra", source="entra_instance",
                         principal_id="p", role_id="role-ga")]
    assert rows[0]["tier"] == "tier2"
    coll._apply_role_names(rows, {"role-ga": "Global Administrator"})
    assert rows[0]["role_name"] == "Global Administrator"
    assert rows[0]["tier"] == "tier0"


def test_role_names_never_overwrite_a_name_the_source_supplied():
    rows = [coll.session(sid="s", plane="azure", source="azure_request", principal_id="p",
                         role_id="r", role_name="Owner")]
    coll._apply_role_names(rows, {"r": "Something Else"})
    assert rows[0]["role_name"] == "Owner"


# --------------------------------------------------------------------------- dedupe
def test_the_richer_source_wins_when_both_describe_one_elevation():
    """A request and an instance for the same principal, role and window are ONE activation
    seen twice. The request carries the justification, so it must survive."""
    instance = coll.session(sid="entra:inst:1", plane="entra", source="entra_instance",
                            principal_id="p1", role_id="r1", start="2026-07-31T04:00:00Z",
                            end="2026-07-31T12:00:00Z", detail_known=False)
    request = coll.session(sid="entra:req:1", plane="entra", source="entra_request",
                           principal_id="p1", role_id="r1", start="2026-07-31T04:00:30Z",
                           end="2026-07-31T12:00:00Z", justification="patching")
    merged = coll._dedupe([instance, request])
    assert len(merged) == 1
    assert merged[0]["justification"] == "patching"


def test_different_planes_are_never_merged():
    a = coll.session(sid="1", plane="entra", source="entra_instance", principal_id="p",
                     role_id="r", start="2026-07-31T04:00:00Z")
    b = coll.session(sid="2", plane="azure", source="azure_request", principal_id="p",
                     role_id="r", start="2026-07-31T04:00:00Z")
    assert len(coll._dedupe([a, b])) == 2


# --------------------------------------------------------------------------- ledger
@pytest.fixture()
def _tenant(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    yield "t1"
    cache.clear_memo()


def test_the_ledger_outlives_the_source_window(_tenant):
    """Graph discards directory audits after 30 days and rejects a 90-day query outright, so
    without this the product could never answer a question older than a month."""
    old = coll.session(sid="s-old", plane="entra", source="entra_instance", principal_id="p",
                       role_id="r", start="2026-01-01T09:00:00Z")
    activations_ledger.append(_tenant, [old])

    # A later refresh no longer sees it — the source has forgotten.
    merged = activations_ledger.merge_with_live(_tenant, [])
    assert [r["id"] for r in merged] == ["s-old"]


def test_recording_the_same_session_twice_does_not_duplicate_it(_tenant):
    row = coll.session(sid="s1", plane="entra", source="entra_instance", principal_id="p",
                       role_id="r", start="2026-07-31T04:00:00Z")
    first = activations_ledger.append(_tenant, [row])
    second = activations_ledger.append(_tenant, [row])
    assert first["added"] == 1
    assert second["added"] == 0
    assert second["total"] == 1


def test_a_later_read_may_sharpen_a_session_but_never_blank_it(_tenant):
    """A live activation is read repeatedly before it expires. A source that cannot express
    justification must not erase one an earlier, richer source recorded."""
    rich = coll.session(sid="s1", plane="entra", source="entra_request", principal_id="p",
                        role_id="r", start="2026-07-31T04:00:00Z", justification="incident 42")
    poor = coll.session(sid="s1", plane="entra", source="entra_instance", principal_id="p",
                        role_id="r", start="2026-07-31T04:00:00Z",
                        end="2026-07-31T12:00:00Z", detail_known=False)
    activations_ledger.append(_tenant, [rich])
    activations_ledger.append(_tenant, [poor])
    kept = activations_ledger.read(_tenant)[0]
    assert kept["justification"] == "incident 42"
    assert kept["end"] == "2026-07-31T12:00:00Z"   # the newer, firmer value IS taken


def test_live_wins_over_the_ledger_for_a_session_still_in_the_window(_tenant):
    stale = coll.session(sid="s1", plane="entra", source="entra_instance", principal_id="p",
                         role_id="r", start="2026-07-31T04:00:00Z", status="Pending")
    activations_ledger.append(_tenant, [stale])
    live = coll.session(sid="s1", plane="entra", source="entra_instance", principal_id="p",
                        role_id="r", start="2026-07-31T04:00:00Z", status="Provisioned")
    merged = activations_ledger.merge_with_live(_tenant, [live])
    assert len(merged) == 1
    assert merged[0]["status"] == "Provisioned"


def test_the_ledger_reports_what_it_trimmed(_tenant, monkeypatch):
    """Unbounded growth is its own failure. Which end gets dropped is a deliberate choice and
    the fact that it happened has to be visible."""
    monkeypatch.setattr(activations_ledger, "MAX_SESSIONS", 3)
    rows = [coll.session(sid=f"s{i}", plane="entra", source="entra_instance",
                         principal_id="p", role_id="r",
                         start=f"2026-07-{10 + i:02d}T04:00:00Z") for i in range(5)]
    result = activations_ledger.append(_tenant, rows)
    assert result["trimmed"] == 2
    assert result["total"] == 3
    kept = {r["id"] for r in activations_ledger.read(_tenant)}
    assert kept == {"s2", "s3", "s4"}          # oldest dropped, newest retained
    assert activations_ledger.stats(_tenant)["trimmed"] == 2


def test_stats_describe_how_far_back_history_reaches(_tenant):
    rows = [coll.session(sid="a", plane="entra", source="entra_instance", principal_id="p",
                         role_id="r", start="2026-06-15T14:00:00Z"),
            coll.session(sid="b", plane="entra", source="entra_instance", principal_id="p",
                         role_id="r", start="2026-07-31T04:00:00Z")]
    activations_ledger.append(_tenant, rows)
    stats = activations_ledger.stats(_tenant)
    assert stats["total"] == 2
    assert stats["earliest"].startswith("2026-06-15")
    assert stats["latest"].startswith("2026-07-31")


# --------------------------------------------------------------------------- attribution
def test_an_action_needing_no_standing_role_is_attributed_to_the_elevation():
    assert activation_actions.classify("entra", [], [], True) == activation_actions.REQUIRED


def test_a_standing_admin_could_have_done_it_without_elevating():
    """The judgment this feature would be dangerous without: someone holding permanent
    Global Administrator did not need the activation to make the change."""
    assert activation_actions.classify(
        "entra", ["Global Administrator"], [], True) == activation_actions.POSSIBLE


def test_no_standing_picture_means_no_claim():
    """Guessing here would manufacture an accusation from missing data."""
    assert activation_actions.classify("azure", [], [], False) == activation_actions.UNKNOWN
    assert activation_actions.classify("azure", [], ["Owner"], True) == activation_actions.POSSIBLE
    assert activation_actions.classify("azure", [], [], True) == activation_actions.REQUIRED


def test_standing_roles_exclude_the_activation_itself():
    """A role row flagged ``activated`` IS the elevation. Counting it as standing power would
    make every action look like it needed no elevation."""
    data = {"roles": {"assignments": [
        {"principal_id": "p1", "role_name": "Global Administrator",
         "role_privileged": True, "activated": True},
    ], "group_derived": []}}
    assert activation_actions._standing_privileged(data, "p1") == []

    data["roles"]["assignments"][0]["activated"] = False
    assert activation_actions._standing_privileged(data, "p1") == ["Global Administrator"]


def test_actions_are_read_over_a_padded_window():
    """Audit events land seconds to minutes late, and the elevation is usable the instant it
    is granted, so an exact window drops real actions at both ends."""
    assert activation_actions.WINDOW_PAD_MINUTES > 0


def test_a_session_with_no_start_cannot_be_traced():
    session = coll.session(sid="s", plane="entra", source="entra_instance",
                           principal_id="p", role_id="r")
    result = asyncio.run(activation_actions.collect_actions("t", None, session, {}))
    assert result["actions"] == []
    assert any("no start time" in n for n in result["notes"])


def test_a_live_session_reads_up_to_now_and_says_so():
    session = coll.session(sid="s", plane="entra", source="entra_instance", principal_id="p",
                           role_id="r",
                           start=(datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
                               "%Y-%m-%dT%H:%M:%SZ"))
    result = asyncio.run(activation_actions.collect_actions("t", None, session, {}))
    assert any("no recorded end" in n for n in result["notes"])
