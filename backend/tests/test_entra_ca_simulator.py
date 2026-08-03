"""Conditional Access Change Simulator.

The plan calls this the place correctness is won or lost, because a wrong
*"nobody gets blocked"* is worse than having no simulator at all. The tests below are
therefore weighted toward the distinctions that cause real incidents:

* challenged (friction) versus blocked-effective (a hard block),
* block beating any grant, whatever the order,
* protection LOST when a policy is disabled or deleted,
* break-glass accounts surfacing first and never being sampled away.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.entra import ca_engine, ca_simulator
from app.entra.ca_simulator import (
    BLOCKED,
    BLOCKED_EFFECTIVE,
    CHALLENGED,
    GRANTED,
    SignInContext,
    SimPrincipal,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
BROWSER = SignInContext("browser", "Browser", client_app="browser")
LEGACY = SignInContext("legacy", "Legacy", client_app="exchangeActiveSync")
COMPLIANT = SignInContext("compliant", "Compliant device", client_app="browser",
                          device_compliant=True, device_hybrid_joined=True)


def _policy(pid, *, name="P", state="enabled", controls=("mfa",), effective=(), operator="OR",
            app_classes=("all_cloud_apps",), conditions=None, is_block=False):
    base_conditions = {
        "include_users": ["All"], "exclude_users": [], "include_groups": [], "exclude_groups": [],
        "include_roles": [], "exclude_roles": [], "include_apps": ["All"], "exclude_apps": [],
        "client_app_types": ["all"], "platforms_include": [], "platforms_exclude": [],
        "locations_include": [], "locations_exclude": [], "sign_in_risk": [], "user_risk": [],
    }
    base_conditions.update(conditions or {})
    return {
        "id": pid, "display_name": name, "state": state,
        "conditions": base_conditions,
        "grant": {"operator": operator, "controls": list(controls), "auth_strength_id": ""},
        "controls": list(controls),
        "effective_ids": list(effective),
        "app_classes": list(app_classes),
        "is_enforced": state == "enabled",
        "is_block": is_block or "block" in controls,
        "is_report_only": state == "enabledForReportingButNotEnforced",
        "is_disabled": state == "disabled",
        "targets_all_apps": True,
    }


def _user(uid, *, mfa=True, phish=False, cohorts=("members",)):
    return SimPrincipal(id=uid, label=f"{uid}@contoso.com", kind="user",
                        mfa_registered=mfa, phishing_resistant=phish, cohorts=list(cohorts))


def _prep(policies):
    return ca_simulator._prepare(policies)  # noqa: SLF001 - internal by design


# ============================================================ the central distinction
def test_user_with_mfa_is_challenged_not_blocked():
    policies = _prep([_policy("p1", controls=["mfa"], effective=["u1"])])
    result = ca_simulator.evaluate(policies, _user("u1", mfa=True), BROWSER)
    assert result["verdict"] == CHALLENGED
    assert result["missing"] == []


def test_service_account_without_mfa_is_effectively_blocked():
    """The whole product value: 'requires MFA' is friction for a person and a hard block for
    an account that has no method registered."""
    policies = _prep([_policy("p1", controls=["mfa"], effective=["svc"])])
    result = ca_simulator.evaluate(policies, _user("svc", mfa=False), BROWSER)
    assert result["verdict"] == BLOCKED_EFFECTIVE
    assert result["missing"] == ["mfa"]


def test_workload_identity_can_never_satisfy_mfa():
    sp = SimPrincipal(id="sp1", label="CI-Deploy", kind="servicePrincipal",
                      mfa_registered=False, cohorts=["workload_identities"])
    policies = _prep([_policy("p1", controls=["mfa"], effective=["sp1"])])
    assert ca_simulator.evaluate(policies, sp, BROWSER)["verdict"] == BLOCKED_EFFECTIVE


def test_unknown_mfa_registration_does_not_invent_a_block():
    """When the registration report was unavailable we must not fabricate hard blocks —
    the result flags the assumption instead."""
    unknown = SimPrincipal(id="u1", label="u1", kind="user", mfa_registered=None)
    policies = _prep([_policy("p1", controls=["mfa"], effective=["u1"])])
    assert ca_simulator.evaluate(policies, unknown, BROWSER)["verdict"] == CHALLENGED
    assert unknown.mfa_unknown is True


def test_compliant_device_satisfies_a_device_control():
    policies = _prep([_policy("p1", controls=["compliant_device"], effective=["u1"])])
    assert ca_simulator.evaluate(policies, _user("u1"), COMPLIANT)["verdict"] == CHALLENGED
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == BLOCKED_EFFECTIVE


# ============================================================== precedence + matching
def test_block_beats_any_grant():
    policies = _prep([
        _policy("p-grant", name="Grant", controls=["mfa"], effective=["u1"]),
        _policy("p-block", name="Block", controls=["block"], effective=["u1"]),
    ])
    result = ca_simulator.evaluate(policies, _user("u1"), BROWSER)
    assert result["verdict"] == BLOCKED
    assert result["blocked_by"] == ["Block"]


def test_policy_that_does_not_target_the_principal_does_not_apply():
    policies = _prep([_policy("p1", controls=["mfa"], effective=["someone-else"])])
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == GRANTED


def test_disabled_policy_never_applies():
    policies = _prep([_policy("p1", state="disabled", controls=["mfa"], effective=["u1"])])
    assert ca_simulator.evaluate(policies, _user("u1", mfa=False), BROWSER)["verdict"] == GRANTED


def test_report_only_policy_never_blocks():
    policies = _prep([_policy("p1", state="enabledForReportingButNotEnforced",
                              controls=["block"], effective=["u1"])])
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == GRANTED


def test_client_app_condition_scopes_the_policy():
    policies = _prep([_policy("p1", controls=["block"], effective=["u1"],
                              conditions={"client_app_types": ["exchangeActiveSync", "other"]})])
    assert ca_simulator.evaluate(policies, _user("u1"), LEGACY)["verdict"] == BLOCKED
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == GRANTED


def test_risk_condition_scopes_the_policy():
    risky = SignInContext("risky", "High risk", sign_in_risk="high")
    policies = _prep([_policy("p1", controls=["block"], effective=["u1"],
                              conditions={"sign_in_risk": ["high"]})])
    assert ca_simulator.evaluate(policies, _user("u1"), risky)["verdict"] == BLOCKED
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == GRANTED


def test_app_class_scopes_the_policy():
    admin_ctx = SignInContext("admin", "Admin portals", app_class="admin_portals")
    policies = _prep([_policy("p1", controls=["mfa"], effective=["u1"], app_classes=["admin_portals"])])
    assert ca_simulator.evaluate(policies, _user("u1"), admin_ctx)["verdict"] == CHALLENGED
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == GRANTED


# =================================================================== grant operators
def test_and_operator_requires_every_control():
    policies = _prep([_policy("p1", controls=["mfa", "compliant_device"], effective=["u1"],
                              operator="AND")])
    # Has MFA but no compliant device -> cannot satisfy both.
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == BLOCKED_EFFECTIVE
    assert ca_simulator.evaluate(policies, _user("u1"), COMPLIANT)["verdict"] == CHALLENGED


def test_or_operator_requires_only_the_cheapest_control():
    policies = _prep([_policy("p1", controls=["mfa", "compliant_device"], effective=["u1"],
                              operator="OR")])
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == CHALLENGED


def test_controls_across_policies_are_conjunctive():
    policies = _prep([
        _policy("p1", controls=["mfa"], effective=["u1"]),
        _policy("p2", controls=["compliant_device"], effective=["u1"]),
    ])
    assert ca_simulator.evaluate(policies, _user("u1"), BROWSER)["verdict"] == BLOCKED_EFFECTIVE
    assert ca_simulator.evaluate(policies, _user("u1"), COMPLIANT)["verdict"] == CHALLENGED


# ================================================================= change application
def test_enable_change_turns_a_policy_on_without_mutating_the_baseline():
    baseline = [_policy("p1", state="disabled", controls=["mfa"], effective=["u1"])]
    proposed, notes = ca_simulator.apply_changes(baseline, [{"kind": "enable", "policy_id": "p1"}])
    assert proposed[0]["state"] == "enabled"
    assert baseline[0]["state"] == "disabled", "the baseline must never be mutated"
    assert notes and "enable" in notes[0]


def test_delete_change_removes_the_policy():
    baseline = [_policy("p1", effective=["u1"]), _policy("p2", effective=["u1"])]
    proposed, _ = ca_simulator.apply_changes(baseline, [{"kind": "delete", "policy_id": "p1"}])
    assert {p["id"] for p in proposed} == {"p2"}


def test_add_change_introduces_a_proposed_policy():
    proposed, _ = ca_simulator.apply_changes([], [{
        "kind": "add",
        "policy": _policy("new", name="Require MFA for all users", effective=["u1"]),
    }])
    assert [p["display_name"] for p in proposed] == ["Require MFA for all users"]


def test_unknown_change_kind_is_rejected_not_silently_ignored():
    """A silent no-op would render as 'nothing changes' — the worst possible answer."""
    baseline = [_policy("p1", effective=["u1"])]
    with pytest.raises(ca_simulator.InvalidChange) as exc:
        ca_simulator.apply_changes(baseline, [{"kind": "remove_exclusion", "policy_id": "p1"}])
    assert "remove_exclusion" in str(exc.value)
    assert "enable" in str(exc.value), "the error must list the kinds that are supported"


def test_unknown_policy_id_is_rejected():
    baseline = [_policy("p1", effective=["u1"])]
    with pytest.raises(ca_simulator.InvalidChange) as exc:
        ca_simulator.apply_changes(baseline, [{"kind": "disable", "policy_id": "ghost"}])
    assert "ghost" in str(exc.value)


def test_every_change_problem_is_reported_together():
    baseline = [_policy("p1", effective=["u1"])]
    with pytest.raises(ca_simulator.InvalidChange) as exc:
        ca_simulator.apply_changes(baseline, [
            {"kind": "nope", "policy_id": "p1"},
            {"kind": "enable", "policy_id": "ghost"},
        ])
    assert "nope" in str(exc.value) and "ghost" in str(exc.value)


# ========================================================================= the diff
def _snapshot(users, sps=()):
    return {
        "people": {"users": users, "groups": [], "capabilities": {}, "counts": {}},
        "roles": {"definitions": [], "assignments": [], "group_derived": [], "eligible": []},
        "apps": {"applications": [], "service_principals": list(sps), "counts": {}},
        "ca": {"policies": [], "group_members": {}, "auth_strengths": [], "counts": {}},
    }


def _snap_user(uid, **kw):
    base = {"id": uid, "upn": f"{uid}@contoso.com", "display_name": uid, "user_type": "Member",
            "enabled": True, "mfa_registered": True, "phishing_resistant": False,
            "on_prem_synced": False, "signin_known": True, "last_signin": NOW.isoformat(),
            "department": "IT", "job_title": "Eng"}
    base.update(kw)
    return base


def _analysis(policies, snapshot):
    return {"policies": policies, "breakglass": {"confirmed_ids": [], "candidates": []},
            "coverage": {}, "conflicts": [], "counts": {}}


def test_enabling_an_mfa_policy_blocks_only_those_who_cannot_satisfy_it():
    users = [_snap_user("alice", mfa_registered=True), _snap_user("svc", mfa_registered=False)]
    snapshot = _snapshot(users)
    policies = [_policy("p1", name="Require MFA", state="disabled", controls=["mfa"],
                        effective=["alice", "svc"])]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "enable", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["counts"]["newly_blocked"] == 1
    assert result["counts"]["newly_challenged"] == 1
    blocked = [c for c in result["cases"] if c["category"] == "newly_blocked"]
    assert blocked[0]["principal"] == "svc@contoso.com"
    assert blocked[0]["missing"] == ["mfa"]


def test_disabling_a_policy_is_reported_as_protection_lost():
    """The category nobody tests for — the silent risk of a 'cleanup'."""
    users = [_snap_user("alice")]
    snapshot = _snapshot(users)
    policies = [_policy("p1", name="Require MFA", controls=["mfa"], effective=["alice"])]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "disable", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["counts"]["protection_lost"] == 1
    assert result["counts"]["newly_blocked"] == 0


def test_deleting_a_policy_is_reported_as_protection_lost():
    users = [_snap_user("alice")]
    snapshot = _snapshot(users)
    policies = [_policy("p1", controls=["mfa"], effective=["alice"])]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "delete", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["counts"]["protection_lost"] == 1


def test_no_change_produces_no_impact():
    users = [_snap_user("alice"), _snap_user("bob")]
    snapshot = _snapshot(users)
    policies = [_policy("p1", controls=["mfa"], effective=["alice", "bob"])]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "enable", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["counts"]["newly_blocked"] == 0
    assert result["counts"]["protection_lost"] == 0
    assert result["cases"] == []


def test_break_glass_impact_is_reported_first_and_separately():
    """The most expensive Conditional Access mistake in the field."""
    users = [_snap_user("bg", mfa_registered=False), _snap_user("alice")]
    snapshot = _snapshot(users)
    policies = [_policy("p1", name="Require MFA", state="disabled", controls=["mfa"],
                        effective=["bg", "alice"])]
    analysis = {"policies": policies,
                "breakglass": {"confirmed_ids": ["bg"], "candidates": [{"user_id": "bg"}]},
                "coverage": {}, "conflicts": [], "counts": {}}
    result = ca_simulator.simulate(snapshot, analysis, [{"kind": "enable", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["break_glass_affected"] == 1
    assert result["break_glass_impact"][0]["principal"] == "bg@contoso.com"
    # And it must sort to the very top of the case list.
    assert result["cases"][0]["principal"] == "bg@contoso.com"


def test_break_glass_is_never_sampled_away():
    principals = [SimPrincipal(id=f"u{i}", label=f"u{i}", cohorts=["members"]) for i in range(500)]
    principals.append(SimPrincipal(id="bg", label="bg", cohorts=["break_glass"]))
    principals.append(SimPrincipal(id="ga", label="ga", cohorts=["global_admins"]))
    selected, meta = ca_simulator.select_principals(principals, sample_size=10)
    ids = {p.id for p in selected}
    assert "bg" in ids and "ga" in ids
    assert meta["sampled"] is True


def test_sampling_is_seeded_so_reruns_are_comparable():
    principals = [SimPrincipal(id=f"u{i}", label=f"u{i}", cohorts=["members"]) for i in range(200)]
    a, _ = ca_simulator.select_principals(principals, sample_size=20)
    b, _ = ca_simulator.select_principals(principals, sample_size=20)
    assert [p.id for p in a] == [p.id for p in b]


def test_result_always_carries_limitations_and_a_confidence_label():
    snapshot = _snapshot([_snap_user("alice")])
    policies = [_policy("p1", controls=["mfa"], effective=["alice"], state="disabled")]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "enable", "policy_id": "p1"}], now=NOW)
    assert result["limitations"], "a bare verdict with no limitations is never acceptable"
    assert result["confidence_label"] == "Modelled locally"


def test_unknown_mfa_assumption_is_surfaced_in_the_result():
    users = [_snap_user("alice", mfa_registered=None)]
    snapshot = _snapshot(users)
    policies = [_policy("p1", controls=["mfa"], effective=["alice"], state="disabled")]
    result = ca_simulator.simulate(snapshot, _analysis(policies, snapshot),
                                   [{"kind": "enable", "policy_id": "p1"}],
                                   contexts=["browser_unmanaged"], now=NOW)
    assert result["assumptions"]["mfa_unknown_principals"] == 1
    assert "may be higher" in result["assumptions"]["mfa_unknown_note"]


def test_simulation_is_deterministic():
    users = [_snap_user(f"u{i}", mfa_registered=(i % 2 == 0)) for i in range(30)]
    snapshot = _snapshot(users)
    policies = [_policy("p1", controls=["mfa"], effective=[u["id"] for u in users], state="disabled")]
    change = [{"kind": "enable", "policy_id": "p1"}]
    a = ca_simulator.simulate(snapshot, _analysis(policies, snapshot), change, now=NOW)
    b = ca_simulator.simulate(snapshot, _analysis(policies, snapshot), change, now=NOW)
    assert a["counts"] == b["counts"]
    assert [c["principal_id"] for c in a["cases"]] == [c["principal_id"] for c in b["cases"]]
    assert a["fingerprint"] == b["fingerprint"]


# ==================================================================== engine divergence
def test_divergence_is_surfaced_not_hidden():
    local = [{"principal_id": "u1", "context": "browser", "to": BLOCKED_EFFECTIVE,
              "principal": "u1", "cohorts": [], "category": "newly_blocked"}]
    agree = ca_simulator.compare_engines(local, {"u1|browser": "blocked"})
    assert agree["disagreements"] == 0 and agree["confidence"] == "verified"

    disagree = ca_simulator.compare_engines(local, {"u1|browser": "success"})
    assert disagree["disagreements"] == 1
    assert disagree["cases"][0]["microsoft_verdict"] == "success"
    assert disagree["confidence"] == "modelled_unverified"


def test_unsampled_cases_do_not_count_as_agreement():
    local = [{"principal_id": "u1", "context": "browser", "to": BLOCKED,
              "principal": "u1", "cohorts": [], "category": "newly_blocked"}]
    result = ca_simulator.compare_engines(local, {})
    assert result["sampled"] == 0
    assert result["confidence"] != "verified"
