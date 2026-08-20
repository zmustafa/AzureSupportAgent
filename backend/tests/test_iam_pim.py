"""PIM / JIT collection for the Azure plane.

The distinction this file protects is the whole point of PIM: privilege someone *holds* versus
privilege someone can *request*. Before P1 every live row was hardcoded ``Active``, so the
eligible KPI was always 0 and standing privilege was indistinguishable from JIT.

The quirks pinned here were all found live on a real tenant (see the Entra work) and every one
of them silently produces a wrong answer rather than an error.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.iam import collectors, compose, schema

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _patch_get_all(monkeypatch, value, err=None, code=200):
    async def fake(_token, _url, _params=None):
        return value, err, code

    monkeypatch.setattr(collectors, "_get_all", fake)


def _eligibility(**props) -> dict:
    base = {
        "principalId": "u-ivan",
        "principalType": "User",
        "roleDefinitionId": "/subscriptions/s1/providers/Microsoft.Authorization/roleDefinitions/rd-owner",
        "scope": "/subscriptions/s1",
        "status": "Provisioned",
        "startDateTime": "2026-01-01T00:00:00Z",
        "memberType": "Direct",
        "expandedProperties": {
            "principal": {"id": "u-ivan", "displayName": "Ivan Incident", "email": "ivan@x.example", "type": "User"},
            "roleDefinition": {"displayName": "Owner"},
            "scope": {"displayName": "Sub 1"},
        },
    }
    base.update(props)
    return {"id": "/subscriptions/s1/.../roleEligibilityScheduleInstances/es1", "properties": base}


_ROLE_DEFS = {"rd-owner": {"roleName": "Owner", "roleIsPrivileged": True, "roleCategory": "ControlPlane", "roleHasDataActions": False}}


async def _collect_eligibility(monkeypatch, payload, policies=None):
    _patch_get_all(monkeypatch, payload)
    return await collectors.collect_pim_eligibility(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="Sub 1",
        tenant_id="t1", role_defs=_ROLE_DEFS, policies=policies,
    )


# --------------------------------------------------------------------------- eligibility
async def test_eligible_assignment_is_state_eligible_not_active(monkeypatch):
    rows, st = await _collect_eligibility(monkeypatch, [_eligibility()])
    assert st.status == schema.STATUS_SUCCEEDED and len(rows) == 1
    row = rows[0]
    assert row["assignmentState"] == schema.STATE_ELIGIBLE
    assert row["assignmentType"] == "RoleEligibility"
    assert row["pimManaged"] is True
    assert row["roleName"] == "Owner"
    assert row["roleIsPrivileged"] is True
    # expandedProperties carries the name inline — a bare GUID in the Role column is a product
    # bug, not a data limitation.
    assert row["effectivePrincipalName"] == "Ivan Incident"


async def test_azure_pim_states_a_duration_not_an_end(monkeypatch):
    """`expiration: {type: AfterDuration, duration: PT8H}` with endDateTime ABSENT is the normal
    Azure shape. Reading only endDateTime leaves every window blank, which reads as
    "never expires" — the exact opposite of the truth."""
    rows, _ = await _collect_eligibility(monkeypatch, [
        _eligibility(startDateTime="2026-01-01T00:00:00Z", expiration={"type": "AfterDuration", "duration": "PT8H"}),
    ])
    end = datetime.fromisoformat(rows[0]["eligibilityEndDateTime"])
    assert end == datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert rows[0]["isPermanentEligible"] is False


async def test_an_explicit_end_date_is_used_as_is(monkeypatch):
    rows, _ = await _collect_eligibility(monkeypatch, [_eligibility(endDateTime="2026-06-01T00:00:00Z")])
    assert rows[0]["eligibilityEndDateTime"] == "2026-06-01T00:00:00Z"
    assert rows[0]["isPermanentEligible"] is False


async def test_no_expiration_is_permanent_eligibility(monkeypatch):
    """Permanently eligible with no approval is JIT in name only — the product must be able to
    say so, which means the flag has to be right."""
    rows, _ = await _collect_eligibility(monkeypatch, [_eligibility(expiration={"type": "NoExpiration"})])
    assert rows[0]["isPermanentEligible"] is True
    assert rows[0]["eligibilityEndDateTime"] == ""


async def test_arm_seven_digit_fractional_seconds_do_not_break_the_window(monkeypatch):
    """ARM emits 7 fractional digits; `datetime.fromisoformat` rejects them. Losing the start
    means losing the derived end, so the window silently reads as permanent."""
    rows, _ = await _collect_eligibility(monkeypatch, [
        _eligibility(startDateTime="2026-01-01T10:20:43.9723951+00:00",
                     expiration={"type": "AfterDuration", "duration": "PT2H"}),
    ])
    assert rows[0]["eligibilityEndDateTime"], "the 7-digit timestamp must still parse"
    assert rows[0]["isPermanentEligible"] is False


@pytest.mark.parametrize("status", ["Denied", "Failed", "Revoked"])
async def test_refused_requests_grant_nothing(monkeypatch, status):
    """A Denied or Failed schedule granted no access. Emitting it accuses someone of holding
    privilege they were explicitly refused."""
    rows, _ = await _collect_eligibility(monkeypatch, [_eligibility(status=status)])
    assert rows == []


async def test_missing_p2_licence_is_skipped_not_failed(monkeypatch):
    """PIM reports a missing license as a 400 WITH A MESSAGE, not a 403. Calling that Failed
    sends the operator chasing a permission problem that does not exist."""
    _patch_get_all(monkeypatch, [], err="HTTP 400: The tenant needs an AADP2 licence to use PIM", code=400)
    rows, st = await collectors.collect_pim_eligibility(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="",
        tenant_id="t1", role_defs={},
    )
    assert rows == [] and st.status == schema.STATUS_SKIPPED
    assert "not licensed" in st.message


async def test_a_real_permission_error_is_still_reported(monkeypatch):
    _patch_get_all(monkeypatch, [], err="HTTP 403: AuthorizationFailed", code=403)
    _rows, st = await collectors.collect_pim_eligibility(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="",
        tenant_id="t1", role_defs={},
    )
    assert st.status == schema.STATUS_UNAUTHORIZED


async def test_activation_policy_is_carried_onto_the_eligible_row(monkeypatch):
    policies = {"rd-owner": {"requiresApproval": True, "requiresMfa": True, "requiresJustification": True, "activationMaxHours": 8.0}}
    rows, _ = await _collect_eligibility(monkeypatch, [_eligibility()], policies=policies)
    assert rows[0]["requiresApproval"] is True
    assert rows[0]["requiresMfa"] is True
    assert rows[0]["activationMaxHours"] == "8.0"


# --------------------------------------------------------------------------- active schedules
def _schedule(assignment_type: str, **props) -> dict:
    base = {
        "originRoleAssignmentId": "/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments/ra-1",
        "assignmentType": assignment_type,
        "status": "Provisioned",
        "memberType": "Direct",
        "startDateTime": "2026-01-01T00:00:00Z",
    }
    base.update(props)
    return {"id": "asi-1", "properties": base}


async def test_active_schedules_are_returned_as_annotations_not_rows(monkeypatch):
    """A PIM activation creates a REAL role assignment, so the same access appears in both
    roleAssignments and roleAssignmentScheduleInstances. Emitting both double-counts every
    elevation; the join key is ARM's originRoleAssignmentId."""
    _patch_get_all(monkeypatch, [_schedule("Activated", expiration={"type": "AfterDuration", "duration": "PT4H"})])
    out, st = await collectors.collect_pim_active_schedules("tok", scope="/subscriptions/s1")
    assert st.status == schema.STATUS_SUCCEEDED
    key = "/subscriptions/s1/providers/microsoft.authorization/roleassignments/ra-1"
    assert key in out
    assert out[key]["activated"] is True
    assert out[key]["activationExpiresOn"]


