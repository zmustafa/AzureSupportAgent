"""Conditional Access session controls — the half of the verdict that is not about entry.

Two distinct properties are guarded here:

  * a session control must NEVER be mistaken for a grant requirement. When the sub-controls
    were split out of the ``session_limits`` aggregate, `required_controls` kept subtracting
    only the aggregate — so a policy whose only control was app-enforced restrictions read
    as **blocked_effective**. A false hard block is the worst answer this screen can give;
  * the session verdict is reported ALONGSIDE the grant verdict, never folded into it.
    "Granted" and "granted but nothing can leave the session" are different answers.
"""
from __future__ import annotations

from app.entra import ca_engine
from app.entra import ca_simulator as sim


def _policy(name: str, controls: list[str], **session: object) -> dict:
    return {
        "id": name,
        "display_name": name,
        "state": "enabled",
        "is_enforced": True,
        "effective_ids": ["u1"],
        "controls": controls,
        "grant": {"operator": "OR", "controls": [c for c in controls if c not in ca_engine.SESSION_CONTROLS]},
        "session": session,
        # Must match the context's `app_class` or `matches()` short-circuits before any of
        # the control logic under test is reached.
        "app_classes": ["all_cloud_apps"],
        "conditions": {"client_app_types": ["all"]},
    }


BROWSER = sim.SignInContext("browser_unmanaged", "Browser, unmanaged device", client_app="browser")
USER = sim.SimPrincipal(id="u1", label="Test user", kind="user")


# --------------------------------------------------------- session != grant requirement
def test_a_session_only_policy_is_not_a_grant_requirement():
    """The regression that made "SharePoint, unmanaged, no download" read as a hard block."""
    prepared = sim._prepare([_policy("limited web access", [ca_engine.CTRL_APP_ENFORCED],
                                     app_enforced_restrictions=True)])
    assert sim.required_controls(prepared) == set()


def test_every_session_control_is_excluded_from_grant_requirements():
    """Guards the guard: a new session control added without updating the subtraction set
    would silently reintroduce the false block."""
    prepared = sim._prepare([
        _policy("all session controls", sorted(ca_engine.SESSION_CONTROLS)),
    ])
    assert sim.required_controls(prepared) == set()


def test_grant_controls_still_required_alongside_session_controls():
    """The fix must not go the other way and start ignoring real grant controls."""
    prepared = sim._prepare([
        _policy("mfa + session", [ca_engine.CTRL_MFA, ca_engine.CTRL_APP_ENFORCED],
                app_enforced_restrictions=True),
    ])
    assert sim.required_controls(prepared) == {ca_engine.CTRL_MFA}


# --------------------------------------------------------------- the reported session block
def test_session_block_names_the_policies_imposing_each_control():
    """A control with no policy named against it cannot be verified or found and changed."""
    prepared = sim._prepare([
        _policy("Limited web access", [ca_engine.CTRL_APP_ENFORCED], app_enforced_restrictions=True),
    ])
    out = sim.session_controls(prepared)
    assert out[ca_engine.CTRL_APP_ENFORCED]["on"] is True
    assert out[ca_engine.CTRL_APP_ENFORCED]["by"] == ["Limited web access"]


def test_a_control_nobody_imposes_is_off_with_an_empty_attribution():
    prepared = sim._prepare([_policy("mfa only", [ca_engine.CTRL_MFA])])
    out = sim.session_controls(prepared)
    assert out[ca_engine.CTRL_APP_ENFORCED] == {"on": False, "by": []}


def test_sign_in_frequency_carries_its_value_and_unit():
    """"Sign-in frequency is on" is not an answer without the number."""
    prepared = sim._prepare([
        _policy("Admin re-auth", [ca_engine.CTRL_SIGNIN_FREQUENCY],
                sign_in_frequency=True, sign_in_frequency_value=4, sign_in_frequency_type="hours"),
    ])
    entry = sim.session_controls(prepared)[ca_engine.CTRL_SIGNIN_FREQUENCY]
    assert entry["on"] is True
    assert entry["value"] == 4
    assert entry["type"] == "hours"


def test_persistent_browser_carries_its_mode():
    prepared = sim._prepare([
        _policy("No persistent browser", [ca_engine.CTRL_PERSISTENT_BROWSER],
                persistent_browser=True, persistent_browser_mode="never"),
    ])
    assert sim.session_controls(prepared)[ca_engine.CTRL_PERSISTENT_BROWSER]["mode"] == "never"


# ------------------------------------------------------------------------ egress summary
def test_egress_restricted_is_true_only_for_controls_that_bound_what_leaves():
    """A sign-in frequency of one hour does not stop a download in minute one. Conflating
    "the session is time-bounded" with "the session cannot export" is the exact mistake
    that hid this gap on the live tenant."""
    timed = sim._prepare([
        _policy("hourly re-auth", [ca_engine.CTRL_SIGNIN_FREQUENCY],
                sign_in_frequency=True, sign_in_frequency_value=1, sign_in_frequency_type="hours"),
    ])
    assert sim.session_controls(timed)["egress_restricted"] is False

    restricted = sim._prepare([
        _policy("limited web access", [ca_engine.CTRL_APP_ENFORCED], app_enforced_restrictions=True),
    ])
    assert sim.session_controls(restricted)["egress_restricted"] is True


def test_casb_also_counts_as_an_egress_control():
    prepared = sim._prepare([
        _policy("via Defender for Cloud Apps", [ca_engine.CTRL_CASB], cloud_app_security=True),
    ])
    assert sim.session_controls(prepared)["egress_restricted"] is True


