"""Guest (B2B) hygiene derivation.

Every test here exists because the corresponding mistake produces a CONFIDENT WRONG ANSWER
rather than an error: an invitation date that silently becomes an acceptance date, a partner
org that resolves to your own tenant, a refresh token that reads as a person, and an
unmeasured account that reads as an unused one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.entra import guests

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _guest(**over: Any) -> dict[str, Any]:
    base = {
        "id": "g1", "user_type": "Guest", "enabled": True,
        "display_name": "Ada Lovelace",
        "upn": "ada_contoso.com#EXT#@host.onmicrosoft.com",
        "mail": "ada@contoso.com",
        "external_user_state": "Accepted",
        "external_state_changed_at": _ago(200),
        "created_at": _ago(400),
        "last_signin": _ago(3),
        "last_noninteractive_signin": _ago(2),
        "signin_known": True,
    }
    base.update(over)
    return base


def _people(*users: dict[str, Any]) -> dict[str, Any]:
    return {"users": list(users)}


# ============================================================ the organisation of a guest
def test_the_guest_domain_is_the_partner_not_the_host_tenant():
    """The UPN suffix is ALWAYS the host tenant. Keying on it reports every guest in the
    directory as belonging to your own company."""
    u = _guest(mail="", upn="ada_contoso.com#EXT#@host.onmicrosoft.com")
    assert guests.guest_domain(u) == "contoso.com"


def test_mail_wins_over_the_mangled_upn():
    u = _guest(mail="ada@fabrikam.co.uk", upn="ada_contoso.com#EXT#@host.onmicrosoft.com")
    assert guests.guest_domain(u) == "fabrikam.co.uk"


def test_the_host_onmicrosoft_domain_is_never_reported_as_a_partner():
    """A guest-typed account with a plain host UPN must not create a partner org out of your
    own tenant \u2014 it would top the rollup on every tenant."""
    u = _guest(mail="", upn="someone@host.onmicrosoft.com")
    assert guests.guest_domain(u) == ""


def test_an_underscore_in_the_local_part_does_not_break_the_split():
    u = _guest(mail="", upn="ada_b_lovelace_contoso.com#EXT#@host.onmicrosoft.com")
    assert guests.guest_domain(u) == "contoso.com"


@pytest.mark.parametrize("domain,expected", [
    ("contoso.com", guests.CLASS_CORPORATE),
    ("gmail.com", guests.CLASS_CONSUMER),
    ("outlook.com", guests.CLASS_CONSUMER),
    # Synthetic hosts under reserved suffixes — these exercise the TLD rule without naming a
    # real counterparty of any tenant this product has been pointed at.
    ("agency.example.gov", guests.CLASS_GOVERNMENT),
    ("base.example.mil", guests.CLASS_GOVERNMENT),
    ("campus.example.edu", guests.CLASS_EDUCATION),
    ("", guests.CLASS_UNRESOLVED),
])
def test_domains_are_classified_for_the_review_conversation(domain, expected):
    """A consumer mailbox cannot be de-provisioned by any partner admin, which is a different
    governance conversation from a corporate counterparty."""
    assert guests.classify_domain(domain) == expected


# ============================================================ the invitation date trap
def test_the_invite_date_survives_acceptance():
    """`externalUserStateChangeDateTime` means "invited at" while pending and silently becomes
    "accepted at" on acceptance. Reading it for an accepted guest reports the wrong date and
    quietly makes every accepted guest look recently invited."""
    u = _guest(external_user_state="Accepted",
               created_at=_ago(400), external_state_changed_at=_ago(200))
    assert guests.invited_at(u) == _ago(400)
    assert guests.accepted_at(u) == _ago(200)


def test_a_pending_guest_has_no_acceptance_date():
    """While pending, the state-change stamp holds the INVITE time. Returning it as an
    acceptance would report an acceptance that never happened."""
    u = _guest(external_user_state="PendingAcceptance",
               created_at=_ago(90), external_state_changed_at=_ago(90))
    assert guests.accepted_at(u) == ""
    assert guests.invited_at(u) == _ago(90)


# ============================================================ lifecycle
def test_the_five_states_partition_the_population():
    """Overlapping states would double-count the funnel and make the totals disagree with the
    grid underneath them."""
    people = _people(
        _guest(id="p", external_user_state="PendingAcceptance"),
        _guest(id="n", last_signin="", last_noninteractive_signin=""),
        _guest(id="d", last_signin=_ago(200), last_noninteractive_signin=_ago(200)),
        _guest(id="a", last_signin=_ago(1), last_noninteractive_signin=_ago(1)),
        _guest(id="u", signin_known=False),
    )
    rows = guests.project_all(people, now=NOW, stale_days=90)
    counts = guests.funnel(rows)
    assert counts["invited"] == 5
    assert (counts["pending"] + counts["never_used"] + counts["dormant"]
            + counts["active"] + counts["not_measured"]) == counts["invited"]
    by_id = {r["id"]: r["lifecycle"] for r in rows}
    assert by_id == {
        "p": guests.STATE_PENDING,
        "n": guests.STATE_NEVER_USED,
        "d": guests.STATE_DORMANT,
        "a": guests.STATE_ACTIVE,
        "u": guests.STATE_UNKNOWN,
    }


def test_an_unmeasured_guest_is_never_called_dormant():
    """"We did not look" and "nobody uses this" are opposite facts. Grading an account whose
    sign-in was never collected is how real access gets revoked for the wrong reason."""
    u = _guest(signin_known=False, last_signin="", last_noninteractive_signin="")
    assert guests.lifecycle(u, now=NOW, stale_days=90) == guests.STATE_UNKNOWN


def test_pending_beats_sign_in_state():
    """A pending invite cannot have been used, so the state must not depend on sign-in data
    that will always be empty for it."""
    u = _guest(external_user_state="PendingAcceptance", signin_known=False)
    assert guests.lifecycle(u, now=NOW, stale_days=90) == guests.STATE_PENDING


def test_the_guest_threshold_is_honoured_not_the_employee_one():
    u = _guest(last_signin=_ago(45), last_noninteractive_signin=_ago(45))
    assert guests.lifecycle(u, now=NOW, stale_days=90) == guests.STATE_ACTIVE
    assert guests.lifecycle(u, now=NOW, stale_days=30) == guests.STATE_DORMANT


# ============================================================ interactive vs token
def test_a_refresh_token_is_not_a_person():
    """Measured live: 517 of 1,018 apparently-active guests had NO interactive sign-in in 30
    days. Reporting only the combined figure hides half the dormant population."""
    u = _guest(last_signin=_ago(300), last_noninteractive_signin=_ago(1))
    row = guests.project(u, now=NOW, stale_days=90)
    assert row["last_human_days_ago"] == 300
    assert row["last_any_days_ago"] == 1
    # The identity is live, so the lifecycle is active \u2014 but the human column tells the truth.
    assert row["lifecycle"] == guests.STATE_ACTIVE


def test_last_any_signin_includes_the_successful_stamp():
    u = _guest(last_signin=_ago(50), last_noninteractive_signin=_ago(60),
               last_successful_signin=_ago(2))
    assert guests.last_any_signin(u) == _ago(2)


# ============================================================ rollups
def test_disabled_guests_are_counted_not_dropped():
    """A disabled guest keeps its group memberships and app assignments \u2014 "disabled but still
    assigned" is a finding, and filtering them out here makes it unreachable."""
    people = _people(_guest(id="on"), _guest(id="off", enabled=False))
    rows = guests.project_all(people, now=NOW, stale_days=90)
    assert len(rows) == 2
    doms = guests.by_domain(rows)
    assert doms[0]["guests"] == 2
    assert doms[0]["enabled"] == 1
    assert doms[0]["disabled"] == 1