async def test_permanent_pim_assignments_get_no_expiry(monkeypatch):
    """`Assigned` means PIM knows about a PERMANENT grant. Giving it an expiry would report
    standing privilege as a time-boxed elevation and understate the risk."""
    _patch_get_all(monkeypatch, [_schedule("Assigned", endDateTime="2026-09-01T00:00:00Z")])
    out, _ = await collectors.collect_pim_active_schedules("tok", scope="/subscriptions/s1")
    entry = next(iter(out.values()))
    assert entry["activated"] is False
    assert entry["activationExpiresOn"] == ""


async def test_active_schedule_without_an_origin_id_is_skipped(monkeypatch):
    _patch_get_all(monkeypatch, [_schedule("Activated", originRoleAssignmentId="")])
    out, _ = await collectors.collect_pim_active_schedules("tok", scope="/subscriptions/s1")
    assert out == {}


# --------------------------------------------------------------------------- annotation join
def test_annotate_pim_marks_only_the_matching_assignment():
    from app.iam.orchestrator import _annotate_pim

    rows = [
        schema.make_row(assignmentId="/SUBS/RA-1", roleName="Owner", roleIsPrivileged=True),
        schema.make_row(assignmentId="/subs/ra-2", roleName="Reader"),
    ]
    n = _annotate_pim(rows, {"/subs/ra-1": {"activated": True, "activationExpiresOn": "2026-01-01T08:00:00Z", "memberType": "Direct"}})
    assert n == 1
    # The join must be case-insensitive: ARM is inconsistent about resource-id casing, and a
    # case-sensitive match silently reports every elevation as standing privilege.
    assert rows[0]["pimManaged"] is True
    assert rows[0]["activationExpiresOn"] == "2026-01-01T08:00:00Z"
    assert rows[0]["assignmentType"] == "ActivatedRoleAssignment"
    assert rows[1]["pimManaged"] is False


