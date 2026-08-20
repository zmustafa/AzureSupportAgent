"""Simulator contexts must model application classes AND user actions correctly.

Two things are asserted here that a passing simulation cannot otherwise distinguish:

1. **The content surface is modeled.** Before, the only non-wildcard contexts were the two
   admin ones, so a change scoped to SharePoint, Exchange or Teams simulated as affecting
   nobody — the simulator was blind to the applications the organization's data lives in.

2. **"All cloud apps" does not reach a user action.** In Entra the target blade is exclusive:
   a policy targets cloud apps, or user actions, or an authentication context. Letting the
   wildcard cover user actions would report device registration as protected on almost every
   tenant, silently contradicting the detector that exists to report exactly that gap.
"""
from __future__ import annotations

from app.entra import ca_simulator, ca_taxonomy
from app.entra.ca_simulator import CONTEXTS_BY_KEY, SimPrincipal


def _policy(pid, *, classes, controls=("mfa",), effective=("u1",)):
    return {
        "id": pid, "display_name": pid, "state": "enabled",
        "conditions": {
            "include_users": ["All"], "exclude_users": [], "include_groups": [],
            "exclude_groups": [], "include_roles": [], "exclude_roles": [],
            "include_apps": ["All"], "exclude_apps": [], "client_app_types": ["all"],
            "platforms_include": [], "platforms_exclude": [], "locations_include": [],
            "locations_exclude": [], "sign_in_risk": [], "user_risk": [],
        },
        "grant": {"operator": "OR", "controls": list(controls), "auth_strength_id": ""},
        "controls": list(controls), "effective_ids": list(effective),
        "app_classes": list(classes), "is_enforced": True,
        "is_block": "block" in controls, "is_report_only": False, "is_disabled": False,
        "targets_all_apps": "all_cloud_apps" in classes,
    }


def _user(uid="u1"):
    return SimPrincipal(id=uid, label=f"{uid}@contoso.com", kind="user",
                        mfa_registered=True, phishing_resistant=False, cohorts=["members"])


def _applies(policy, ctx_key):
    prepared = ca_simulator._prepare([policy])  # noqa: SLF001 - internal by design
    return ca_simulator.matches(prepared[0], _user(), CONTEXTS_BY_KEY[ctx_key])


# ------------------------------------------------------------------ coverage of classes
def test_every_targetable_taxonomy_class_has_a_simulation_context():
    """A class with no context cannot be simulated, so changes to it look like no-ops."""
    derived = {c["id"] for c in ca_taxonomy.classes() if c.get("derived")}
    modelled = {c.app_class for c in ca_simulator.DEFAULT_CONTEXTS}
    missing = {c["id"] for c in ca_taxonomy.classes()} - derived - modelled
    # `legacy_protocols` is modeled by the two legacy CLIENT contexts rather than by a class.
    missing -= {"legacy_protocols", "scoped_constructs", "custom_lob"}
    assert not missing, f"no simulation context covers: {sorted(missing)}"


def test_the_content_surface_is_simulated():
    assert "collaboration_content" in {c.app_class for c in ca_simulator.DEFAULT_CONTEXTS}


# --------------------------------------------------- wildcard vs. narrower app classes
def test_an_all_cloud_apps_policy_reaches_a_narrower_application_class():
    assert _applies(_policy("p", classes=["all_cloud_apps"]), "collab_content") is True
    assert _applies(_policy("p", classes=["all_cloud_apps"]), "admin_portal") is True


def test_a_narrow_policy_does_not_reach_a_different_class():
    assert _applies(_policy("p", classes=["admin_planes"]), "collab_content") is False


# --------------------------------------------------------- wildcard vs. user actions
def test_an_all_cloud_apps_policy_does_NOT_reach_a_user_action():
    """The exclusivity rule. This is the assertion that keeps the simulator honest."""
    p = _policy("p", classes=["all_cloud_apps"])
    assert _applies(p, "register_device") is False, (
        "an All-cloud-apps policy does not protect device registration; treating it as though "
        "it does would report almost every tenant as protected against an attack it is open to"
    )
    assert _applies(p, "register_security_info") is False


def test_a_policy_that_targets_the_user_action_does_reach_it():
    p = _policy("p", classes=["identity_lifecycle"])
    assert _applies(p, "register_device") is True
    assert _applies(p, "register_security_info") is True


def test_a_user_action_policy_does_not_reach_ordinary_applications():
    p = _policy("p", classes=["identity_lifecycle"])
    assert _applies(p, "collab_content") is False
    assert _applies(p, "browser_unmanaged") is False
