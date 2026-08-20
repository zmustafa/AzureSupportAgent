"""Application-class exposure detectors.

Every detector gets two tests: one tenant where it must fire, one where it must stay quiet.
The pair matters more than either half. A detector that always fires is noise an operator learns
to scroll past; one that never fires is a green screen over a real gap, and this session has
already produced one of those by accident.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.entra import ca_engine
from app.entra.signal_defs import ca_appclass as sig
from app.entra.signals import SignalContext, SignalUnavailable

SHAREPOINT = "00000003-0000-0ff1-ce00-000000000000"
EXCHANGE = "00000002-0000-0ff1-ce00-000000000000"
TEAMS = "cc15fd57-2c6c-4117-a88c-83b1d56b4bbe"
ARM = "797f4846-ba00-4fd7-ba43-dac1f8f63013"
GRAPH = "00000003-0000-0000-c000-000000000000"
AZURE_PORTAL = "c44b4083-3bb0-49c1-b47d-974e53cbdf3c"
CTX = SignalContext(tenant_id="t1")


def _user(uid: str, *, guest: bool = False) -> dict[str, Any]:
    return {"id": uid, "upn": f"{uid}@contoso.com", "display_name": uid, "enabled": True,
            "user_type": "Guest" if guest else "Member", "mfa_registered": True}


def _sp(app_id: str, name: str) -> dict[str, Any]:
    return {"object_id": f"o-{app_id}", "app_id": app_id, "display_name": name,
            "sp_type": "Application", "enabled": True, "is_first_party": True}


def _policy(pid, *, apps=("All",), exclude_apps=(), controls=("mfa",), state="enabled",
            users=("All",), exclude_users=(), operator="OR", session=None, user_actions=(),
            client_app_types=("all",)) -> dict[str, Any]:
    return {
        "id": pid, "display_name": pid, "state": state,
        "conditions": {
            "include_users": list(users), "exclude_users": list(exclude_users),
            "include_groups": [], "exclude_groups": [], "include_roles": [], "exclude_roles": [],
            "include_apps": list(apps), "exclude_apps": list(exclude_apps),
            "client_app_types": list(client_app_types), "user_actions": list(user_actions),
            "auth_contexts": [],
        },
        "grant": {"operator": operator, "controls": list(controls), "auth_strength_id": ""},
        "session": session or {},
    }


def _snap(policies, *, users=None, sps=None, admins=()) -> dict[str, Any]:
    return {
        "people": {"users": users or [_user("u1")]},
        "ca": {"policies": policies, "group_members": {}, "auth_strengths": []},
        "roles": {
            "definitions": [],
            "assignments": [{"principal_id": a, "role_privileged": True} for a in admins],
        },
        "apps": {"service_principals": sps or [
            _sp(SHAREPOINT, "SharePoint"), _sp(EXCHANGE, "Exchange"), _sp(TEAMS, "Teams"),
            _sp(ARM, "Azure Resource Manager"), _sp(GRAPH, "Microsoft Graph"),
            _sp(AZURE_PORTAL, "Azure portal"),
        ]},
    }


def _run(snap, fn):
    snap["_ca_analysis"] = ca_engine.analyze(snap)
    return fn(snap, CTX)


# ---------------------------------------------------------------- class never targeted
def test_class_never_targeted_fires_when_nothing_covers_a_class():
    found = _run(_snap([_policy("p1", apps=[SHAREPOINT])]), sig._class_never_targeted)
    assert found
    assert "management_apis" in {f["object_id"] for f in found}


def test_class_never_targeted_is_silent_when_an_all_apps_policy_exists():
    found = _run(_snap([_policy("p1", apps=["All"])]), sig._class_never_targeted)
    ids = {f["object_id"] for f in found}
    assert "management_apis" not in ids and "all_cloud_apps" not in ids


def test_class_never_targeted_defers_to_the_per_user_action_detectors():
    """One root cause, one finding.

    Both user actions already have dedicated detectors that name the action and give a fitting
    remediation. Reporting the class as well put three findings on screen for one problem.
    """
    found = _run(_snap([_policy("p1", apps=[SHAREPOINT])]), sig._class_never_targeted)
    assert "identity_lifecycle" not in {f["object_id"] for f in found}
    # ...and the specific detectors must still be the ones reporting it.
    snap = _snap([_policy("p1", apps=[SHAREPOINT])])
    assert _run(snap, sig._security_info_unprotected)
    assert _run(snap, sig._device_join_unprotected)


# --------------------------------------------------------------------- dependency split
def test_dependency_split_fires_when_teams_is_covered_but_sharepoint_is_not():
    found = _run(_snap([_policy("p1", apps=[TEAMS])]), sig._dependency_split)
    assert found, "Teams covered while SharePoint and Exchange are open is the classic bypass"
    assert SHAREPOINT in found[0]["evidence"]["apps_missing"]


def test_dependency_split_is_silent_when_the_content_services_are_covered_too():
    snap = _snap([_policy("p1", apps=[TEAMS, SHAREPOINT, EXCHANGE])])
    assert _run(snap, sig._dependency_split) == []


# ----------------------------------------------------------------- management API gap
def test_management_api_gap_fires_when_only_the_portal_is_protected():
    found = _run(_snap([_policy("p1", apps=[AZURE_PORTAL])]), sig._management_api_gap)
    assert found, "protecting the portal but not ARM enforces the control only against browsers"
    assert found[0]["evidence"]["control"] == "mfa"


def test_management_api_gap_is_silent_when_the_api_is_protected_too():
    snap = _snap([_policy("p1", apps=[AZURE_PORTAL, ARM, GRAPH])])
    assert _run(snap, sig._management_api_gap) == []


# ------------------------------------------------------------------ exclusion carve-out
def test_exclusion_defeats_control_fires_on_an_all_apps_policy_with_a_carve_out():
    found = _run(_snap([_policy("p1", apps=["All"], exclude_apps=[ARM])]),
                 sig._exclusion_defeats_control)
    assert found
    assert ARM in found[0]["evidence"]["excluded"]


def test_exclusion_defeats_control_is_silent_without_an_exclusion():
    assert _run(_snap([_policy("p1", apps=["All"])]), sig._exclusion_defeats_control) == []


def test_an_all_apps_policy_with_an_exclusion_does_not_report_the_class_as_covered():
    """The exclusion must break coverage, not merely raise a separate finding."""
    snap = _snap([_policy("p1", apps=["All"], exclude_apps=[ARM])])
    analysis = ca_engine.analyze(snap)
    row = next(r for r in analysis["coverage"]["matrix"] if r["cohort"] == "members")
    cell = row["cells"]["management_apis|mfa"]
    assert cell["state"] != "covered", "an excluded app cannot count as covered"
    assert ARM in cell["apps_missing"]


# ------------------------------------------------------------------------- guest gap
def test_guest_scope_gap_fires_when_members_are_covered_and_guests_are_not():
    users = [_user("u1"), _user("g1", guest=True)]
    snap = _snap([_policy("p1", apps=["All"], users=["All"], exclude_users=["g1"])], users=users)
    found = _run(snap, sig._guest_scope_gap)
    assert found, "members fully covered while guests are excluded is a real standard gap"


def test_guest_scope_gap_is_silent_when_guests_are_included():
    users = [_user("u1"), _user("g1", guest=True)]
    snap = _snap([_policy("p1", apps=["All"], users=["All"])], users=users)
    assert _run(snap, sig._guest_scope_gap) == []


# --------------------------------------------------------------------- session control
def test_no_session_control_fires_when_only_authentication_is_controlled():
    found = _run(_snap([_policy("p1", apps=["All"], controls=["mfa"])]),
                 sig._no_session_control_on_content)
    assert found


def test_no_session_control_is_silent_when_a_session_control_exists():
    snap = _snap([_policy("p1", apps=["All"], controls=["mfa"],
                          session={"app_enforced_restrictions": True})])
    assert _run(snap, sig._no_session_control_on_content) == []


# ------------------------------------------------------------------------ shadowed class
def test_shadowed_class_fires_when_every_covering_policy_is_report_only():
    snap = _snap([_policy("p1", apps=[ARM], state="enabledForReportingButNotEnforced")])
    found = _run(snap, sig._shadowed_class)
    assert "management_apis" in {f["object_id"] for f in found}


def test_shadowed_class_is_silent_when_one_policy_is_enforcing():
    snap = _snap([
        _policy("p1", apps=[ARM], state="enabledForReportingButNotEnforced"),
        _policy("p2", apps=[ARM]),
    ])
    assert "management_apis" not in {f["object_id"] for f in _run(snap, sig._shadowed_class)}


# ------------------------------------------------------------------------ weak grant
def test_weak_grant_fires_on_or_across_mfa_and_device():
    snap = _snap([_policy("p1", controls=["mfa", "compliantDevice"], operator="OR")])
    assert _run(snap, sig._weak_grant_semantics)


def test_weak_grant_is_silent_on_and():
    snap = _snap([_policy("p1", controls=["mfa", "compliantDevice"], operator="AND")])
    assert _run(snap, sig._weak_grant_semantics) == []


# -------------------------------------------------------------- security info registration
def test_security_info_registration_fires_when_the_user_action_is_untargeted():
    assert _run(_snap([_policy("p1", apps=["All"])]), sig._security_info_unprotected)


def test_security_info_registration_is_silent_when_the_user_action_is_targeted():
    snap = _snap([_policy("p1", apps=[], user_actions=["urn:user:registersecurityinfo"],
                          controls=["mfa"])])
    assert _run(snap, sig._security_info_unprotected) == []


# ------------------------------------------------------------------ device registration
def test_device_join_fires_when_the_user_action_is_untargeted():
    assert _run(_snap([_policy("p1", apps=["All"])]), sig._device_join_unprotected)


def test_device_join_is_silent_when_the_user_action_is_targeted():
    snap = _snap([_policy("p1", apps=[], user_actions=["urn:user:registerdevice"],
                          controls=["mfa"])])
    assert _run(snap, sig._device_join_unprotected) == []


def test_protecting_one_user_action_does_not_silence_the_other():
    """The conflation bug.

    Both user actions live in one taxonomy class. Reading the class's matrix cell meant a
    policy protecting security-info registration marked the class covered and silenced the
    device-registration detector too — a false negative on a real gap, reported as clean.
    """
    snap = _snap([_policy("p1", apps=[], user_actions=["urn:user:registersecurityinfo"],
                          controls=["mfa"])])
    assert _run(snap, sig._security_info_unprotected) == [], "the protected action must be quiet"
    assert _run(snap, sig._device_join_unprotected), (
        "device registration is unprotected here and must still be reported"
    )


def test_a_user_action_policy_with_no_protective_control_does_not_count_as_protection():
    """Targeting the action while granting nothing protects nobody."""
    snap = _snap([_policy("p1", apps=[], user_actions=["urn:user:registerdevice"], controls=[])])
    assert _run(snap, sig._device_join_unprotected)


# ------------------------------------------------------------------ break-glass consistency
def _bg_snap(policies, *, confirmed=("bg1",)):
    users = [_user("u1"), _user("bg1")]
    snap = _snap(policies, users=users)
    snap["_confirmed_breakglass"] = {u: {"confirmed": True} for u in confirmed}
    return snap


def _run_bg(snap, confirmed=("bg1",)):
    snap["_ca_analysis"] = ca_engine.analyze(
        snap, confirmed_breakglass={u: {"confirmed": True} for u in confirmed})
    return sig._breakglass_inconsistent(snap, CTX)


def test_breakglass_inconsistency_fires_when_excluded_from_some_policies_only():
    found = _run_bg(_bg_snap([
        _policy("p1", apps=["All"], controls=["mfa"], exclude_users=["bg1"]),
        _policy("p2", apps=["All"], controls=["mfa"]),
    ]))
    assert found, "excluded from one blocking policy and in scope of another is inconsistent"
    assert found[0]["object_id"] == "bg1"


def test_breakglass_inconsistency_is_silent_when_uniformly_excluded():
    assert _run_bg(_bg_snap([
        _policy("p1", apps=["All"], controls=["mfa"], exclude_users=["bg1"]),
        _policy("p2", apps=["All"], controls=["mfa"], exclude_users=["bg1"]),
    ])) == []


def test_breakglass_inconsistency_is_silent_when_uniformly_in_scope():
    """Consistently in scope is a different decision, not an inconsistency."""
    assert _run_bg(_bg_snap([
        _policy("p1", apps=["All"], controls=["mfa"]),
        _policy("p2", apps=["All"], controls=["mfa"]),
    ])) == []


def test_breakglass_inconsistency_is_reported_as_reliability_not_security():
    spec = next(s for s in sig.SPECS if s.id == "ca.breakglass_inconsistent")
    assert "reliability" in spec.tags, (
        "this is an availability problem, not a hardening one; tagging it as security would "
        "push an operator to 'fix' it by removing the exclusion that keeps the account working"
    )


# ---------------------------------------------------------------------- unattributed apps
def test_unattributed_apps_refuses_to_report_clean_when_activity_was_not_collected():
    """Not measured must be UNAVAILABLE, never an empty (reassuring) result."""
    snap = _snap([_policy("p1", apps=["All"])])
    with pytest.raises(SignalUnavailable):
        _run(snap, sig._unattributed_apps)


def test_unattributed_apps_fires_on_a_signed_into_app_no_policy_covers():
    snap = _snap([_policy("p1", apps=[SHAREPOINT])])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30, "active_app_ids": [SHAREPOINT, ARM],
    }
    found = _run(snap, sig._unattributed_apps)
    assert {f["object_id"] for f in found} == {ARM}


def test_unattributed_apps_is_silent_when_everything_active_is_covered():
    snap = _snap([_policy("p1", apps=["All"])])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30, "active_app_ids": [SHAREPOINT, ARM],
    }
    assert _run(snap, sig._unattributed_apps) == []


def test_an_all_apps_policy_covers_active_apps_it_does_not_name():
    """The 359-false-positives bug.

    An "All cloud apps" policy resolves to the wildcard `*`, not to a list of ids. Treating the
    wildcard as covering nothing reported 359 of a real tenant's 513 active applications as
    ungoverned when every one of them was covered — which would train an operator to ignore the
    panel entirely, burying the few genuine gaps.
    """
    snap = _snap([_policy("p1", apps=["All"], controls=["mfa"])])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30,
        "active_app_ids": [SHAREPOINT, ARM, TEAMS, EXCHANGE, GRAPH],
    }
    assert _run(snap, sig._unattributed_apps) == []


def test_an_app_excluded_from_the_only_all_apps_policy_is_unattributed():
    snap = _snap([_policy("p1", apps=["All"], exclude_apps=[ARM], controls=["mfa"])])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30, "active_app_ids": [SHAREPOINT, ARM],
    }
    found = _run(snap, sig._unattributed_apps)
    assert {f["object_id"] for f in found} == {ARM}


def test_an_app_excluded_from_only_one_of_two_all_apps_policies_is_still_governed():
    """One policy still reaching it is enough. Intersecting the exclusions, not unioning them."""
    snap = _snap([
        _policy("p1", apps=["All"], exclude_apps=[ARM], controls=["mfa"]),
        _policy("p2", apps=["All"], controls=["mfa"]),
    ])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30, "active_app_ids": [SHAREPOINT, ARM],
    }
    assert _run(snap, sig._unattributed_apps) == []


def test_unattributed_apps_are_named_not_shown_as_guids():
    """A list of raw GUIDs says something is wrong but not what to go and look at."""
    snap = _snap([_policy("p1", apps=[SHAREPOINT])])
    snap["apps"]["signin_activity"] = {
        "measured": True, "window_days": 30, "active_app_ids": [SHAREPOINT, ARM],
    }
    found = _run(snap, sig._unattributed_apps)
    assert found
    assert found[0]["object_name"] == "Azure Resource Manager", (
        f"expected a resolved display name, got {found[0]['object_name']!r}"
    )