def test_annotate_pim_is_a_noop_without_schedules():
    from app.iam.orchestrator import _annotate_pim

    rows = [schema.make_row(assignmentId="ra-1")]
    assert _annotate_pim(rows, {}) == 0
    assert rows[0]["pimManaged"] is False


# --------------------------------------------------------------------------- standing privilege
def _priv(**kw):
    return schema.make_row(roleName="Owner", roleIsPrivileged=True, **kw)


def test_standing_privilege_excludes_eligible_and_elevated():
    # Permanent privileged access — the thing PIM exists to eliminate.
    assert schema.is_standing_privilege(_priv()) is True
    # Eligible: must be activated first, so it is not held.
    assert schema.is_standing_privilege(_priv(assignmentState=schema.STATE_ELIGIBLE)) is False
    # Active right now, but via PIM and expiring — not standing.
    assert schema.is_standing_privilege(_priv(pimManaged=True, activationExpiresOn="2026-01-01T08:00:00Z")) is False
    # PIM knows about it but it never expires: still standing privilege.
    assert schema.is_standing_privilege(_priv(pimManaged=True)) is True
    # Non-privileged and deny rows never count.
    assert schema.is_standing_privilege(schema.make_row(roleName="Reader")) is False
    assert schema.is_standing_privilege(_priv(effect=schema.EFFECT_DENY)) is False


def test_standing_ratio_is_none_when_there_is_nothing_to_measure(isolated_cache_for_ratio):
    """A 0% ratio would read as a perfect JIT posture. "Nothing to measure" and "everything is
    JIT" are opposite facts and must not render alike."""
    from app.iam import cache

    cache.write_scope("t1", "/subscriptions/s", meta={}, rows=[schema.make_row(roleName="Reader")])
    k = compose.compute_overview("t1")["kpis"]
    assert k["standing_ratio"] is None


def test_a_scope_collected_before_pim_existed_reports_no_ratio(isolated_cache_for_ratio):
    """**Blind is not zero.** A cache collected before the PIM collectors existed (or by a
    connection that got a 403 on the schedule APIs) contains no eligible rows, which computes to
    "100% of privileged access is permanent" — a damning finding when the truth is that nobody
    looked. This is the single most misleading number this feature could produce."""
    from app.iam import cache

    cache.write_scope(
        "t1", "/subscriptions/s",
        meta={"collectors": [{"collector": "AzureSubscriptionRbac", "status": schema.STATUS_SUCCEEDED}]},
        rows=[_priv(effectivePrincipalId="u1")],
    )
    k = compose.compute_overview("t1")["kpis"]
    assert k["standing_privileged"] == 1
    assert k["pim_collected"] is False
    assert k["standing_ratio"] is None, "an uncollected surface must not produce a 100% figure"


