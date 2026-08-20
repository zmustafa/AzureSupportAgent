"""P7 governance workflow: run diffing, attribution, remediation artifacts, framework mapping
and access review campaigns.

The tests that matter here are the ones about what the product REFUSES to do: auto-approving an
expired campaign, guessing an actor, emitting a revoke with no rollback, or showing a control as
passing when nothing measured it. Each of those is a plausible, convenient behavior that would
make the feature look better and the evidence worthless.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.iam import attribution, cache, campaigns, diff, frameworks, remediation, schema

SUB = "11111111-1111-1111-1111-111111111111"
SUB2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _row(**kw):
    base = dict(
        surface=schema.SURFACE_AZURE_RBAC,
        effect=schema.EFFECT_ALLOW,
        assignmentState=schema.STATE_ACTIVE,
        accessPath=schema.PATH_DIRECT,
        principalId="alice",
        effectivePrincipalId="alice",
        effectivePrincipalName="Alice",
        effectivePrincipalType="User",
        roleDefinitionId="/rd/reader",
        roleName="Reader",
        scope=f"/subscriptions/{SUB}",
        scopeDisplayName="sub-1",
        assignmentId="a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


def _classes(result):
    return {c["class"] for c in result["changes"]}


# =========================================================================== diff keys
def test_the_diff_is_keyed_on_the_effective_principal_not_the_assignment_holder():
    """A user who gains access by being added to a GROUP is the most common way privilege
    appears. A principalId-keyed diff sees the group's assignment unchanged and reports nothing
    at all — the change is invisible precisely when it matters."""
    group_grant = _row(
        principalId="group-1", effectivePrincipalId="group-1",
        effectivePrincipalName="Admins", roleName="Owner", roleDefinitionId="/rd/owner",
    )
    # Same assignment, now expanded to a member. principalId is unchanged; the ACCESS is not.
    member_grant = _row(
        principalId="group-1", effectivePrincipalId="bob", effectivePrincipalName="Bob",
        roleName="Owner", roleDefinitionId="/rd/owner", accessPath=schema.PATH_GROUP,
    )
    out = diff.compute([group_grant], [group_grant, member_grant])
    assert out["total"] == 1
    assert out["changes"][0]["principalName"] == "Bob"
    assert out["changes"][0]["class"] == diff.ADDED


def test_assignment_state_is_part_of_the_key_so_an_activation_is_a_change():
    before = [_row(assignmentState=schema.STATE_ELIGIBLE, roleName="Owner", roleDefinitionId="/rd/owner")]
    after = [_row(assignmentState=schema.STATE_ACTIVE, roleName="Owner", roleDefinitionId="/rd/owner")]
    out = diff.compute(before, after)
    assert _classes(out) == {diff.ACTIVATED}
    assert out["counts_by_class"][diff.ACTIVATED] == 1


def test_a_deactivation_is_reported_and_does_not_worsen():
    before = [_row(assignmentState=schema.STATE_ACTIVE, roleName="Owner", roleDefinitionId="/rd/owner")]
    after = [_row(assignmentState=schema.STATE_ELIGIBLE, roleName="Owner", roleDefinitionId="/rd/owner")]
    out = diff.compute(before, after)
    assert _classes(out) == {diff.DEACTIVATED}
    assert out["changes"][0]["worsens"] is False


def test_key_set_hash_is_stable_under_reordering_and_changes_with_content():
    a, b = _row(assignmentId="a1"), _row(assignmentId="a2", roleName="Owner", roleDefinitionId="/rd/owner")
    assert diff.key_set_hash([a, b]) == diff.key_set_hash([b, a])
    assert diff.key_set_hash([a]) != diff.key_set_hash([a, b])


def test_rows_without_an_effective_principal_id_do_not_collapse_into_one_phantom_grant():
    """An imported scanner row may carry only a display name. Keying those to the empty string
    would merge every one of them into a single fake assignment."""
    r1 = _row(effectivePrincipalId="", principalId="", effectivePrincipalName="Alice")
    r2 = _row(effectivePrincipalId="", principalId="", effectivePrincipalName="Bob", assignmentId="a2")
    assert diff.row_key(r1) != diff.row_key(r2)


# =========================================================================== change classes
def test_all_nine_change_classes_are_produced():
    """Every class in the vocabulary must be reachable. A class nobody can produce is a lie in
    the filter dropdown."""
    seen: set[str] = set()

    seen |= _classes(diff.compute([], [_row()]))                                     # added
    seen |= _classes(diff.compute([_row()], []))                                     # removed
    seen |= _classes(diff.compute(
        [_row()], [_row(roleName="Owner", roleDefinitionId="/rd/owner")]))           # escalated
    seen |= _classes(diff.compute(
        [_row(roleName="Owner", roleDefinitionId="/rd/owner")], [_row()]))           # de_escalated
    seen |= _classes(diff.compute(
        [_row()], [_row(scope="/")]))                                                # re_scoped
    seen |= _classes(diff.compute(
        [_row(assignmentState=schema.STATE_ELIGIBLE)], [_row()]))                    # activated
    seen |= _classes(diff.compute(
        [_row()], [_row(assignmentState=schema.STATE_ELIGIBLE)]))                    # deactivated
    seen |= _classes(diff.compute(
        [_row()], [_row(accessPath=schema.PATH_GROUP)]))                             # path_changed
    seen |= _classes(diff.compute(
        [_row()], [_row(principalExists=schema.EXISTS_FALSE)]))                      # orphaned

    assert seen == set(diff.CHANGE_CLASSES), f"unreachable: {set(diff.CHANGE_CLASSES) - seen}"


def test_an_escalation_and_a_de_escalation_are_told_apart_by_tier_not_by_name():
    up = diff.compute([_row()], [_row(roleName="Owner", roleDefinitionId="/rd/owner")])
    down = diff.compute([_row(roleName="Owner", roleDefinitionId="/rd/owner")], [_row()])
    assert up["changes"][0]["class"] == diff.ESCALATED
    assert down["changes"][0]["class"] == diff.DE_ESCALATED
    assert up["changes"][0]["to"]["tier"] > up["changes"][0]["from"]["tier"]


def test_an_unknown_custom_role_is_never_reported_as_a_de_escalation_from_reader():
    """Assuming an unclassified role grants nothing manufactures de-escalations out of roles
    nobody has got round to categorising."""
    custom = _row(roleName="Contoso Deployment Operator", roleDefinitionId="/rd/custom")
    assert diff.privilege_tier(custom) > diff.privilege_tier(_row())
    out = diff.compute([_row()], [custom])
    assert out["changes"][0]["class"] == diff.ESCALATED


def test_a_re_scope_records_whether_it_went_broader():
    """`/subscriptions/x` to `/` is one character of visual diff and a tenant-wide grant."""
    broader = diff.compute([_row()], [_row(scope="/")])["changes"][0]
    narrower = diff.compute([_row()], [_row(scope=f"/subscriptions/{SUB}/resourceGroups/rg1")])["changes"][0]
    assert broader["broader"] is True
    assert narrower["broader"] is False


def test_a_path_change_is_reported_even_though_the_access_is_identical():
    """Access that was direct and became group-derived is now controlled by whoever manages the
    group. Same permissions, different governance, and only one of those is on the row."""
    out = diff.compute([_row()], [_row(accessPath=schema.PATH_GROUP)])
    assert out["changes"][0]["class"] == diff.PATH_CHANGED


def test_orphaned_is_detected_on_a_surviving_row_not_only_on_a_key_change():
    """The grant is untouched; the principal behind it stopped resolving. That is not a key
    change, so detecting it only from added/removed would miss it entirely."""
    before = [_row(principalExists=schema.EXISTS_TRUE)]
    after = [_row(principalExists=schema.EXISTS_FALSE)]
    out = diff.compute(before, after)
    assert diff.ORPHANED in _classes(out)


def test_a_principal_that_was_already_unknown_does_not_re_report_as_newly_orphaned():
    same = [_row(principalExists=schema.EXISTS_FALSE)]
    assert diff.ORPHANED not in _classes(diff.compute(same, list(same)))


def test_one_human_change_is_one_diff_entry_not_a_removed_plus_an_added():
    out = diff.compute([_row()], [_row(roleName="Owner", roleDefinitionId="/rd/owner")])
    assert out["total"] == 1, "a role change split into two unrelated lines the reader must correlate by eye"


def test_the_diff_is_bounded_and_says_when_it_truncated():
    after = [_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}") for i in range(50)]
    out = diff.compute([], after, max_changes=10)
    assert len(out["changes"]) == 10
    assert out["total"] == 50 and out["truncated"] is True


def test_worsening_counts_only_the_changes_that_increase_risk():
    out = diff.compute(
        [_row(assignmentId="a2", roleName="Owner", roleDefinitionId="/rd/owner")],
        [_row()],
    )
    # Reader appears, Owner disappears — neither is an escalation.
    assert out["worsening"] == 0


def test_a_deny_assignment_never_counts_as_privilege_when_tiering():
    assert diff.privilege_tier(_row(effect=schema.EFFECT_DENY, roleName="Owner")) == diff.TIER_NONE


# =========================================================================== attribution
def _event(**kw):
    base = {
        "operation": "Microsoft.Authorization/roleAssignments/write",
        "resourceId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleAssignments/a1",
        "eventTime": "2026-08-01T09:00:00Z",
        "actor": "bob@contoso.com",
        "actorObjectId": "bob",
        "actorKind": "User",
        "actorIp": "203.0.113.9",
        "correlationId": "corr-1",
        "raw": {},
    }
    base.update(kw)
    return base


def test_the_activity_log_window_is_clamped_to_real_retention_and_the_clamp_is_reported():
    """`directoryAudits` REJECTS an over-long filter with a 400 rather than returning less — the
    whole source is lost if the query is not clamped. Assume the same class of behavior."""
    _s, _e, note = attribution.clamp_window(365)
    assert note and "90" in note
    _s, _e, quiet = attribution.clamp_window(7)
    assert quiet == ""


def test_a_clamped_window_still_returns_a_usable_range():
    start, end, _ = attribution.clamp_window(365)
    span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    assert span.days == attribution.ACTIVITY_LOG_RETENTION_DAYS


def test_an_exact_assignment_id_match_attributes_the_actor():
    change = {"assignmentId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleAssignments/a1",
              "scope": f"/subscriptions/{SUB}"}
    stats = attribution.attribute_all([change], [_event()])
    assert change["actor"]["actorDisplayName"] == "bob@contoso.com"
    assert change["actor"]["confidence"] == attribution.CONFIDENCE_EXACT
    assert stats["attributed_exact"] == 1


def test_an_unmatched_change_is_unknown_and_never_blank():
    """Leaving `actor` blank reads as 'nobody did this', which is never true."""
    change = {"assignmentId": "/nope", "scope": "/subscriptions/other"}
    attribution.attribute_all([change], [_event()])
    assert change["actor"]["confidence"] == "unknown"
    assert change["actor"]["changeSource"] == attribution.SOURCE_UNKNOWN
    assert change["actor"] is not None


def test_an_ambiguous_scope_match_refuses_to_name_anybody():
    """Two authorization events on the same scope inside the window: there is no honest way to
    say which produced the change. Naming one of two possible people is worse than naming
    neither, and it is the failure that puts the wrong name in an audit report."""
    change = {"assignmentId": "", "scope": f"/subscriptions/{SUB}"}
    two = [_event(actor="bob@contoso.com"), _event(actor="carol@contoso.com", resourceId=f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleAssignments/a2")]
    attribution.attribute_all([change], two)
    assert change["actor"]["confidence"] == "unknown"


def test_an_unambiguous_scope_match_is_marked_inferred_not_exact():
    change = {"assignmentId": "", "scope": f"/subscriptions/{SUB}"}
    attribution.attribute_all([change], [_event()])
    assert change["actor"]["confidence"] == attribution.CONFIDENCE_INFERRED


def test_only_authorization_operations_are_considered():
    assert attribution.is_authorization_event("Microsoft.Authorization/roleAssignments/write")
    assert not attribution.is_authorization_event("Microsoft.Compute/virtualMachines/write")
    change = {"assignmentId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleAssignments/a1", "scope": ""}
    attribution.attribute_all([change], [_event(operation="Microsoft.Compute/virtualMachines/write")])
    assert change["actor"]["confidence"] == "unknown"


@pytest.mark.parametrize(
    "agent,expected",
    [
        ("HashiCorp Terraform/1.5 (+https://www.terraform.io)", attribution.SOURCE_TERRAFORM),
        ("AzureCLI/2.53.0", attribution.SOURCE_CLI),
        ("Mozilla/5.0 AzurePortal", attribution.SOURCE_PORTAL),
        ("azure-sdk-for-python", attribution.SOURCE_ARM),
    ],
)
def test_change_source_distinguishes_iac_from_a_human_at_the_keyboard(agent, expected):
    """'Granted by Terraform' and 'granted by a person at 2am' are different findings."""
    assert attribution.infer_change_source(_event(userAgent=agent)) == expected


def test_terraform_is_matched_before_the_generic_sdk_string_it_is_built_on():
    """Terraform's agent contains Go-http-client and the CLI's contains python-requests. An
    unordered match would classify every IaC change as a generic SDK call."""
    assert attribution.infer_change_source(
        _event(userAgent="Go-http-client/2.0 HashiCorp Terraform")
    ) == attribution.SOURCE_TERRAFORM


def test_a_pim_operation_is_attributed_to_pim_regardless_of_user_agent():
    assert attribution.infer_change_source(
        _event(operation="Microsoft.Authorization/roleEligibilityScheduleRequests/write",
               userAgent="AzurePortal")
    ) == attribution.SOURCE_PIM


def test_iac_and_human_source_sets_do_not_overlap():
    assert not (attribution.IAC_SOURCES & attribution.HUMAN_SOURCES)


# =========================================================================== remediation
def test_every_generated_action_carries_a_rollback():
    """A revoke script with no way back is not a remediation, it is an outage waiting for a
    change-advisory board."""
    for fmt in remediation.FORMATS:
        action = remediation.revoke_assignment(_row(), fmt)
        assert action["rollback"].strip(), f"{fmt} emitted a revoke with no rollback"
        assert action["breaks_if"].strip()
        assert action["dry_run"].strip()


def test_the_bundle_contains_a_rollback_section_for_every_action():
    actions = [remediation.revoke_assignment(_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}"), "az") for i in range(3)]
    bundle = remediation.build_bundle(actions, "az", title="t")
    assert "===== ROLLBACK =====" in bundle["script"]
    for a in actions:
        assert a["rollback"] in bundle["script"]


def test_the_revoke_and_undo_halves_are_offered_separately():
    """They are run at different times by different people. A single blob means whoever reaches
    for the rollback has to select the right half of it by hand, under pressure."""
    actions = [remediation.revoke_assignment(_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}"), "az") for i in range(3)]
    b = remediation.build_bundle(actions, "az", title="t")

    # Each half stands alone: no revoke command leaks into the undo, and vice versa.
    for a in actions:
        assert a["command"] in b["revoke_script"]
        assert a["command"] not in b["rollback_script"]
        assert a["rollback"] in b["rollback_script"]
        assert a["rollback"] not in b["revoke_script"]

    # Both carry the provenance and the warning, because either can be copied on its own.
    for half in (b["revoke_script"], b["rollback_script"]):
        assert remediation.GENERATOR_VERSION in half
        assert "NOT RUN BY THE PRODUCT" in half
    # …and the combined document still exists for anything that stored one script.
    assert b["revoke_script"] in b["script"] and b["rollback_script"] in b["script"]


def test_the_undo_runs_in_the_reverse_order_of_the_revoke():
    """The revoke removes group membership first. Restoring it first would briefly hand back
    more than the person started with."""
    direct = remediation.revoke_assignment(_row(accessPath=schema.PATH_DIRECT, effectivePrincipalId="p-d"), "az")
    via = remediation.revoke_assignment(
        _row(accessPath=schema.PATH_GROUP, sourceGroupId="g-1", effectivePrincipalId="p-g"), "az"
    )
    b = remediation.build_bundle([direct, via], "az", title="t")
    assert b["revoke_script"].index(via["command"]) < b["revoke_script"].index(direct["command"])
    assert b["rollback_script"].index(direct["rollback"]) < b["rollback_script"].index(via["rollback"])


def test_a_reduce_grants_the_narrower_role_before_revoking_the_wider_one():
    """Revoking first leaves a window with no access at all. On a deployment identity or a
    break-glass account that window is an incident."""
    action = remediation.reduce_assignment(_row(roleName="Owner"), "Reader", "az")
    script = action["command"]
    assert script.index("role assignment create") < script.index("role assignment delete")


def test_group_derived_access_is_ordered_before_direct_assignments():
    """Revoking a direct grant while the principal still inherits the same access through a
    group looks successful and changes nothing — 'we revoked it' and 'they still have it' end
    up both being true."""
    direct = remediation.revoke_assignment(_row(accessPath=schema.PATH_DIRECT), "az")
    via_group = remediation.revoke_assignment(_row(accessPath=schema.PATH_GROUP), "az")
    assert via_group["order_hint"] < direct["order_hint"]
    bundle = remediation.build_bundle([direct, via_group], "az")
    # Compared by plane rather than identity: the bundle folds duplicate memberships and hands
    # back a copy carrying the list of grants the single step covers.
    assert bundle["actions"][0]["plane"] == remediation.PLANE_GROUP_MEMBERSHIP
    assert bundle["actions"][1]["plane"] == remediation.PLANE_AZURE_RBAC


def test_broader_scopes_are_ordered_before_narrower_ones():
    wide = remediation.revoke_assignment(_row(scope="/"), "az")
    narrow = remediation.revoke_assignment(_row(scope=f"/subscriptions/{SUB}/resourceGroups/rg1"), "az")
    assert wide["order_hint"] < narrow["order_hint"]


def test_bicep_does_not_pretend_to_have_a_delete_verb():
    """Emitting a fake 'delete' resource would be a lie that deploys cleanly and changes
    nothing, which is the worst possible outcome for a remediation artifact."""
    action = remediation.revoke_assignment(_row(), remediation.BICEP)
    assert "DELETE the resource block" in action["command"]
    assert "Complete mode" in action["command"]


def test_terraform_revocation_tells_you_to_import_before_planning():
    action = remediation.revoke_assignment(_row(), remediation.TERRAFORM)
    assert "terraform import" in action["command"]


def test_a_bundle_that_would_contain_a_secret_is_refused_not_scrubbed():
    """A hit means one of OUR templates is wrong. Silently scrubbing it hides the bug and the
    next template gets it right by accident."""
    with pytest.raises(remediation.SecretLeak):
        remediation.assert_no_secrets("az storage account show-connection-string ... ConnectionString=abc")
    with pytest.raises(remediation.SecretLeak):
        remediation.assert_no_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9x")


def test_no_generated_bundle_contains_a_secret():
    for fmt in remediation.FORMATS:
        actions = [remediation.revoke_assignment(_row(), fmt), remediation.reduce_assignment(_row(), "Reader", fmt)]
        remediation.build_bundle(actions, fmt)  # raises if a template regresses


def test_the_bundle_header_names_its_provenance_and_says_it_is_not_run_for_you():
    bundle = remediation.build_bundle(
        [remediation.revoke_assignment(_row(), "az")], "az", title="Campaign X", run_id="r1", campaign_id="c1"
    )
    for needle in ("Campaign X", "r1", "c1", remediation.GENERATOR_VERSION, "NOT RUN BY THE PRODUCT"):
        assert needle in bundle["script"]


def test_an_approve_decision_generates_nothing():
    assert remediation.for_decision(_row(), campaigns.APPROVE, "az") is None
    assert remediation.for_decision(_row(), campaigns.REVOKE, "az") is not None


def test_quoting_survives_a_principal_name_containing_a_quote():
    action = remediation.revoke_assignment(_row(effectivePrincipalId="o'brien"), "az")
    assert "o''brien" in action["command"]


# =========================================================================== frameworks
class _FakeResult:
    def __init__(self, spec, findings_list, measured):
        self.spec = spec
        self.findings = findings_list
        self.measured = measured


class _FakeSpec:
    def __init__(self, sid, refs):
        self.id = sid
        self.frameworks = tuple(refs)


def test_a_control_whose_signals_could_not_run_is_not_measured_never_passing():
    """This is the artifact an auditor relies on. It will be wrong in the direction of comfort
    unless a blind control is stated as blind."""
    out = frameworks.map_results([_FakeResult(_FakeSpec("x.y", ["CIS-Azure:3.8"]), [], measured=False)])
    control = out["controls"][0]
    assert control["state"] == frameworks.NOT_MEASURED
    assert control["state"] != frameworks.PASS


def test_a_control_passes_only_when_measured_and_clean():
    passing = frameworks.map_results([_FakeResult(_FakeSpec("x.y", ["CIS-Azure:3.8"]), [], measured=True)])
    failing = frameworks.map_results([_FakeResult(_FakeSpec("x.y", ["CIS-Azure:3.8"]), ["f"], measured=True)])
    assert passing["controls"][0]["state"] == frameworks.PASS
    assert failing["controls"][0]["state"] == frameworks.FAIL


def test_a_control_with_one_blind_and_one_clean_signal_still_reports_what_it_measured():
    out = frameworks.map_results([
        _FakeResult(_FakeSpec("a", ["NIST:AC-6"]), [], measured=False),
        _FakeResult(_FakeSpec("b", ["NIST:AC-6"]), [], measured=True),
    ])
    control = out["controls"][0]
    assert control["state"] == frameworks.PASS
    assert control["measured_signals"] == 1 and len(control["signals"]) == 2


def test_the_mapping_always_states_that_it_is_not_a_full_framework_assessment():
    """A reader taking this as an assessment would be relying on coverage that was never
    claimed, and unmapped controls are ABSENT rather than passing."""
    out = frameworks.map_results([])
    assert any("NOT a full assessment" in l for l in out["limitations"])
    assert any("absent from this table" in l for l in out["limitations"])


def test_an_unparseable_control_reference_is_kept_rather_than_dropped():
    out = frameworks.map_results([_FakeResult(_FakeSpec("x", ["SOMETHING"]), [], measured=True)])
    assert out["controls"][0]["framework"] == "Other"
    assert out["controls"][0]["control"] == "SOMETHING"


def test_controls_sort_numerically_so_3_8_precedes_3_10():
    out = frameworks.map_results([
        _FakeResult(_FakeSpec("a", ["CIS-Azure:3.10"]), [], measured=True),
        _FakeResult(_FakeSpec("b", ["CIS-Azure:3.8"]), [], measured=True),
    ])
    assert [c["control"] for c in out["controls"]] == ["3.8", "3.10"]


def test_every_framework_reference_in_the_shipped_registry_parses():
    from app.iam import signals as sig

    for spec in sig.all_signals():
        for ref in spec.frameworks:
            family, control = frameworks.parse_ref(ref)
            assert family and control, f"{spec.id} carries an unusable framework ref {ref!r}"


def test_the_registry_actually_maps_to_the_frameworks_it_claims():
    covered = frameworks.covered_controls()
    for family in ("CIS-Azure", "NIST", "MCSB"):
        assert covered.get(family), f"no signal maps to {family}"


# =========================================================================== campaign selectors
def test_a_privileged_selector_picks_privileged_rows_only():
    rows = [_row(roleIsPrivileged=True, roleName="Owner"), _row(assignmentId="a2")]
    assert len(campaigns.select_rows(rows, {"kind": "privileged"})) == 1


def test_deny_rows_are_never_put_in_front_of_a_reviewer():
    """A deny assignment grants nothing. Asking a human to certify one wastes the only resource
    this feature really spends, which is their attention."""
    rows = [_row(effect=schema.EFFECT_DENY, roleIsPrivileged=True), _row(roleIsPrivileged=True)]
    assert len(campaigns.select_rows(rows, {"kind": "privileged"})) == 1


def test_a_scope_selector_includes_everything_beneath_the_scope():
    rows = [_row(), _row(assignmentId="a2", scope=f"/subscriptions/{SUB}/resourceGroups/rg1"), _row(assignmentId="a3", scope=f"/subscriptions/{SUB2}")]
    got = campaigns.select_rows(rows, {"kind": "scope", "scope_id": f"/subscriptions/{SUB}"})
    assert len(got) == 2


def test_an_external_selector_finds_guests_by_upn_marker():
    guest = _row(effectivePrincipalUserPrincipalName="x_contoso.com#EXT#@fabrikam.onmicrosoft.com")
    assert len(campaigns.select_rows([guest, _row(assignmentId="a2")], {"kind": "external"})) == 1


def test_an_unknown_selector_kind_is_refused_rather_than_matching_everything():
    """A selector that silently matches nothing creates an empty campaign; one that silently
    matches everything creates a review of the entire estate. Both are worse than an error."""
    with pytest.raises(campaigns.CampaignError):
        campaigns.select_rows([_row()], {"kind": "whatever"})


def test_selection_is_deterministic_and_puts_privileged_first():
    rows = [_row(assignmentId="a1"), _row(assignmentId="a2", roleIsPrivileged=True, roleName="Owner")]
    first = campaigns.select_rows(rows, {"kind": "scope", "scope_id": f"/subscriptions/{SUB}"})
    second = campaigns.select_rows(list(reversed(rows)), {"kind": "scope", "scope_id": f"/subscriptions/{SUB}"})
    assert [r["assignmentId"] for r in first] == [r["assignmentId"] for r in second]
    assert first[0]["roleIsPrivileged"] is True


# =========================================================================== reviewers
def test_nobody_is_ever_assigned_to_review_their_own_access():
    """A reviewer asked to certify their own access will approve it, and an audit trail that
    records that as certification is actively misleading."""
    row = _row(effectivePrincipalId="alice")
    reviewer, source = campaigns.resolve_reviewer(row, strategy=campaigns.BY_OWNER, tenant_id="t1", fallback="alice")
    assert reviewer != "alice"
    assert source == "unassigned"


def test_an_unassignable_item_says_so_rather_than_defaulting_to_the_subject():
    row = _row(effectivePrincipalId="alice")
    reviewer, source = campaigns.resolve_reviewer(row, strategy=campaigns.BY_OWNER, tenant_id="t1")
    assert reviewer == "" and source == "unassigned"


def test_a_fallback_reviewer_is_used_when_ownership_resolves_to_nobody():
    row = _row(effectivePrincipalId="alice")
    reviewer, source = campaigns.resolve_reviewer(row, strategy=campaigns.BY_OWNER, tenant_id="t1", fallback="carol")
    assert (reviewer, source) == ("carol", "fallback")


def test_self_review_is_available_but_flagged_as_attestation_only():
    row = _row(effectivePrincipalId="alice")
    reviewer, source = campaigns.resolve_reviewer(row, strategy=campaigns.SELF, tenant_id="t1")
    assert (reviewer, source) == ("alice", campaigns.SELF)


def test_a_service_principal_gets_no_invented_manager():
    """A service principal has no manager. Inventing one puts a decision in front of somebody
    who cannot make it, and they will approve it to clear their queue."""
    assert campaigns._manager_for("t1", "some-sp") == ""


# =========================================================================== item context
def test_item_context_never_presents_unmeasured_usage_as_unused():
    """A 'last used: never' that actually means 'never measured' gets access revoked on the
    strength of a blank."""
    ctx = campaigns.build_context(_row())
    assert ctx["usage"] is None
    assert "never measured" in ctx["usageNote"]


def test_item_context_carries_why_the_access_is_held():
    ctx = campaigns.build_context(_row(accessPath=schema.PATH_GROUP, groupChain="Admins > Alice"))
    assert ctx["why"] == schema.PATH_GROUP
    assert ctx["groupChain"] == "Admins > Alice"


# =========================================================================== evidence
def _campaign(**kw):
    base = {
        "id": "c1", "name": "Q3 privileged review", "status": campaigns.COMPLETED,
        "attestation_only": False, "baseline_run_id": "r1",
        "stats": {"total": 10, "decided": 6, "undecided": 4, "complete": False},
    }
    base.update(kw)
    return base


def test_the_evidence_pack_states_that_undecided_items_were_not_approved():
    """Auto-approving on expiry manufactures the exact evidence the auditor came for, out of
    nothing. The pack has to say the opposite explicitly."""
    content = campaigns.evidence_content(_campaign(), [])
    assert any("NOT approved" in s for s in content["statements"])
    assert content["completeness"]["undecided"] == 4
    assert content["completeness"]["complete"] is False


def test_the_evidence_pack_labels_a_self_attestation_campaign_as_not_certification():
    content = campaigns.evidence_content(_campaign(attestation_only=True), [])
    assert any("not independent certification" in s for s in content["statements"])


def test_the_evidence_pack_never_claims_the_product_applied_anything():
    content = campaigns.evidence_content(_campaign(), [])
    assert any("never writes to Azure" in s for s in content["statements"])


def test_the_evidence_digest_is_stable_and_content_sensitive():
    a = campaigns.evidence_content(_campaign(), [])
    b = campaigns.evidence_content(_campaign(), [])
    c = campaigns.evidence_content(_campaign(name="different"), [])
    assert campaigns.content_digest(a) == campaigns.content_digest(b)
    assert campaigns.content_digest(a) != campaigns.content_digest(c)


# =========================================================================== drift signals
def _drift_ctx(changes, **kw):
    from app.iam import signals as sig

    return sig.SignalContext(
        tenant_id="t1", rows=[], kpis={}, scopes=[],
        drift={"changes": changes, "available": True},
        drift_available=True,
        **kw,
    )


def _change(**kw):
    base = {
        "class": diff.ADDED, "key": "k1", "principalId": "alice", "principalName": "Alice",
        "scope": f"/subscriptions/{SUB}", "roleName": "Owner", "privileged": True,
        "actor": dict(attribution.UNKNOWN_ACTOR),
    }
    base.update(kw)
    return base


def test_drift_signals_report_not_measured_when_there_is_nothing_to_compare_against():
    """A tenant with one scan has no baseline. An empty change list would otherwise read as
    'nothing has changed' — false on every first run."""
    from app.iam import signals as sig
    from app.iam.signal_defs import drift as drift_defs

    ctx = sig.SignalContext(tenant_id="t1", rows=[], kpis={}, scopes=[], drift_available=False)
    for spec in drift_defs.SIGNALS:
        with pytest.raises(sig.SignalUnavailable):
            spec.evaluate(ctx)


def test_new_privileged_access_produces_a_finding():
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_privileged_added")
    out = spec.evaluate(_drift_ctx([_change()]))
    assert len(out) == 1 and "Alice" in out[0].detail


def test_after_hours_is_judged_in_local_time_not_utc():
    """Judging raw UTC calls a Tokyo morning suspicious and misses a London midnight entirely."""
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_after_hours")
    change = _change(actor={**attribution.UNKNOWN_ACTOR, "eventTimestamp": "2026-08-01T23:30:00Z"})
    # UTC 23:30 is the middle of the night in London...
    assert spec.evaluate(_drift_ctx([change], utc_offset_minutes=0))
    # ...and 08:30 the next morning in Tokyo, which is not suspicious at all.
    assert not spec.evaluate(_drift_ctx([change], utc_offset_minutes=540))


def test_a_self_grant_is_detected_and_is_critical_when_privileged():
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_self_grant")
    change = _change(actor={**attribution.UNKNOWN_ACTOR, "actorPrincipalId": "alice"})
    out = spec.evaluate(_drift_ctx([change]))
    assert len(out) == 1 and out[0].severity == "critical"


def test_a_grant_by_somebody_else_is_not_a_self_grant():
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_self_grant")
    change = _change(actor={**attribution.UNKNOWN_ACTOR, "actorPrincipalId": "bob"})
    assert spec.evaluate(_drift_ctx([change])) == []


def test_an_unattributed_change_is_never_reported_as_a_self_grant():
    """An empty actor id equals an empty principal id under a naive comparison, which would
    accuse every unattributed change of being a self-grant."""
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_self_grant")
    change = _change(principalId="", actor=dict(attribution.UNKNOWN_ACTOR))
    assert spec.evaluate(_drift_ctx([change])) == []


def test_out_of_band_needs_an_iac_baseline_before_it_calls_anything_out_of_band():
    """In a click-ops estate every change is 'out of band'; that signal is noise, and noise
    trains people to ignore the findings list."""
    from app.iam import signals as sig
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_out_of_band")
    portal_only = [_change(actor={**attribution.UNKNOWN_ACTOR, "changeSource": attribution.SOURCE_PORTAL})]
    with pytest.raises(sig.SignalUnavailable):
        spec.evaluate(_drift_ctx(portal_only))


def test_out_of_band_fires_when_the_estate_demonstrably_uses_iac():
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_out_of_band")
    mixed = [
        _change(key="k1", actor={**attribution.UNKNOWN_ACTOR, "changeSource": attribution.SOURCE_TERRAFORM}),
        _change(key="k2", actor={**attribution.UNKNOWN_ACTOR, "changeSource": attribution.SOURCE_PORTAL}),
    ]
    out = spec.evaluate(_drift_ctx(mixed))
    assert len(out) == 1 and out[0].subject == "k2"


def test_a_removed_and_re_added_grant_is_reported_as_a_revert():
    from app.iam.signal_defs import drift as drift_defs

    spec = next(s for s in drift_defs.SIGNALS if s.id == "gov.drift_reverted")
    out = spec.evaluate(_drift_ctx([_change(class_="x", **{"class": diff.REMOVED}), _change()]))
    assert len(out) == 1


def test_every_drift_signal_is_registered_in_the_gov_pillar():
    from app.iam import signals as sig

    ids = {s.id for s in sig.all_signals()}
    for expected in ("gov.drift_privileged_added", "gov.drift_out_of_band", "gov.drift_after_hours",
                     "gov.drift_self_grant", "gov.drift_reverted"):
        assert expected in ids, f"{expected} is not discoverable through the registry"


# =========================================================================== run retention
@pytest.mark.anyio
async def test_the_newest_run_keeps_its_rows_and_older_unpinned_runs_do_not(isolated_cache, monkeypatch):
    """Regression, found only by running it.

    `save_run` pruned older snapshots with `IamScanRun.id != run.id`. The primary key is a
    Python-side default assigned at FLUSH time, so `run.id` was still None when the statement
    was built — and SQLAlchemy renders `!= None` as `IS NOT NULL`, which matches every row
    including the one just written. The clause that existed to keep the newest snapshot deleted
    it, the rolling buffer retained nothing, and the classified diff could never find a baseline
    on any tenant. No unit test touched `save_run` because it needs a database."""
    from app.iam import demo, store

    tenant = f"retention-{uuid.uuid4().hex[:8]}"
    demo.seed_demo(tenant)

    first = await store.save_run(tenant, trigger="test")
    assert first["rows_retained"] is True, "the run just written must keep its own rows"
    assert await store.run_rows(tenant, first["id"])

    second = await store.save_run(tenant, trigger="test")
    assert second["rows_retained"] is True
    # Exactly one unpinned snapshot is retained: the newest.
    assert await store.run_rows(tenant, first["id"]) is None
    # …and having a baseline is what makes the classified diff possible at all.
    assert (second["diff"] or {}).get("classified_available") is True


@pytest.mark.anyio
async def test_a_pinned_run_keeps_its_rows_when_the_buffer_rolls(isolated_cache):
    """A pinned run is the only thing that makes "show me who had privileged access on 1 April"
    answerable later."""
    from app.iam import demo, store

    tenant = f"pinned-{uuid.uuid4().hex[:8]}"
    demo.seed_demo(tenant)

    first = await store.save_run(tenant, trigger="test")
    pinned = await store.pin_run(tenant, first["id"], reason="quarterly baseline")
    assert pinned and pinned["pinned"] is True

    await store.save_run(tenant, trigger="test")
    assert await store.run_rows(tenant, first["id"]), "a pinned run must survive the rolling buffer"


@pytest.mark.anyio
async def test_pinning_a_run_whose_rows_are_already_gone_is_refused(isolated_cache):
    """Pinning an empty snapshot would claim a fidelity the record does not have — and it is the
    record an auditor would later be handed."""
    from app.iam import demo, store

    tenant = f"gone-{uuid.uuid4().hex[:8]}"
    demo.seed_demo(tenant)
    first = await store.save_run(tenant, trigger="test")
    await store.save_run(tenant, trigger="test")

    result = await store.pin_run(tenant, first["id"])
    assert result is not None
    assert result["pinned"] is False
    assert "already discarded" in result.get("error", "")
