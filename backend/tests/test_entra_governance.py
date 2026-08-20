"""Identity governance: coverage synthesis, review quality and lifecycle effectiveness.

The behavior these tests protect hardest is that the coverage view still works with no
governance license at all. A tenant that cannot read access reviews should learn that 18
privileged roles have never been reviewed — not see an empty screen saying "requires P2".
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.entra.collectors.governance import COVERAGE_CLASSES, _scope_summary, coverage
from app.entra.signal_defs import gov as gov_signals
from app.entra.signals import SignalContext, SignalUnavailable

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _ctx():
    return SignalContext(now=NOW, tenant_id="t1")


def _data(*, reviews=(), packages=(), assignments=(), workflows=(), caps=None,
          users=(), groups=(), roles=None, sps=()):
    return {
        "governance": {
            "reviews": list(reviews), "packages": list(packages),
            "assignments": list(assignments), "workflows": list(workflows),
            "capabilities": caps if caps is not None else {
                "access_reviews": True, "entitlement": True, "lifecycle": True},
        },
        "people": {"users": list(users), "groups": list(groups)},
        "roles": roles or {"assignments": [], "group_derived": [], "eligible": [],
                           "definitions": []},
        "apps": {"service_principals": list(sps)},
    }


# ==================================================================== coverage
def test_coverage_reports_every_class_even_on_an_empty_tenant():
    rows = coverage(_data())
    assert {r["key"] for r in rows} == {c["key"] for c in COVERAGE_CLASSES}
    assert all(r["label"] and r["why"] for r in rows), "every row must explain itself"


# ------------------------------------------------------------- review scope parsing
# Both query strings below are copied verbatim from a live tenant. An access-review scope
# is an OData query, not a typed object, so a shape nobody anticipated is silently invisible
# to the coverage join and the tenant is told it reviews nothing.
_LIVE_GROUP_SCOPE = {
    "@odata.type": "#microsoft.graph.accessReviewQueryScope",
    "query": "/v1.0/groups/9b256dc4-a3f0-4281-b61c-9ab4028fa541/transitiveMembers"
             "/microsoft.graph.user",
    "queryType": "MicrosoftGraph",
}
_LIVE_PACKAGE_SCOPE = {
    "@odata.type": "#microsoft.graph.accessReviewQueryScope",
    "query": "/v1.0/identityGovernance/entitlementManagement/assignments?$filter="
             "(accessPackage/id eq '3798004a-c701-4206-a7f7-9ea3db0508e4' and "
             "assignmentPolicy/id eq '6486148b-29d3-4b8b-ad84-c17d754155bf')",
    "queryType": "MicrosoftGraph",
}


def test_a_group_review_scope_yields_the_group_id_not_the_last_path_segment():
    """Taking the last segment of the query gave the literal string 'microsoft.graph.user',
    which matches no group, so every group review counted for nothing."""
    scope = _scope_summary(_LIVE_GROUP_SCOPE)
    assert scope["kind"] == "group"
    assert scope["target"] == "9b256dc4-a3f0-4281-b61c-9ab4028fa541"


def test_an_access_package_review_scope_is_recognised():
    """Entitlement-management reviews were parsed as 'unknown' and dropped."""
    scope = _scope_summary(_LIVE_PACKAGE_SCOPE)
    assert scope["kind"] == "access_package"
    assert scope["target"] == "3798004a-c701-4206-a7f7-9ea3db0508e4"


def test_an_unrecognised_scope_is_reported_as_unknown_not_guessed():
    scope = _scope_summary({"@odata.type": "#microsoft.graph.accessReviewQueryScope",
                            "query": "/v1.0/somethingNew/xyz"})
    assert scope["kind"] == "unknown"
    assert scope["target"] == ""


def test_a_group_reviewed_through_its_access_package_counts_as_reviewed():
    """The review targets the package's assignments, not the group, so a direct-scope-only
    join reported '0 reviewed' on a tenant reviewing every package monthly."""
    pkg = "3798004a-c701-4206-a7f7-9ea3db0508e4"
    data = _data(
        reviews=[{"scope": _scope_summary(_LIVE_PACKAGE_SCOPE)}],
        assignments=[{"principal_id": "g1", "package_id": pkg}],
        groups=[{"id": "g1", "display_name": "Prod Contributors",
                 "is_assignable_to_role": True}],
    )
    row = next(r for r in coverage(data) if r["key"] == "role_assignable_groups")
    assert row["count"] == 1
    assert row["reviewed"] == 1
    assert row["gap"] == 0


def test_an_assignment_in_an_unreviewed_package_is_still_a_gap():
    data = _data(
        reviews=[{"scope": _scope_summary(_LIVE_PACKAGE_SCOPE)}],
        assignments=[{"principal_id": "g1", "package_id": "some-other-package"}],
        groups=[{"id": "g1", "display_name": "Prod Contributors",
                 "is_assignable_to_role": True}],
    )
    row = next(r for r in coverage(data) if r["key"] == "role_assignable_groups")
    assert row["reviewed"] == 0
    assert row["gap"] == 1


def test_coverage_works_without_a_governance_licence():
    """The whole point: no reviews readable means everything counts as unreviewed."""
    data = _data(
        caps={"access_reviews": False, "entitlement": False, "lifecycle": False},
        roles={"assignments": [{"role_name": "Global Administrator", "role_privileged": True},
                               {"role_name": "Security Reader", "role_privileged": True}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    row = next(r for r in coverage(data) if r["key"] == "privileged_roles")
    assert row["count"] == 2
    assert row["reviewed"] == 0
    assert row["gap"] == 2


def test_a_role_scoped_review_reduces_the_gap():
    data = _data(
        reviews=[{"id": "r1", "scope": {"kind": "role", "target": "", "query": "/roleManagement"}}],
        roles={"assignments": [{"role_name": "Global Administrator", "role_privileged": True}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    row = next(r for r in coverage(data) if r["key"] == "privileged_roles")
    assert row["reviewed"] == 1
    assert row["gap"] == 0


def test_guests_governed_by_an_access_package_are_not_a_gap():
    data = _data(
        assignments=[{"principal_id": "g1", "package_id": "p1"}],
        users=[{"id": "g1", "user_type": "Guest", "upn": "a@x", "enabled": True},
               {"id": "g2", "user_type": "Guest", "upn": "b@x", "enabled": True}],
    )
    row = next(r for r in coverage(data) if r["key"] == "guests")
    assert row["count"] == 2
    assert row["governed"] == 1
    assert row["gap"] == 1


def test_eligible_privileged_roles_count_towards_coverage():
    """PIM eligibility is still privilege; a review that ignores it reviews half the problem."""
    data = _data(roles={
        "assignments": [], "group_derived": [], "definitions": [],
        "eligible": [{"role_name": "Privileged Role Administrator", "role_privileged": True}],
    })
    row = next(r for r in coverage(data) if r["key"] == "privileged_roles")
    assert row["count"] == 1


def test_only_critical_and_high_permission_apps_count_as_high_privilege():
    data = _data(sps=[
        {"object_id": "sp1", "display_name": "Powerful",
         "granted_app_permissions": [{"permission": "Mail.ReadWrite", "tier": "critical"}]},
        {"object_id": "sp2", "display_name": "Harmless",
         "granted_app_permissions": [{"permission": "User.Read", "tier": "low"}]},
    ])
    row = next(r for r in coverage(data) if r["key"] == "high_privilege_apps")
    assert row["count"] == 1
    assert row["objects"] == ["Powerful"]


# ======================================================= coverage-gap signals
def test_unreviewed_privileged_roles_fire_without_governance_data():
    data = _data(
        caps={"access_reviews": False, "entitlement": False, "lifecycle": False},
        roles={"assignments": [{"role_name": "Global Administrator", "role_privileged": True}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    fn = gov_signals._unreviewed("privileged_roles", "gov.privileged_roles_unreviewed",  # noqa: SLF001
                                 "high", "privileged role", ("roles",))
    out = fn(data, _ctx())
    assert len(out) == 1
    assert out[0]["evidence"]["access_reviews_readable"] is False
    assert "correct assumption when no review data exists" in out[0]["detail"]


def test_unreviewed_signal_is_silent_when_the_gap_is_closed():
    data = _data(
        reviews=[{"id": "r1", "scope": {"kind": "role", "target": "", "query": "/roleManagement"}}],
        roles={"assignments": [{"role_name": "Global Administrator", "role_privileged": True}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    fn = gov_signals._unreviewed("privileged_roles", "gov.privileged_roles_unreviewed",  # noqa: SLF001
                                 "high", "privileged role", ("roles",))
    assert fn(data, _ctx()) == []


def test_unreviewed_signal_needs_its_inventory_domain():
    fn = gov_signals._unreviewed("privileged_roles", "gov.privileged_roles_unreviewed",  # noqa: SLF001
                                 "high", "privileged role", ("roles",))
    with pytest.raises(SignalUnavailable):
        fn({"governance": {}}, _ctx())


# ========================================================== review quality
def test_overdue_review_is_measured_from_the_instance_end_date():
    data = _data(reviews=[{
        "id": "r1", "display_name": "Guests", "auto_apply": True, "scope": {},
        "instances": [{"id": "i1", "status": "InProgress", "end": "2026-07-01T00:00:00Z"}],
    }])
    out = gov_signals._review_overdue(data, _ctx())  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["evidence"]["days_overdue"] == 30


def test_a_completed_instance_is_never_overdue():
    data = _data(reviews=[{
        "id": "r1", "display_name": "Guests", "auto_apply": True, "scope": {},
        "instances": [{"id": "i1", "status": "Completed", "end": "2026-07-01T00:00:00Z"}],
    }])
    assert gov_signals._review_overdue(data, _ctx()) == []  # noqa: SLF001


def test_default_approve_is_reported_because_it_cannot_remove_anything():
    data = _data(reviews=[
        {"id": "r1", "display_name": "Lax", "default_decision_enabled": True,
         "default_decision": "Approve", "scope": {}},
        {"id": "r2", "display_name": "Strict", "default_decision_enabled": True,
         "default_decision": "Deny", "scope": {}},
    ])
    out = gov_signals._review_default_approve(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["r1"]


def test_reviews_without_auto_apply_are_reported():
    data = _data(reviews=[{"id": "r1", "display_name": "Manual", "auto_apply": False, "scope": {}},
                          {"id": "r2", "display_name": "Automatic", "auto_apply": True,
                           "scope": {}}])
    out = gov_signals._review_no_auto_apply(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["r1"]


def test_one_off_reviews_are_reported_and_recurring_ones_are_not():
    data = _data(reviews=[{"id": "r1", "display_name": "Once", "recurrence": "one-off",
                           "scope": {}},
                          {"id": "r2", "display_name": "Quarterly", "recurrence": "quarterly",
                           "scope": {}}])
    out = gov_signals._review_one_off_only(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["r1"]


def test_review_signals_report_not_measured_when_reviews_are_unreadable():
    data = _data(caps={"access_reviews": False, "entitlement": False, "lifecycle": False})
    for fn in (gov_signals._review_overdue,             # noqa: SLF001
               gov_signals._review_no_auto_apply,       # noqa: SLF001
               gov_signals._review_default_approve):    # noqa: SLF001
        with pytest.raises(SignalUnavailable):
            fn(data, _ctx())


# ============================================================ entitlement
def test_package_without_a_review_policy_is_reported():
    data = _data(packages=[
        {"id": "p1", "display_name": "Unreviewed", "policies": [{"review_required": False}]},
        {"id": "p2", "display_name": "Reviewed", "policies": [{"review_required": True}]},
    ])
    out = gov_signals._entitlement_no_review(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["p1"]


def test_package_with_a_never_expiring_policy_is_reported():
    data = _data(packages=[
        {"id": "p1", "display_name": "Forever",
         "policies": [{"display_name": "All staff", "expires": False}]},
        {"id": "p2", "display_name": "Bounded",
         "policies": [{"display_name": "All staff", "expires": True}]},
    ])
    out = gov_signals._entitlement_no_expiry(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["p1"]


def test_expiring_assignments_are_advance_notice_not_a_fault():
    data = _data(assignments=[
        {"id": "a1", "package_name": "Partner", "principal_name": "Pat",
         "principal_type": "Guest", "expires_at": "2026-08-05T00:00:00Z"},
        {"id": "a2", "package_name": "Partner", "principal_name": "Sam",
         "principal_type": "Guest", "expires_at": "2026-12-05T00:00:00Z"},
    ])
    out = gov_signals._entitlement_expiring(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["a1"]
    assert out[0]["severity"] == "low"


def test_bypass_needs_entitlement_to_be_partially_in_use():
    """Nothing governed is a different finding; everything governed is no finding."""
    nothing = _data(assignments=[], users=[{"id": "g1", "user_type": "Guest", "enabled": True}])
    assert gov_signals._direct_assignment_bypass(nothing, _ctx()) == []  # noqa: SLF001

    everything = _data(assignments=[{"principal_id": "g1"}],
                       users=[{"id": "g1", "user_type": "Guest", "enabled": True}])
    assert gov_signals._direct_assignment_bypass(everything, _ctx()) == []  # noqa: SLF001

    partial = _data(assignments=[{"principal_id": "g1"}],
                    users=[{"id": "g1", "user_type": "Guest", "upn": "a@x", "enabled": True},
                           {"id": "g2", "user_type": "Guest", "upn": "b@x", "enabled": True}])
    out = gov_signals._direct_assignment_bypass(partial, _ctx())  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["evidence"]["guests_direct"] == 1


# ============================================================== lifecycle
def test_missing_leaver_workflow_is_reported():
    data = _data(workflows=[{"id": "w1", "category": "joiner", "enabled": True}])
    out = gov_signals._no_leaver_workflow(data, _ctx())  # noqa: SLF001
    assert len(out) == 1


def test_a_disabled_leaver_workflow_does_not_count_as_present():
    data = _data(workflows=[{"id": "w1", "category": "leaver", "enabled": False}])
    assert len(gov_signals._no_leaver_workflow(data, _ctx())) == 1  # noqa: SLF001


def test_failing_workflow_runs_are_reported():
    data = _data(workflows=[
        {"id": "w1", "display_name": "Leaver", "category": "leaver", "enabled": True,
         "runs": {"total": 10, "failed": 3, "successful": 7}, "task_count": 3},
        {"id": "w2", "display_name": "Joiner", "category": "joiner", "enabled": True,
         "runs": {"total": 10, "failed": 0, "successful": 10}, "task_count": 3},
    ])
    out = gov_signals._lifecycle_workflow_failing(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["w1"]


def test_an_enabled_leaver_workflow_with_surviving_access_is_the_worst_case():
    """False assurance: the control is green and the access is still there."""
    data = _data(
        workflows=[{"id": "w1", "display_name": "Leaver", "category": "leaver", "enabled": True,
                    "runs": {"total": 5, "failed": 0, "successful": 5}}],
        users=[{"id": "u1", "upn": "gone@x", "enabled": False, "licence_count": 2}],
    )
    out = gov_signals._leaver_workflow_ineffective(data, _ctx())  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["evidence"]["disabled_with_access"] == 1


def test_leaver_effectiveness_is_silent_when_no_workflow_exists():
    """Covered by gov.no_leaver_workflow instead; reporting both would double-count."""
    data = _data(workflows=[],
                 users=[{"id": "u1", "upn": "gone@x", "enabled": False, "licence_count": 2}])
    assert gov_signals._leaver_workflow_ineffective(data, _ctx()) == []  # noqa: SLF001