def test_an_unlicensed_tenant_still_gets_a_ratio(isolated_cache_for_ratio):
    """`Skipped` means we DID look and PIM is not available — the tenant genuinely has no
    eligibility, so 100% standing is the honest answer and must be reported."""
    from app.iam import cache

    cache.write_scope(
        "t1", "/subscriptions/s",
        meta={"collectors": [{"collector": "AzurePimEligibility", "status": schema.STATUS_SKIPPED}]},
        rows=[_priv(effectivePrincipalId="u1")],
    )
    k = compose.compute_overview("t1")["kpis"]
    assert k["pim_collected"] is True
    assert k["standing_ratio"] == 1.0


def test_a_pim_collector_that_could_not_read_does_not_count_as_collected(isolated_cache_for_ratio):
    from app.iam import cache

    cache.write_scope(
        "t1", "/subscriptions/s",
        meta={"collectors": [{"collector": "AzurePimEligibility", "status": schema.STATUS_UNAUTHORIZED}]},
        rows=[_priv(effectivePrincipalId="u1")],
    )
    k = compose.compute_overview("t1")["kpis"]
    assert k["pim_collected"] is False
    assert k["standing_ratio"] is None


def test_standing_ratio_over_demo_data(isolated_cache_for_ratio):
    from app.iam import demo

    demo.seed_demo("t1")
    k = compose.compute_overview("t1")["kpis"]
    # 3 Azure PIM + 1 Entra directory-role eligibility. The third Azure one belongs to a
    # DISABLED account (Oscar, permanently eligible for Owner) — an eligibility survives
    # offboarding, which is precisely why it is counted here rather than ignored.
    assert k["eligible_privileged"] == 4
    assert k["active_elevations"] == 1        # Eve is elevated right now
    assert k["standing_privileged"] > 0
    # The elevated row is active but must NOT be counted as standing privilege.
    master = compose.build_master_rows("t1")
    elevated = [r for r in master if r["activationExpiresOn"]]
    assert elevated and all(not schema.is_standing_privilege(r) for r in elevated)
    assert 0 < k["standing_ratio"] < 1


@pytest.fixture()
def isolated_cache_for_ratio(tmp_path, monkeypatch):
    from app.iam import cache

    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


# --------------------------------------------------------------------------- policies
def _policy_assignment(rules: list[dict]) -> dict:
    return {
        "id": "pa-1",
        "properties": {
            "roleDefinitionId": "/subscriptions/s1/providers/Microsoft.Authorization/roleDefinitions/rd-owner",
            "effectiveRules": rules,
        },
    }


async def test_activation_policy_decodes_approval_mfa_and_duration(monkeypatch):
    _patch_get_all(monkeypatch, [_policy_assignment([
        {"id": "Approval_EndUser_Assignment", "ruleType": "RoleManagementPolicyApprovalRule",
         "setting": {"isApprovalRequired": True}},
        {"id": "Enablement_EndUser_Assignment", "ruleType": "RoleManagementPolicyEnablementRule",
         "enabledRules": ["MultiFactorAuthentication", "Justification"]},
        {"id": "Expiration_EndUser_Assignment", "ruleType": "RoleManagementPolicyExpirationRule",
         "maximumDuration": "PT8H"},
    ])])
    out, st = await collectors.collect_pim_policies("tok", scope="/subscriptions/s1")
    assert st.status == schema.STATUS_SUCCEEDED
    p = out["rd-owner"]
    assert p["requiresApproval"] is True
    assert p["requiresMfa"] is True and p["requiresJustification"] is True
    assert p["activationMaxHours"] == 8.0


