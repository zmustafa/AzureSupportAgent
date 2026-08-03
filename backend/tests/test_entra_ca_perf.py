"""The Conditional Access analysis must stay off the "frozen application" path.

`detect_breakglass` rebuilt `set(policy["effective_ids"])` inside a per-user loop. On a
5,000-user tenant with 60 policies that is 600,000 reconstructions of a 5,000-element set, and
it took **30 seconds** — on the event loop, in the request path for the Conditional Access page.
The demo tenant is far too small to show it: the same function returns in 1 ms there, which is
why it survived.

The budget below is deliberately loose (5s against a measured ~0.8s). This is a regression
guard, not a benchmark; it should fail when someone reintroduces quadratic work, not when CI is
busy.
"""
from __future__ import annotations

import time
import uuid

import pytest

from app.entra import ca_engine

USERS = 5000
APPS = 800
POLICIES = 60
BUDGET_S = 5.0


def _big_tenant() -> dict:
    return {
        "people": {"users": [
            {"id": f"u{i}", "upn": f"u{i}@contoso.com", "display_name": f"User {i}",
             "enabled": True, "user_type": "Guest" if i % 10 == 0 else "Member",
             "mfa_registered": True}
            for i in range(USERS)
        ]},
        "apps": {"service_principals": [
            {"object_id": f"o{i}", "app_id": str(uuid.UUID(int=i)), "display_name": f"app{i}",
             "sp_type": "Application", "enabled": True, "is_first_party": False}
            for i in range(APPS)
        ]},
        "roles": {"definitions": [], "assignments": [
            {"principal_id": f"u{i}", "role_privileged": True} for i in range(20)
        ]},
        "ca": {"group_members": {}, "auth_strengths": [], "policies": [
            {"id": f"p{i}", "display_name": f"Policy {i}", "state": "enabled",
             "conditions": {
                 "include_users": ["All"], "exclude_users": [f"u{i}"],
                 "include_groups": [], "exclude_groups": [], "include_roles": [],
                 "exclude_roles": [], "include_apps": ["All"], "exclude_apps": [],
                 "client_app_types": ["all"], "user_actions": [], "auth_contexts": [],
             },
             "grant": {"operator": "OR", "controls": ["mfa"], "auth_strength_id": ""},
             "session": {"sign_in_frequency": True}}
            for i in range(POLICIES)
        ]},
    }


@pytest.mark.slow
def test_the_full_analysis_stays_within_budget_on_a_large_tenant():
    data = _big_tenant()
    start = time.perf_counter()
    analysis = ca_engine.analyse(data)
    elapsed = time.perf_counter() - start
    assert analysis["coverage"]["matrix"], "the analysis must actually produce a matrix"
    assert elapsed < BUDGET_S, (
        f"analyse() took {elapsed:.1f}s for {USERS} users / {APPS} apps / {POLICIES} policies "
        f"(budget {BUDGET_S}s). This ran on the event loop and froze the page once already; "
        f"look for a set or list rebuilt inside a per-user or per-cell loop."
    )


@pytest.mark.slow
def test_break_glass_detection_specifically_is_not_quadratic():
    """The exact function that took 30 seconds."""
    data = _big_tenant()
    policies = ca_engine.normalize_policies(data)
    start = time.perf_counter()
    ca_engine.detect_breakglass(policies, data, {})
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"detect_breakglass took {elapsed:.1f}s; it must not rebuild sets per user"


def test_hoisting_the_sets_did_not_change_what_break_glass_detects():
    """Same answers, faster - the optimisation must be behaviour-preserving."""
    data = _big_tenant()
    # Give one account every break-glass characteristic so the detector has something to find.
    data["people"]["users"].append({
        "id": "bg1", "upn": "breakglass@contoso.com", "display_name": "Emergency Access",
        "enabled": True, "user_type": "Member", "mfa_registered": False, "on_prem_synced": False,
    })
    for p in data["ca"]["policies"]:
        p["conditions"]["exclude_users"] = list(p["conditions"]["exclude_users"]) + ["bg1"]

    policies = ca_engine.normalize_policies(data)
    result = ca_engine.detect_breakglass(policies, data, {})
    ids = {c["user_id"] for c in result["candidates"]}
    assert "bg1" in ids, "an excluded, cloud-only, emergency-named admin must be a candidate"

    # The excluded account must rank above an ordinary user, otherwise "candidate" means nothing.
    by_id = {c["user_id"]: c for c in result["candidates"]}
    ordinary = [c for uid, c in by_id.items() if uid != "bg1"]
    assert by_id["bg1"]["score"] >= max((c["score"] for c in ordinary), default=0), (
        "the emergency account must not be out-scored by an ordinary one"
    )