def test_the_domain_rollup_leads_with_the_biggest_exposure():
    people = _people(
        _guest(id="a1", mail="a@big.com"), _guest(id="a2", mail="b@big.com"),
        _guest(id="a3", mail="c@big.com"), _guest(id="b1", mail="d@small.com"),
    )
    rows = guests.project_all(people, now=NOW, stale_days=90)
    doms = guests.by_domain(rows)
    assert [d["domain"] for d in doms] == ["big.com", "small.com"]
    assert doms[0]["guests"] == 3


def test_a_resolved_partner_carries_its_real_name():
    people = _people(_guest(mail="a@fabrikam.com"))
    people["guest_domain_tenants"] = {
        "fabrikam.com": {"tenant_id": "t-1", "display_name": "Fabrikam"}}
    s = guests.summarise(people, now=NOW, stale_days=90)
    assert s["domains"][0]["partner_name"] == "Fabrikam"
    assert s["domains"][0]["partner_tenant_id"] == "t-1"


# ============================================================ partner governance
def test_an_unreadable_partner_list_reports_unknown_not_ungoverned():
    """Saying 410 partners are ungoverned because we could not read the list would be the
    loudest false claim this screen could make."""
    doms = [{"domain": "contoso.com", "partner_tenant_id": "t-1"}]
    out = guests.annotate_partners(doms, {"known": False, "partners": []})
    assert out[0]["governance"] == "unknown"


def test_a_named_partner_is_reported_as_governed():
    doms = [{"domain": "contoso.com", "partner_tenant_id": "t-1"}]
    out = guests.annotate_partners(
        doms, {"known": True, "partners": [{"tenant_id": "t-1", "b2b_inbound_configured": True}]})
    assert out[0]["governance"] == "governed"


def test_a_partner_with_no_entry_inherits_the_default_and_says_so():
    doms = [{"domain": "contoso.com", "partner_tenant_id": "t-9"}]
    out = guests.annotate_partners(
        doms, {"known": True, "partners": [{"tenant_id": "t-1", "b2b_inbound_configured": True}]})
    assert out[0]["governance"] == "default_only"
    assert "default" in out[0]["governance_reason"]


def test_an_unresolved_domain_is_unknown_not_ungoverned():
    """A consumer domain has no partner tenant at all. That is not the same as a partner we
    failed to govern."""
    doms = [{"domain": "gmail.com", "partner_tenant_id": ""}]
    out = guests.annotate_partners(doms, {"known": True, "partners": []})
    assert out[0]["governance"] == "unknown"


# ============================================================ population selection
def test_only_guests_are_projected():
    people = _people(_guest(id="g"), {"id": "m", "user_type": "Member", "enabled": True})
    rows = guests.project_all(people, now=NOW, stale_days=90)
    assert [r["id"] for r in rows] == ["g"]


def test_summarise_reports_both_numbers_never_a_bare_ratio():
    people = _people(_guest(id="a"), _guest(id="b", signin_known=False))
    s = guests.summarise(people, now=NOW, stale_days=90)
    assert s["counts"]["invited"] == 2
    assert s["signin_measured"] == 1
