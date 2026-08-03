"""Conditional Access engine — resolution truth table, coverage matrix, conflicts,
break-glass detection.

These are the tests that decide whether the coverage matrix can be trusted. Each one
targets a place where a Conditional Access analysis is easy to get subtly wrong:
exclusions, "All" semantics, role templates, eligible role holders, and block precedence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.entra import ca_engine
from app.entra.ca_coverage import (
    CELL_COVERED,
    CELL_PARTIAL,
    CELL_REPORT_ONLY,
    CELL_UNCOVERED,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
GA_TEMPLATE = "62e90394-69f5-4237-9190-012177145e10"


def _user(uid, **kw):
    base = {"id": uid, "upn": f"{uid}@contoso.com", "display_name": uid, "user_type": "Member",
            "enabled": True, "on_prem_synced": False, "department": "IT", "job_title": "Eng",
            "mfa_registered": True, "signin_known": True, "last_signin": (NOW - timedelta(days=1)).isoformat()}
    base.update(kw)
    return base


def _policy(pid, name="P", state="enabled", conditions=None, grant=None, session=None, modified=None):
    c = {
        "include_users": [], "exclude_users": [], "include_groups": [], "exclude_groups": [],
        "include_roles": [], "exclude_roles": [], "include_guests": [], "exclude_guests": [],
        "include_apps": ["All"], "exclude_apps": [], "user_actions": [], "auth_contexts": [],
        "client_app_types": ["all"], "platforms_include": [], "platforms_exclude": [],
        "locations_include": [], "locations_exclude": [], "device_filter_mode": "",
        "device_filter_rule": "", "sign_in_risk": [], "user_risk": [], "service_principal_risk": [],
        "client_applications": {"include_service_principals": [], "exclude_service_principals": []},
    }
    c.update(conditions or {})
    g = {"operator": "OR", "controls": ["mfa"], "custom_controls": [], "terms_of_use": [],
         "auth_strength_id": "", "auth_strength_name": "", "present": True}
    g.update(grant or {})
    s = {"sign_in_frequency": False, "persistent_browser": False, "app_enforced_restrictions": False,
         "cloud_app_security": False, "continuous_access_evaluation": "", "present": False,
         "sign_in_frequency_value": None, "sign_in_frequency_type": "", "persistent_browser_mode": ""}
    s.update(session or {})
    return {"id": pid, "display_name": name, "state": state,
            "created_at": (NOW - timedelta(days=400)).isoformat(),
            "modified_at": modified or (NOW - timedelta(days=10)).isoformat(),
            "conditions": c, "grant": g, "session": s}


def _snapshot(users, policies, *, group_members=None, roles=None, sps=None):
    return {
        "people": {"users": users, "groups": [], "capabilities": {}, "counts": {}},
        "roles": roles or {"definitions": [], "assignments": [], "group_derived": [], "eligible": []},
        "apps": {"applications": [], "service_principals": sps or [], "counts": {}},
        "ca": {"policies": policies, "named_locations": [], "auth_strengths": [], "auth_contexts": [],
               "group_members": group_members or {}, "counts": {}},
    }


def _norm(snap):
    return ca_engine.normalize_policies(snap)


# ================================================================= resolution truth table
def test_all_users_resolves_to_every_enabled_user():
    snap = _snapshot([_user("u1"), _user("u2"), _user("u3", enabled=False)],
                     [_policy("p1", conditions={"include_users": ["All"]})])
    p = _norm(snap)[0]
    assert set(p["effective_ids"]) == {"u1", "u2"}      # disabled accounts are not counted
    assert p["include_all_users"] is True


def test_all_users_includes_guests():
    """Most coverage gaps are guest gaps, so 'All' must not quietly mean 'members'."""
    snap = _snapshot([_user("u1"), _user("g1", user_type="Guest")],
                     [_policy("p1", conditions={"include_users": ["All"]})])
    assert set(_norm(snap)[0]["effective_ids"]) == {"u1", "g1"}


def test_guests_or_external_users_token_resolves_to_guests_only():
    snap = _snapshot([_user("u1"), _user("g1", user_type="Guest")],
                     [_policy("p1", conditions={"include_users": ["GuestsOrExternalUsers"]})])
    assert set(_norm(snap)[0]["effective_ids"]) == {"g1"}


def test_none_token_resolves_to_nobody():
    snap = _snapshot([_user("u1")], [_policy("p1", conditions={"include_users": ["None"]})])
    assert _norm(snap)[0]["effective_ids"] == []


def test_groups_expand_through_transitive_membership():
    snap = _snapshot([_user("u1"), _user("u2"), _user("u3")],
                     [_policy("p1", conditions={"include_groups": ["g-eng"]})],
                     group_members={"g-eng": ["u1", "u2"]})
    assert set(_norm(snap)[0]["effective_ids"]) == {"u1", "u2"}


def test_exclusion_always_wins():
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_users": ["All"], "exclude_users": ["u2"]})])
    p = _norm(snap)[0]
    assert set(p["effective_ids"]) == {"u1"}
    assert set(p["excluded_ids"]) == {"u2"}


def test_group_exclusion_wins_over_user_inclusion():
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_users": ["u1", "u2"],
                                                "exclude_groups": ["g-x"]})],
                     group_members={"g-x": ["u2"]})
    assert set(_norm(snap)[0]["effective_ids"]) == {"u1"}


def test_include_roles_uses_the_role_template_id_not_the_definition_id():
    """Mapping the wrong id silently yields an empty role set and a policy that looks like
    it protects nobody."""
    roles = {
        "definitions": [{"id": "rd-ga", "template_id": GA_TEMPLATE, "display_name": "Global Administrator",
                         "tier": "tier0", "privileged": True}],
        "assignments": [{"role_id": "rd-ga", "principal_id": "u1", "role_privileged": True,
                         "principal_type": "User", "role_name": "Global Administrator"}],
        "group_derived": [], "eligible": [],
    }
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_roles": [GA_TEMPLATE]})], roles=roles)
    assert set(_norm(snap)[0]["effective_ids"]) == {"u1"}


def test_role_scoped_policy_covers_eligible_holders_too():
    """A policy targeting a role covers PIM-eligible holders, which the portal does not show."""
    roles = {
        "definitions": [{"id": "rd-ga", "template_id": GA_TEMPLATE, "display_name": "Global Administrator",
                         "tier": "tier0", "privileged": True}],
        "assignments": [],
        "group_derived": [],
        "eligible": [{"role_id": "rd-ga", "principal_id": "u2", "role_privileged": True,
                      "principal_type": "User", "role_name": "Global Administrator"}],
    }
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_roles": [GA_TEMPLATE]})], roles=roles)
    assert set(_norm(snap)[0]["effective_ids"]) == {"u2"}


# ============================================================================ controls
def test_block_control_is_detected():
    snap = _snapshot([_user("u1")], [_policy("p1", conditions={"include_users": ["All"]},
                                             grant={"controls": ["block"]})])
    p = _norm(snap)[0]
    assert p["is_block"] is True
    assert "block" in p["controls"]


def test_authentication_strength_implies_mfa_and_can_imply_phishing_resistance():
    snap = _snapshot([_user("u1")], [_policy("p1", conditions={"include_users": ["All"]},
                                             grant={"controls": [], "auth_strength_id": "as-1"})])
    snap["ca"]["auth_strengths"] = [{"id": "as-1", "display_name": "Phishing-resistant MFA",
                                     "combinations": ["fido2"]}]
    p = _norm(snap)[0]
    assert "mfa" in p["controls"] and "phishing_resistant" in p["controls"]


def test_legacy_auth_block_requires_targeting_only_legacy_client_apps():
    blocking = _policy("p1", conditions={"include_users": ["All"],
                                         "client_app_types": ["exchangeActiveSync", "other"]},
                       grant={"controls": ["block"]})
    broad = _policy("p2", conditions={"include_users": ["All"], "client_app_types": ["all"]},
                    grant={"controls": ["block"]})
    snap = _snapshot([_user("u1")], [blocking, broad])
    a, b = _norm(snap)
    assert a["blocks_legacy"] is True
    assert b["blocks_legacy"] is False


# ==================================================================== coverage matrix
def _coverage(snap):
    policies = _norm(snap)
    bg = ca_engine.detect_breakglass(policies, snap, {}, now=NOW)
    cohorts = ca_engine.build_cohorts(snap, set(bg["confirmed_ids"]))
    return ca_engine.build_coverage(policies, cohorts, snap), cohorts


def _cell(coverage, cohort, app_class="all_cloud_apps", control="mfa"):
    row = next(r for r in coverage["matrix"] if r["cohort"] == cohort)
    return row["cells"][f"{app_class}|{control}"]


def test_cell_is_enforced_only_when_the_whole_cohort_is_covered():
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_users": ["All"]})])
    coverage, _ = _coverage(snap)
    assert _cell(coverage, "members")["state"] == CELL_COVERED


def test_cell_is_partial_when_some_of_the_cohort_is_excluded():
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", conditions={"include_users": ["All"], "exclude_users": ["u2"]})])
    coverage, _ = _coverage(snap)
    cell = _cell(coverage, "members")
    assert cell["state"] == CELL_PARTIAL
    assert cell["users_covered"] == 1 and cell["users_total"] == 2
    assert cell["uncovered_sample"] == ["u2"]


def test_report_only_policies_never_count_as_enforced():
    snap = _snapshot([_user("u1")],
                     [_policy("p1", state="enabledForReportingButNotEnforced",
                              conditions={"include_users": ["All"]})])
    coverage, _ = _coverage(snap)
    assert _cell(coverage, "members")["state"] == CELL_REPORT_ONLY


def test_disabled_policies_protect_nobody():
    snap = _snapshot([_user("u1")], [_policy("p1", state="disabled",
                                             conditions={"include_users": ["All"]})])
    coverage, _ = _coverage(snap)
    assert _cell(coverage, "members")["state"] == CELL_UNCOVERED


def test_policy_scoped_to_one_app_does_not_cover_all_cloud_apps():
    snap = _snapshot([_user("u1")],
                     [_policy("p1", conditions={"include_users": ["All"], "include_apps": ["some-app"]})])
    coverage, _ = _coverage(snap)
    assert _cell(coverage, "members", app_class="all_cloud_apps")["state"] == CELL_UNCOVERED


def test_headline_counts_only_enforced_policies_and_applies_exclusions():
    snap = _snapshot(
        [_user("u1"), _user("u2"), _user("u3")],
        [_policy("p1", conditions={"include_users": ["All"], "exclude_users": ["u3"]}),
         _policy("p2", state="disabled", conditions={"include_users": ["All"]})],
    )
    coverage, _ = _coverage(snap)
    headline = coverage["headline"]
    assert headline["uncovered_users"] == 1
    assert headline["total_users"] == 3
    assert headline["assumptions"]


# ================================================================= conflict detection
def test_policy_with_no_users_is_reported_as_no_effect():
    snap = _snapshot([_user("u1")], [_policy("p1", conditions={"include_users": ["None"]})])
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    assert any(c["kind"] == "policy_no_effect" for c in conflicts)


def test_block_beats_grant_is_reported_as_a_contradiction():
    """Entra evaluates every policy and a block always wins — administrators routinely
    assume an ordering that does not exist."""
    snap = _snapshot(
        [_user("u1")],
        [_policy("p-block", name="Block all", conditions={"include_users": ["All"]},
                 grant={"controls": ["block"]}),
         _policy("p-grant", name="Require MFA", conditions={"include_users": ["All"]})],
    )
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    contradiction = next(c for c in conflicts if c["kind"] == "conflicting_block_grant")
    assert contradiction["policy_name"] == "Require MFA"
    assert contradiction["other_name"] == "Block all"
    assert contradiction["affected"] == 1


def test_a_conditional_block_is_not_reported_as_an_absolute_contradiction():
    """'Block Countries' only blocks sessions from those countries. Reporting it as
    "the grant can never be satisfied" made four healthy policies look broken on a live
    tenant and buried the one real contradiction."""
    snap = _snapshot(
        [_user("u1")],
        [_policy("p-block", name="Block Countries",
                 conditions={"include_users": ["All"], "locations_include": ["loc-cn"]},
                 grant={"controls": ["block"]}),
         _policy("p-grant", name="Require MFA", conditions={"include_users": ["All"]})],
    )
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    assert not [c for c in conflicts if c["kind"] == "conflicting_block_grant"]


def test_subsumed_policy_is_reported_as_redundant():
    snap = _snapshot(
        [_user("u1"), _user("u2")],
        [_policy("p-wide", name="Wide", conditions={"include_users": ["All"]}),
         _policy("p-narrow", name="Narrow", conditions={"include_users": ["u1"]})],
    )
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    assert any(c["kind"] == "redundant_policy" and c["policy_name"] == "Narrow" for c in conflicts)


def test_privileged_exclusion_is_detected():
    snap = _snapshot([_user("u1"), _user("admin")],
                     [_policy("p1", conditions={"include_users": ["All"], "exclude_users": ["admin"]})])
    conflicts = ca_engine.detect_conflicts(_norm(snap), {"admin"})
    hit = next(c for c in conflicts if c["kind"] == "exclusion_privileged")
    assert hit["sample"] == ["admin"]


def test_unreachable_condition_is_detected():
    snap = _snapshot([_user("u1")],
                     [_policy("p1", conditions={"include_users": ["All"],
                                                "platforms_include": ["iOS"],
                                                "platforms_exclude": ["iOS"]})])
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    assert any(c["kind"] == "unreachable_condition" for c in conflicts)


def test_no_false_conflicts_on_a_clean_policy_set():
    snap = _snapshot(
        [_user("u1"), _user("g1", user_type="Guest")],
        [_policy("p1", name="MFA for members", conditions={"include_users": ["u1"]}),
         _policy("p2", name="MFA for guests", conditions={"include_users": ["GuestsOrExternalUsers"]})],
    )
    conflicts = ca_engine.detect_conflicts(_norm(snap), set())
    assert conflicts == []


# ======================================================================== break-glass
def _bg_snapshot():
    roles = {
        "definitions": [{"id": "rd-ga", "template_id": GA_TEMPLATE, "display_name": "Global Administrator",
                         "tier": "tier0", "privileged": True}],
        "assignments": [{"role_id": "rd-ga", "principal_id": "bg1", "role_privileged": True,
                         "principal_type": "User", "role_name": "Global Administrator"}],
        "group_derived": [], "eligible": [],
    }
    users = [
        _user("u1"),
        _user("bg1", upn="bg-emergency-01@contoso.com", display_name="Break Glass 01",
              department="", job_title="", mfa_registered=False, last_signin=""),
    ]
    policies = [_policy("p1", name="MFA all", conditions={"include_users": ["All"],
                                                          "exclude_users": ["bg1"]})]
    return _snapshot(users, policies, roles=roles)


def test_break_glass_candidate_is_detected_but_not_auto_confirmed():
    snap = _bg_snapshot()
    bg = ca_engine.detect_breakglass(_norm(snap), snap, {}, now=NOW)
    names = {c["user_id"] for c in bg["candidates"]}
    assert "bg1" in names
    candidate = next(c for c in bg["candidates"] if c["user_id"] == "bg1")
    # Detection is heuristic: it must never silently classify and then exclude the account.
    assert candidate["confirmed"] is None
    assert bg["confirmed_count"] == 0
    assert "confirmed" in bg["heuristic_note"]


def test_ordinary_admin_is_not_flagged_as_break_glass():
    snap = _bg_snapshot()
    bg = ca_engine.detect_breakglass(_norm(snap), snap, {}, now=NOW)
    assert "u1" not in {c["user_id"] for c in bg["candidates"]}


def test_a_guest_is_never_a_break_glass_candidate():
    """Guests score on every generic signal — cloud-only, no department, no recent
    interactive sign-in, excluded from a policy — so on a tenant with a real B2B population
    they flooded the list and buried the one genuine emergency account."""
    snap = _bg_snapshot()
    snap["people"]["users"].append(_user(
        "guest1", upn="someone_partner.com#EXT#@contoso.onmicrosoft.com",
        display_name="Partner Person", user_type="Guest", department="", job_title="",
        last_signin="", on_prem_synced=False))
    snap["ca"]["policies"] = [_policy("p1", name="MFA all",
                                      conditions={"include_users": ["All"],
                                                  "exclude_users": ["bg1", "guest1"]})]
    bg = ca_engine.detect_breakglass(_norm(snap), snap, {}, now=NOW)
    ids = {c["user_id"] for c in bg["candidates"]}
    assert "guest1" not in ids
    assert "bg1" in ids, "the real emergency account must still be found"


def test_confirmed_break_glass_captured_by_a_policy_is_a_lockout_risk():
    """The most expensive Conditional Access mistake: the account that exists to recover
    the tenant is caught by the policy that broke it."""
    snap = _bg_snapshot()
    snap["ca"]["policies"] = [_policy("p1", name="MFA all", conditions={"include_users": ["All"]})]
    bg = ca_engine.detect_breakglass(_norm(snap), snap, {"bg1": {"confirmed": True}}, now=NOW)
    over = bg["over_covered"]
    assert [c["user_id"] for c in over] == ["bg1"]
    assert over[0]["mfa_registered"] is False


# ====================================================================== fingerprinting
def test_fingerprint_ignores_modified_timestamp():
    a = _policy("p1", modified=(NOW - timedelta(days=1)).isoformat())
    b = _policy("p1", modified=(NOW - timedelta(days=300)).isoformat())
    assert ca_engine.policy_fingerprint(a) == ca_engine.policy_fingerprint(b)


def test_fingerprint_changes_when_an_exclusion_is_added():
    a = _policy("p1", conditions={"include_users": ["All"]})
    b = _policy("p1", conditions={"include_users": ["All"], "exclude_groups": ["g1"]})
    assert ca_engine.policy_fingerprint(a) != ca_engine.policy_fingerprint(b)


# ============================================================================ analyse
def test_analyse_is_deterministic():
    snap = _bg_snapshot()
    a = ca_engine.analyse(snap, now=NOW)
    b = ca_engine.analyse(snap, now=NOW)
    assert a["counts"] == b["counts"]
    assert [c["kind"] for c in a["conflicts"]] == [c["kind"] for c in b["conflicts"]]
    assert a["coverage"]["headline"] == b["coverage"]["headline"]
