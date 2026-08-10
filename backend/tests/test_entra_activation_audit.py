"""Activation detail without a write scope.

`roleAssignmentScheduleRequests` looks like the source of activation justification, but for
an app-only token Graph answers 403 naming only WRITE scopes:

    RoleAssignmentSchedule.ReadWrite.Directory, RoleManagement.ReadWrite.Directory,
    RoleAssignmentSchedule.Remove.Directory

Verified live on 2026-07-31 with RoleAssignmentSchedule.Read.Directory granted and consented:
still 403. A read-only product cannot use that collection at all.

The PIM audit log carries the same facts -- and more -- under AuditLog.Read.All.
"""
from __future__ import annotations

from app.entra.collectors.activations import (
    _AUDIT_DIRECTORY_CATEGORY,
    _audit_actor,
    _audit_principal,
    _dedupe,
    _merge_overlapping,
    _richer,
    _within,
    session,
)

ACTOR = "556f8a5f-5c19-4e14-b334-735b008fb13b"
ROLE = "62e90394-69f5-4237-9190-012177145e10"


def _audit_row() -> dict:
    """Shaped from a real directoryAudits PIM row."""
    return {
        "id": "audit-1",
        "category": "RoleManagement",
        "activityDateTime": "2026-07-31T15:27:31.214597Z",
        "result": "success",
        "initiatedBy": {"user": {"id": ACTOR}},
        "targetResources": [{
            "id": ACTOR, "type": "User", "displayName": "P-Alex",
            "userPrincipalName": "p-alex@example.com",
        }],
    }


def test_the_audit_category_separates_the_two_planes():
    """`loggedByService eq 'PIM'` spans directory roles, Azure roles and PIM for Groups.

    Live on 2026-07-31 one week held 28 RoleManagement, 29 ResourceManagement and 1
    GroupManagement ActivateRole rows. Taking them all as directory activations duplicated
    the Azure half (already read from ARM) and left those rows with an ARM role id that no
    directory lookup resolves — which grades them tier-2 and silences every tier-0 signal.
    """
    assert _AUDIT_DIRECTORY_CATEGORY == "RoleManagement"


def _sess(*, sid, start, end="", justification="", detail_known=True, source="entra_audit"):
    return session(
        sid=sid, plane="entra", source=source, principal_id=ACTOR, role_id=ROLE,
        role_name="Global Administrator", start=start, end=end, action="activated",
        status="success", justification=justification, detail_known=detail_known)


# ------------------------------------------------------------------ audit parsing
def test_the_audit_target_is_the_account_whose_privilege_was_raised():
    who = _audit_principal(_audit_row())
    assert who["id"] == ACTOR
    assert who["upn"] == "p-alex@example.com"


def test_a_row_with_no_usable_target_returns_nothing_rather_than_guessing():
    assert _audit_principal({"targetResources": [{"type": "Other", "id": "x"}]}) == {}


def test_the_actor_is_read_from_initiated_by():
    assert _audit_actor(_audit_row()) == ACTOR


def test_an_app_initiated_activation_still_resolves_an_actor():
    row = {"initiatedBy": {"app": {"servicePrincipalId": "sp-1"}}}
    assert _audit_actor(row) == "sp-1"


# ------------------------------------------------------------------ overlap merge
def test_two_sources_describing_one_elevation_collapse_to_one_session():
    """The audit stamps the grant, the instance the window — seconds apart.

    A minute-precision key alone let both through, so one activation appeared twice: once
    with its justification and once without, reading as two separate elevations.
    """
    audit = _sess(sid="entra:audit:1", start="2026-07-31T15:27:30Z",
                  end="2026-07-31T16:57:30Z", justification="Sub permissions.")
    instance = _sess(sid="entra:inst:1", start="2026-07-31T15:29:05Z",
                     end="2026-07-31T16:57:30Z", detail_known=False, source="entra_instance")
    out = _merge_overlapping([audit, instance])
    assert len(out) == 1
    assert out[0]["justification"] == "Sub permissions."
    assert out[0]["detail_known"] is True


def test_two_genuinely_separate_activations_are_not_merged():
    """Same principal and role, hours apart — two real elevations."""
    first = _sess(sid="a", start="2026-07-31T09:00:00Z", end="2026-07-31T10:00:00Z")
    second = _sess(sid="b", start="2026-07-31T15:00:00Z", end="2026-07-31T16:00:00Z")
    assert len(_merge_overlapping([first, second])) == 2


def test_a_merge_never_loses_a_field_the_other_source_had():
    """Neither source is a superset: one has the reason, the other the exact window."""
    audit = _sess(sid="a", start="2026-07-31T15:27:30Z", justification="Reason.")
    instance = _sess(sid="b", start="2026-07-31T15:27:35Z",
                     end="2026-07-31T16:57:30Z", detail_known=False)
    merged = _richer(audit, instance)
    assert merged["justification"] == "Reason."
    assert merged["end"] == "2026-07-31T16:57:30Z"
    assert merged["granted_hours"] is not None


def test_different_roles_never_merge():
    a = _sess(sid="a", start="2026-07-31T15:27:30Z")
    b = dict(_sess(sid="b", start="2026-07-31T15:27:35Z"), role_id="other-role")
    assert len(_merge_overlapping([a, b])) == 2


def test_unparseable_times_do_not_merge_unrelated_rows():
    """A blank start must not make everything look simultaneous."""
    assert _within("", "2026-07-31T15:27:30Z", 10) is False
    assert _within(None, None, 10) is False


def test_dedupe_end_to_end_keeps_the_record_with_the_reason():
    audit = _sess(sid="entra:audit:1", start="2026-07-31T15:27:30Z",
                  end="2026-07-31T16:57:30Z", justification="Sub permissions.")
    instance = _sess(sid="entra:inst:1", start="2026-07-31T15:27:31Z",
                     end="2026-07-31T16:57:30Z", detail_known=False, source="entra_instance")
    out = _dedupe([instance, audit])
    assert len(out) == 1
    assert out[0]["justification"] == "Sub permissions."