# ------------------------------------------------------------------- verdict integration
def test_every_verdict_carries_a_session_block():
    """Including the ones where it is empty — a caller must never have to guess whether the
    absence of the key means "no controls" or "not evaluated"."""
    out = sim.evaluate([], USER, BROWSER)
    assert "session" in out
    assert out["session"]["egress_restricted"] is False


def test_a_blocked_sign_in_still_reports_the_session_controls():
    """A block tells you nothing about what a session would have been allowed to do had a
    different context reached the app."""
    prepared = sim._prepare([
        _policy("block legacy", [ca_engine.CTRL_BLOCK]),
        _policy("limited web access", [ca_engine.CTRL_APP_ENFORCED], app_enforced_restrictions=True),
    ])
    for p in prepared:
        p["is_block"] = ca_engine.CTRL_BLOCK in p["controls"]
    out = sim.evaluate(prepared, USER, BROWSER)
    assert out["verdict"] == sim.BLOCKED
    assert "session" in out


# ------------------------------------------------------------------------- limitations
def test_limitations_state_who_actually_enforces_app_restrictions():
    """We report the control; the APPLICATION implements it. Claiming otherwise is the false
    confidence this list exists to prevent."""
    joined = " ".join(sim.LIMITATIONS).lower()
    assert "implemented by the application" in joined
    assert "not the application's own configuration" in joined


# --------------------------------------------------------------- session-only CHANGES
# A change that only touches session controls leaves both verdicts identical. Before
# `_session_delta` existed it categorized as "unchanged" and the case was DROPPED, so the
# simulator told an operator their new "browse but do not download" policy did nothing.
def test_turning_on_a_session_control_is_not_reported_as_unchanged():
    before = {"verdict": sim.GRANTED, "protected": False,
              "session": sim.session_controls([])}
    after_pol = sim._prepare([_policy("limited web access", [ca_engine.CTRL_APP_ENFORCED],
                                      app_enforced_restrictions=True)])
    after = {"verdict": sim.GRANTED, "protected": False,
             "session": sim.session_controls(after_pol)}
    assert sim._categorise(before, after) == "session_tightened"


def test_removing_a_session_control_is_a_protection_loss():
    """The silent risk of a cleanup, in the session half."""
    pol = sim._prepare([_policy("limited web access", [ca_engine.CTRL_APP_ENFORCED],
                                app_enforced_restrictions=True)])
    before = {"verdict": sim.GRANTED, "protected": False, "session": sim.session_controls(pol)}
    after = {"verdict": sim.GRANTED, "protected": False, "session": sim.session_controls([])}
    assert sim._categorise(before, after) == "protection_lost"


def test_no_session_movement_is_still_unchanged():
    same = {"verdict": sim.GRANTED, "protected": False, "session": sim.session_controls([])}
    assert sim._categorise(same, dict(same)) == "unchanged"


def test_sign_in_frequency_alone_does_not_claim_egress_is_restricted():
    """The trap in the user's question: a re-auth prompt bounds session DURATION, it does
    not stop a download. Reporting it as a restriction would be a confident wrong answer."""
    pol = sim._prepare([_policy("re-auth", [ca_engine.CTRL_SIGNIN_FREQUENCY],
                                sign_in_frequency_value=4, sign_in_frequency_type="hours")])
    block = sim.session_controls(pol)
    assert block[ca_engine.CTRL_SIGNIN_FREQUENCY]["on"] is True
    assert block["egress_restricted"] is False


# ------------------------------------------------------- authentication-flow policies
# "Block authentication flows" targets device code flow / authentication transfer. Nothing
# in the rest of the condition set narrows it, so before `auth_flows` was collected the
# policy read as an unconditional block on All users and All apps — and every simulated
# sign-in on a real tenant came back BLOCKED.
def test_an_auth_flow_policy_does_not_match_an_ordinary_sign_in():
    from app.entra import ca_simulator as sim

    policy = sim._prepare([{
        "id": "p", "display_name": "Block authentication flows", "state": "enabled",
        "is_enforced": True, "is_block": True, "controls": ["block"],
        "effective_ids": ["u1"], "app_classes": ["all_cloud_apps"],
        "conditions": {"client_app_types": ["all"], "auth_flows": ["deviceCodeFlow"]},
    }])[0]
    user = sim.SimPrincipal(id="u1", label="u", kind="user")
    for ctx in sim.DEFAULT_CONTEXTS:
        assert sim.matches(policy, user, ctx) is False, ctx.key


def test_the_same_policy_without_the_flow_condition_still_matches():
    """Guards the guard: the skip must key on the condition, not on the policy name."""
    from app.entra import ca_simulator as sim

    policy = sim._prepare([{
        "id": "p", "display_name": "Block authentication flows", "state": "enabled",
        "is_enforced": True, "is_block": True, "controls": ["block"],
        "effective_ids": ["u1"], "app_classes": ["all_cloud_apps"],
        "conditions": {"client_app_types": ["all"], "auth_flows": []},
    }])[0]
    user = sim.SimPrincipal(id="u1", label="u", kind="user")
    assert any(sim.matches(policy, user, c) for c in sim.DEFAULT_CONTEXTS)


def test_an_auth_flow_condition_counts_as_narrowing():
    """`ca_engine` uses narrowing_conditions to decide what is an UNCONDITIONAL block."""
    from app.entra import ca_engine

    assert "authentication flow" in ca_engine._narrowing_conditions({"auth_flows": ["deviceCodeFlow"]})
    assert ca_engine._narrowing_conditions({"auth_flows": []}) == []


# ------------------------------------------------------- authentication-flow scoping
# Microsoft's recommended "block device code flow" policy targets ALL users and ALL apps and
# narrows on nothing except the authentication flow. The collector did not capture that
# condition, so the policy read as an unconditional block and EVERY simulated sign-in in the
