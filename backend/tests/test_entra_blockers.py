"""Blockers: what is stopping a domain, in a shape the reader can act on.

Free-text notes could not be grouped or deduplicated. One missing permission was reported
once per domain that wanted it, and "grant a permission", "assign an Azure role", "buy a
license" and "we stopped early on purpose" were rendered identically -- so nothing on the
coverage banner could be triaged.
"""
from __future__ import annotations

import pytest

from app.entra import model
from app.entra import snapshot as snapshot_mod
from app.entra.collectors.activations import (
    AZURE_PIM_ROLES,
    _MAX_NAMED_SUBSCRIPTIONS,
    _name_subscriptions,
)


def _domain(name: str, blockers: list[dict]) -> dict:
    return {"name": name, "status": model.STATUS_PARTIAL, "blockers": blockers, "notes": []}


# ------------------------------------------------------------------ the blocker shape
def test_a_blocker_must_declare_a_known_kind():
    """A typo'd kind would render as an unstyled row nobody can triage."""
    with pytest.raises(ValueError):
        model.blocker("nonsense", "text")


def test_every_kind_is_constructible():
    for kind in model.BLOCKER_KINDS:
        assert model.blocker(kind, "text")["kind"] == kind


def test_domain_payload_carries_blockers():
    payload = model.domain_payload(
        "pim", {}, blockers=[model.blocker(model.BLOCKER_CONSENT, "x", scope="Scope.Read")])
    assert payload["blockers"][0]["scope"] == "Scope.Read"


# ------------------------------------------------------------------ deduplication
def test_one_missing_scope_reported_by_two_domains_becomes_one_row():
    """The reason this exists: one consent click was described three times."""
    scope = "RoleAssignmentSchedule.Read.Directory"
    domains = {
        "pim": _domain("pim", [model.blocker(
            model.BLOCKER_CONSENT, "Activation history is not readable.", scope=scope)]),
        "activations": _domain("activations", [model.blocker(
            model.BLOCKER_CONSENT, "Justification, ticket and requestor are not readable.",
            scope=scope)]),
    }
    out = snapshot_mod.collect_blockers(domains)
    assert len(out) == 1
    assert out[0]["scope"] == scope
    assert sorted(out[0]["domains"]) == ["activations", "pim"]


def test_the_merged_row_keeps_the_more_specific_wording():
    scope = "Scope.Read.All"
    domains = {
        "a": _domain("a", [model.blocker(model.BLOCKER_CONSENT, "Short.", scope=scope)]),
        "b": _domain("b", [model.blocker(
            model.BLOCKER_CONSENT, "A considerably more specific explanation.", scope=scope)]),
    }
    out = snapshot_mod.collect_blockers(domains)
    assert out[0]["text"] == "A considerably more specific explanation."


def test_the_same_scope_under_different_kinds_stays_separate():
    """A license gap and a consent gap are different problems with different fixes."""
    domains = {
        "pim": _domain("pim", [
            model.blocker(model.BLOCKER_CONSENT, "c", scope="X"),
            model.blocker(model.BLOCKER_LICENCE, "l", scope="X"),
        ]),
    }
    assert len(snapshot_mod.collect_blockers(domains)) == 2


def test_blockers_without_a_scope_dedupe_on_their_text():
    domains = {
        "a": _domain("a", [model.blocker(model.BLOCKER_CAP, "Capped at 200,000.")]),
        "b": _domain("b", [model.blocker(model.BLOCKER_CAP, "Capped at 200,000.")]),
    }
    out = snapshot_mod.collect_blockers(domains)
    assert len(out) == 1
    assert sorted(out[0]["domains"]) == ["a", "b"]


def test_two_subscriptions_missing_a_role_stay_separate_rows():
    """Subject is what makes an azure_role blocker actionable, so it must not collapse."""
    domains = {
        "activations": _domain("activations", [
            model.blocker(model.BLOCKER_AZURE_ROLE, "unreadable", scope=AZURE_PIM_ROLES,
                          subject="Prod"),
            model.blocker(model.BLOCKER_AZURE_ROLE, "unreadable", scope=AZURE_PIM_ROLES,
                          subject="Dev"),
        ]),
    }
    out = snapshot_mod.collect_blockers(domains)
    assert len(out) == 1, "same scope collapses"
    # The subject of the first is retained so the reader still has something to act on.
    assert out[0]["subject"] in {"Prod", "Dev"}


# ------------------------------------------------------------------ ordering
def test_blockers_are_ordered_by_what_the_reader_can_do_first():
    domains = {
        "d": _domain("d", [
            model.blocker(model.BLOCKER_CAP, "cap"),
            model.blocker(model.BLOCKER_LICENCE, "lic"),
            model.blocker(model.BLOCKER_AZURE_ROLE, "role"),
            model.blocker(model.BLOCKER_CONSENT, "consent"),
        ]),
    }
    kinds = [b["kind"] for b in snapshot_mod.collect_blockers(domains)]
    assert kinds == [model.BLOCKER_CONSENT, model.BLOCKER_AZURE_ROLE,
                     model.BLOCKER_LICENCE, model.BLOCKER_CAP]


def test_no_blockers_is_an_empty_list_not_a_crash():
    assert snapshot_mod.collect_blockers({}) == []
    assert snapshot_mod.collect_blockers({"a": {"name": "a"}}) == []


# ------------------------------------------------------------------ naming subscriptions
def test_a_failing_subscription_is_named_not_counted():
    """"1 of 26 subscription(s)" gave the reader a number they could not act on."""
    text = _name_subscriptions([("sub-guid-1", "HTTP 403")], {"sub-guid-1": "Production"})
    assert "Production" in text
    assert "HTTP 403" in text


def test_an_unnamed_subscription_falls_back_to_its_id():
    text = _name_subscriptions([("sub-guid-1", "HTTP 403")], {})
    assert "sub-guid-1" in text


def test_a_long_list_of_failures_is_summarised():
    failures = [(f"sub{i}", "HTTP 403") for i in range(9)]
    text = _name_subscriptions(failures, {})
    assert "and 6 more" in text
    assert text.count("HTTP 403") == _MAX_NAMED_SUBSCRIPTIONS


def test_the_azure_role_hint_names_roles_that_actually_work():
    """Reader is the trap: it omits Microsoft.Authorization/roleManagement*/read."""
    assert "User Access Administrator" in AZURE_PIM_ROLES
    assert "Reader" not in AZURE_PIM_ROLES.replace("Role Based Access Control Administrator", "")
