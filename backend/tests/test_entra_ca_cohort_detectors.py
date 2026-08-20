"""The three cohort-coverage detectors must actually fire.

These exist because the taxonomy rename silently disarmed `ca.admins_uncovered`,
`ca.users_uncovered` and `ca.guests_uncovered` and the whole 717-test Entra suite still passed.
Both failure modes were invisible:

* the class id moved from ``all`` to ``all_cloud_apps``, so the cell lookup missed and returned
  ``{}``;
* the cell's uncovered count was renamed, so ``cell.get("uncovered_total")`` was ``None``.

Either one makes the detector return "no findings", which reads on screen as *this tenant is
fine*. A detector that cannot fail is worse than one that is absent, because absence is visible.
Each test below is therefore paired with a partner asserting the detector goes quiet on a tenant
that genuinely is covered, so passing by always-returning-findings is not available either.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.entra import ca_engine
from app.entra.signal_defs import ca as ca_signals
from app.entra.signals import SignalContext

NOW = None


def _user(uid: str, *, guest: bool = False) -> dict[str, Any]:
    return {
        "id": uid, "upn": f"{uid}@contoso.com", "display_name": uid, "enabled": True,
        "user_type": "Guest" if guest else "Member", "mfa_registered": True,
    }


def _policy(pid: str, *, include_users: list[str], exclude_users: list[str] | None = None,
            state: str = "enabled") -> dict[str, Any]:
    return {
        "id": pid, "display_name": pid, "state": state,
        "conditions": {
            "include_users": include_users, "exclude_users": exclude_users or [],
            "include_groups": [], "exclude_groups": [], "include_roles": [], "exclude_roles": [],
            "include_apps": ["All"], "exclude_apps": [], "client_app_types": ["all"],
            "user_actions": [], "auth_contexts": [],
        },
        "grant": {"operator": "OR", "controls": ["mfa"], "auth_strength_id": ""},
        "session": {},
    }


def _snapshot(users: list[dict], policies: list[dict], admin_ids: list[str] | None = None):
    admins = admin_ids or []
    return {
        "people": {"users": users},
        "ca": {"policies": policies, "group_members": {}, "auth_strengths": []},
        "roles": {
            "definitions": [{"id": "ga", "template_id": "62e90394-69f5-4237-9190-012177145e10",
                             "display_name": "Global Administrator"}],
            "assignments": [
                {"principal_id": a, "role_definition_id": "ga", "role_privileged": True}
                for a in admins
            ],
        },
        "apps": {"service_principals": []},
    }


def _findings(snapshot: dict[str, Any], fn) -> list[dict[str, Any]]:
    snapshot["_ca_analysis"] = ca_engine.analyze(snapshot)
    return fn(snapshot, SignalContext(tenant_id="t1"))


# ------------------------------------------------------------------ it must fire
@pytest.mark.parametrize(
    ("fn", "signal_id", "guest", "admin"),
    [
        (ca_signals._users_uncovered, "ca.users_uncovered", False, False),
        (ca_signals._guests_uncovered, "ca.guests_uncovered", True, False),
        (ca_signals._admins_uncovered, "ca.admins_uncovered", False, True),
    ],
)
def test_the_cohort_detector_fires_when_a_principal_is_left_out(fn, signal_id, guest, admin):
    users = [_user("u1", guest=guest), _user("u2", guest=guest)]
    snap = _snapshot(
        users,
        [_policy("p1", include_users=["All"], exclude_users=["u2"])],
        admin_ids=["u1", "u2"] if admin else [],
    )
    found = _findings(snap, fn)
    assert found, f"{signal_id} reported a clean tenant while u2 is excluded from every policy"
    assert {f["object_id"] for f in found} == {"u2"}
    assert all(f["signal_id"] == signal_id for f in found)


# ------------------------------------------------------- and it must be able to stay quiet
@pytest.mark.parametrize(
    ("fn", "guest", "admin"),
    [
        (ca_signals._users_uncovered, False, False),
        (ca_signals._guests_uncovered, True, False),
        (ca_signals._admins_uncovered, False, True),
    ],
)
def test_the_cohort_detector_is_silent_when_everyone_is_covered(fn, guest, admin):
    users = [_user("u1", guest=guest), _user("u2", guest=guest)]
    snap = _snapshot(
        users,
        [_policy("p1", include_users=["All"])],
        admin_ids=["u1", "u2"] if admin else [],
    )
    assert _findings(snap, fn) == []


def test_the_cell_the_detectors_read_still_carries_its_count_key():
    """Guards the exact rename that disarmed them."""
    snap = _snapshot([_user("u1"), _user("u2")],
                     [_policy("p1", include_users=["All"], exclude_users=["u2"])])
    analysis = ca_engine.analyze(snap)
    row = next(r for r in analysis["coverage"]["matrix"] if r["cohort"] == "members")
    cell = row["cells"]["all_cloud_apps|mfa"]
    assert "uncovered_total" in cell, "the detectors gate on this key; renaming it silences them"
    assert cell["uncovered_total"] == 1
    assert cell["uncovered_sample"] == ["u2"]