async def test_admin_rules_are_not_mistaken_for_activation_rules(monkeypatch):
    """`Admin_*` rules describe what an ADMINISTRATOR must do to grant eligibility — a different
    question from what a user must do to activate. Conflating them reports approval as required
    when activation in fact requires nothing."""
    _patch_get_all(monkeypatch, [_policy_assignment([
        {"id": "Approval_Admin_Assignment", "ruleType": "RoleManagementPolicyApprovalRule",
         "setting": {"isApprovalRequired": True}},
        {"id": "Enablement_Admin_Assignment", "ruleType": "RoleManagementPolicyEnablementRule",
         "enabledRules": ["MultiFactorAuthentication"]},
    ])])
    out, _ = await collectors.collect_pim_policies("tok", scope="/subscriptions/s1")
    assert out["rd-owner"]["requiresApproval"] is False
    assert out["rd-owner"]["requiresMfa"] is False


async def test_a_policy_requiring_nothing_is_reported_as_such(monkeypatch):
    """A tenant that bought PIM but requires neither approval nor MFA to activate Owner has
    bought very little, and nothing else in the product would show that."""
    _patch_get_all(monkeypatch, [_policy_assignment([
        {"id": "Approval_EndUser_Assignment", "ruleType": "RoleManagementPolicyApprovalRule",
         "setting": {"isApprovalRequired": False}},
        {"id": "Enablement_EndUser_Assignment", "ruleType": "RoleManagementPolicyEnablementRule",
         "enabledRules": []},
    ])])
    out, _ = await collectors.collect_pim_policies("tok", scope="/subscriptions/s1")
    assert out["rd-owner"] == {"requiresApproval": False, "requiresMfa": False,
                               "requiresJustification": False, "activationMaxHours": None}


async def test_policy_licence_error_is_skipped(monkeypatch):
    _patch_get_all(monkeypatch, [], err="HTTP 400: Tenant is not eligible; a premium licence is required", code=400)
    out, st = await collectors.collect_pim_policies("tok", scope="/subscriptions/s1")
    assert out == {} and st.status == schema.STATUS_SKIPPED


# --------------------------------------------------------------------------- schema contract
async def test_eligibility_rows_carry_the_full_schema(monkeypatch):
    rows, _ = await _collect_eligibility(monkeypatch, [_eligibility()])
    assert set(rows[0].keys()) == set(schema.COLUMNS)


# --------------------------------------------------------------------------- grid lenses
def test_the_grid_exposes_eligible_and_elevated_as_server_side_lenses():
    """The PIM screen must be able to ask for ALL eligible grants, not the eligible ones that
    happen to land in a page.

    Found live: the tab pulled `tab=all&limit=200` from a 5,506-grant estate and filtered for
    `Eligible` in the browser, so it rendered "Eligible assignments (3)" next to a KPI reading
    137. A list that presents a page of itself as the complete set is worse than an error —
    the 154 assignments it omitted were invisible and unmentioned."""
    from app.api.iam import _TAB_FILTERS

    eligible = _row(assignmentState=schema.STATE_ELIGIBLE)
    active = _row(assignmentState=schema.STATE_ACTIVE)
    elevated = _row(assignmentState=schema.STATE_ACTIVE, activationExpiresOn="2026-08-02T00:00:00Z")

    assert "eligible" in _TAB_FILTERS and "elevated" in _TAB_FILTERS
    assert _TAB_FILTERS["eligible"](eligible) is True
    assert _TAB_FILTERS["eligible"](active) is False
    assert _TAB_FILTERS["elevated"](elevated) is True
    assert _TAB_FILTERS["elevated"](active) is False


def _row(**kw):
    base = dict(
        surface=schema.SURFACE_AZURE_RBAC,
        effect=schema.EFFECT_ALLOW,
        accessPath=schema.PATH_DIRECT,
        principalId="u-ivan",
        effectivePrincipalId="u-ivan",
        roleDefinitionId="/rd/owner",
        roleName="Owner",
        roleIsPrivileged=True,
        scope="/subscriptions/s1",
        assignmentId="a1",
    )
    base.update(kw)
    return schema.make_row(**base)
