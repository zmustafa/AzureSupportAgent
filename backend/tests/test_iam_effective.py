"""The effective-permission engine: *can principal P perform action A on resource R?*

Every rule tested here is one ARM actually enforces, and getting any of them wrong produces a
confident wrong answer — which is worse than no answer, because someone acts on it:

* a deny read as overridable          -> "Alice can delete this" when she provably cannot
* ``notActions`` read as a deny       -> "Alice is blocked" when a second role allows her
* planes conflated                    -> "Reader can read your blobs" (it cannot)
* scope direction reversed            -> an RG grant reported at a sibling RG
* an unevaluated condition read as ok -> a conditional grant reported as unconditional
* an uncollected role read as empty   -> Owner reported as granting nothing
"""
from __future__ import annotations

import pytest

from app.iam import effective, schema
from app.iam.effective import ALLOWED, DENIED, INDETERMINATE, NOT_GRANTED

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- fixtures
OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
BLOB_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
CONTRIB = "b24988ac-6180-42a0-ab88-20f7382dd24c"
CUSTOM = "11111111-2222-3333-4444-555555555555"

ROLE_DEFS = [
    {
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{OWNER}",
        "roleName": "Owner",
        "actions": ["*"],
        "notActions": [],
        "dataActions": [],
        "notDataActions": [],
    },
    {
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{READER}",
        "roleName": "Reader",
        "actions": ["*/read"],
        "notActions": [],
        "dataActions": [],
        "notDataActions": [],
    },
    {
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{CONTRIB}",
        "roleName": "Contributor",
        "actions": ["*"],
        "notActions": [
            "Microsoft.Authorization/*/Delete",
            "Microsoft.Authorization/*/Write",
            "Microsoft.Authorization/elevateAccess/Action",
        ],
        "dataActions": [],
        "notDataActions": [],
    },
    {
        "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{BLOB_READER}",
        "roleName": "Storage Blob Data Reader",
        "actions": ["Microsoft.Storage/storageAccounts/blobServices/containers/read"],
        "notActions": [],
        "dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"],
        "notDataActions": [],
    },
]

ROLE_INDEX = effective.build_role_index(ROLE_DEFS)

SUB = "/subscriptions/11111111-1111-1111-1111-111111111111"
RG = f"{SUB}/resourceGroups/prod"
RG2 = f"{SUB}/resourceGroups/dev"
SA = f"{RG}/providers/Microsoft.Storage/storageAccounts/acct"

VM_DELETE = "Microsoft.Compute/virtualMachines/delete"
VM_READ = "Microsoft.Compute/virtualMachines/read"
BLOB_READ = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"
ROLE_WRITE = "Microsoft.Authorization/roleAssignments/write"


def _row(**kw):
    base = {
        "surface": schema.SURFACE_AZURE_RBAC,
        "effect": schema.EFFECT_ALLOW,
        "assignmentState": schema.STATE_ACTIVE,
        "accessPath": schema.PATH_DIRECT,
        "principalId": "alice",
        "effectivePrincipalId": "alice",
        "scope": SUB,
        "scopeDisplayName": SUB,
        "assignmentId": "a1",
        "condition": "",
    }
    base.update(kw)
    return schema.make_row(**base)


def _grant(role_guid: str, role_name: str, **kw):
    return _row(
        roleDefinitionId=f"/providers/Microsoft.Authorization/roleDefinitions/{role_guid}",
        roleName=role_name,
        **kw,
    )


def _ev(rows, action=VM_DELETE, scope=RG, principal="alice", plane=""):
    return effective.evaluate(
        rows, ROLE_INDEX, principal_id=principal, scope=scope, action=action, plane=plane
    )


# --------------------------------------------------------------------------- wildcard matcher
@pytest.mark.parametrize(
    ("pattern", "action", "expected"),
    [
        ("*", VM_DELETE, True),
        ("Microsoft.Compute/*", VM_DELETE, True),
        ("Microsoft.Compute/*/read", VM_READ, True),
        # `*` spans segments, so a mid-pattern wildcard reaches nested resource types.
        ("Microsoft.Compute/*/read", "Microsoft.Compute/virtualMachines/extensions/read", True),
        ("*/read", VM_READ, True),
        ("*/read", VM_DELETE, False),
        ("Microsoft.Compute/virtualMachines/read", VM_READ, True),
        # Case-insensitive: ARM role definitions are inconsistent about casing.
        ("microsoft.compute/virtualmachines/READ", VM_READ, True),
        # The near-misses. `.` must be escaped or "Microsoft.Compute/*" matches "MicrosoftXCompute".
        ("Microsoft.Compute/*", "Microsoft.Computer/virtualMachines/read", False),
        ("Microsoft.Compute/*", "MicrosoftXCompute/virtualMachines/read", False),
        ("Microsoft.Compute/virtualMachines/read", "Microsoft.Compute/virtualMachines/readXX", False),
        ("Microsoft.Storage/*/read", "Microsoft.Compute/virtualMachines/read", False),
        ("", VM_READ, False),
        ("*", "", False),
    ],
)
def test_wildcard_matcher_golden_table(pattern, action, expected):
    assert effective.action_matches(pattern, action) is expected


def test_regex_metacharacters_in_a_pattern_are_literal():
    """A custom role name or action containing regex syntax must not become a pattern."""
    assert effective.action_matches("Microsoft.Foo/a+b/read", "Microsoft.Foo/a+b/read") is True
    assert effective.action_matches("Microsoft.Foo/a+b/read", "Microsoft.Foo/aab/read") is False


def test_any_matches_returns_the_pattern_not_a_bool():
    """The UI has to be able to say WHICH wildcard granted the access."""
    assert effective.any_matches(["*/read", "Microsoft.Compute/*"], VM_DELETE) == "Microsoft.Compute/*"
    assert effective.any_matches(["*/read"], VM_DELETE) == ""


# --------------------------------------------------------------------------- scope arithmetic
@pytest.mark.parametrize(
    ("assignment", "target", "covers"),
    [
        (SUB, RG, True),
        (RG, RG, True),
        (RG, SA, True),
        ("/", SA, True),
        # Below the target does not apply.
        (RG, SUB, False),
        (SA, RG, False),
        # A sibling resource group never applies.
        (RG2, RG, False),
        # The prefix trap: /subscriptions/abc must not cover /subscriptions/abcdef.
        ("/subscriptions/abc", "/subscriptions/abcdef", False),
        ("/subscriptions/abc", "/subscriptions/abc/resourceGroups/x", True),
        # Trailing slashes and casing are noise.
        (SUB + "/", RG, True),
        (SUB.upper(), RG, True),
    ],
)
def test_scope_covers(assignment, target, covers):
    assert effective.scope_covers(assignment, target) is covers


# --------------------------------------------------------------------------- evaluation order
def test_owner_at_subscription_allows_at_a_resource_group():
    dec = _ev([_grant(OWNER, "Owner", scope=SUB)])
    assert dec.verdict == ALLOWED
    assert dec.decided_by["roleName"] == "Owner"
    assert "Owner" in dec.reason


def test_an_assignment_below_the_target_does_not_grant():
    dec = _ev([_grant(OWNER, "Owner", scope=SA)], scope=RG)
    assert dec.verdict == NOT_GRANTED


def test_a_sibling_resource_group_does_not_grant():
    dec = _ev([_grant(OWNER, "Owner", scope=RG2)], scope=RG)
    assert dec.verdict == NOT_GRANTED


def test_a_deny_at_subscription_beats_an_owner_at_resource_scope():
    """Deny is evaluated FIRST and cannot be overridden, not even by Owner. Getting this
    backwards produces the single most dangerous wrong answer this engine can give."""
    rows = [
        _grant(OWNER, "Owner", scope=SA, assignmentId="allow-1"),
        _row(effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=SUB,
             roleName="Blueprint lock", assignmentId="deny-1"),
    ]
    dec = _ev(rows, scope=SA)
    assert dec.verdict == DENIED
    assert dec.decided_by["assignmentId"] == "deny-1"
    assert "cannot be overridden" in dec.reason


def test_a_deny_below_the_target_does_not_apply():
    rows = [
        _grant(OWNER, "Owner", scope=SUB),
        _row(effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=SA, roleName="lock"),
    ]
    assert _ev(rows, scope=RG).verdict == ALLOWED


def test_do_not_apply_to_child_scopes_confines_a_deny_to_its_own_scope():
    """A deny at RG with the flag set does not reach a resource inside it. Ignoring the flag
    reports people as blocked on resources the deny never touched."""
    deny = _row(
        effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=RG,
        roleName="lock", doNotApplyToChildScopes=True,
    )
    rows = [_grant(OWNER, "Owner", scope=SUB), deny]
    assert _ev(rows, scope=RG).verdict == DENIED
    assert _ev(rows, scope=SA).verdict == ALLOWED


def test_do_not_apply_to_child_scopes_accepts_a_string_flag():
    """Resource Graph projects the flag as the string "true"; ARM returns a real bool."""
    deny = _row(
        effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=RG,
        roleName="lock", doNotApplyToChildScopes="true",
    )
    rows = [_grant(OWNER, "Owner", scope=SUB), deny]
    assert _ev(rows, scope=SA).verdict == ALLOWED


# --------------------------------------------------------------------------- notActions
def test_not_actions_subtract_from_their_own_role():
    dec = _ev([_grant(CONTRIB, "Contributor", scope=SUB)], action=ROLE_WRITE)
    assert dec.verdict == NOT_GRANTED
    assert dec.not_action_exclusions
    assert "notActions" in dec.reason


def test_not_actions_are_a_subtraction_not_a_deny():
    """Contributor's notActions must not veto Owner's grant. Treating notActions as a deny
    reports an Owner as unable to assign roles."""
    rows = [
        _grant(CONTRIB, "Contributor", scope=SUB, assignmentId="c1"),
        _grant(OWNER, "Owner", scope=SUB, assignmentId="o1"),
    ]
    dec = _ev(rows, action=ROLE_WRITE)
    assert dec.verdict == ALLOWED
    assert dec.decided_by["roleName"] == "Owner"


def test_contributor_still_grants_what_it_does_not_subtract():
    dec = _ev([_grant(CONTRIB, "Contributor", scope=SUB)], action=VM_DELETE)
    assert dec.verdict == ALLOWED


# --------------------------------------------------------------------------- plane separation
def test_a_control_plane_wildcard_never_grants_a_data_action():
    """Owner has actions ["*"] and no dataActions. It does NOT grant blob data access —
    a real and frequently-misunderstood Azure behavior."""
    dec = _ev([_grant(OWNER, "Owner", scope=SUB)], action=BLOB_READ, plane=effective.PLANE_DATA)
    assert dec.verdict == NOT_GRANTED
    assert "data plane" in dec.reason


def test_reader_does_not_grant_blob_data_read():
    dec = _ev([_grant(READER, "Reader", scope=SUB)], action=BLOB_READ, plane=effective.PLANE_DATA)
    assert dec.verdict == NOT_GRANTED


def test_the_data_role_grants_the_data_action():
    dec = _ev(
        [_grant(BLOB_READER, "Storage Blob Data Reader", scope=SUB)],
        action=BLOB_READ, plane=effective.PLANE_DATA,
    )
    assert dec.verdict == ALLOWED


def test_a_data_role_does_not_grant_unrelated_control_actions():
    dec = _ev([_grant(BLOB_READER, "Storage Blob Data Reader", scope=SUB)], action=VM_DELETE)
    assert dec.verdict == NOT_GRANTED


def test_the_plane_is_inferred_when_not_supplied():
    assert effective.classify_plane(BLOB_READ) == effective.PLANE_DATA
    assert effective.classify_plane(VM_DELETE) == effective.PLANE_CONTROL
    # And the inference must actually be used.
    dec = _ev([_grant(OWNER, "Owner", scope=SUB)], action=BLOB_READ)
    assert dec.plane == effective.PLANE_DATA
    assert dec.verdict == NOT_GRANTED


# --------------------------------------------------------------------------- conditions
def test_an_abac_condition_yields_indeterminate_never_allowed():
    """Never claim a definitive Yes when an unevaluated condition is in the path."""
    row = _grant(OWNER, "Owner", scope=SUB, condition="@Resource[tag:env] StringEquals 'prod'")
    dec = _ev([row])
    assert dec.verdict == INDETERMINATE
    assert dec.condition_unevaluated
    assert "condition" in dec.reason


def test_an_unconditional_grant_wins_over_a_conditional_one():
    rows = [
        _grant(OWNER, "Owner", scope=SUB, condition="@Resource[tag:env] StringEquals 'prod'", assignmentId="cond"),
        _grant(OWNER, "Owner", scope=RG, assignmentId="plain"),
    ]
    dec = _ev(rows)
    assert dec.verdict == ALLOWED
    assert dec.decided_by["assignmentId"] == "plain"
    # …but the conditional one is still disclosed, because it may widen the answer elsewhere.
    assert dec.condition_unevaluated
    assert "not evaluated" in dec.reason


def test_a_deny_still_beats_a_conditional_grant():
    rows = [
        _grant(OWNER, "Owner", scope=SUB, condition="@Resource[tag:env] StringEquals 'prod'"),
        _row(effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=SUB, roleName="lock"),
    ]
    assert _ev(rows).verdict == DENIED


# --------------------------------------------------------------------------- unknown roles
def test_an_uncollected_role_definition_yields_indeterminate_not_not_granted():
    """An empty action list is indistinguishable from "we never fetched this role". Reporting
    not_granted would turn "we don't know what Owner grants" into "Owner grants nothing"."""
    row = _row(
        roleDefinitionId="/providers/Microsoft.Authorization/roleDefinitions/99999999-0000-0000-0000-000000000000",
        roleName="Some Uncollected Role",
        scope=SUB,
    )
    dec = _ev([row])
    assert dec.verdict == INDETERMINATE
    assert dec.unknown_roles == ["Some Uncollected Role"]


def test_a_known_role_alongside_an_unknown_one_still_allows():
    rows = [
        _row(roleDefinitionId="/providers/Microsoft.Authorization/roleDefinitions/deadbeef-0000-0000-0000-000000000000",
             roleName="Mystery", scope=SUB, assignmentId="m1"),
        _grant(OWNER, "Owner", scope=SUB, assignmentId="o1"),
    ]
    dec = _ev(rows)
    assert dec.verdict == ALLOWED
    # The uncertainty is still disclosed rather than dropped.
    assert dec.unknown_roles == ["Mystery"]
    assert "could not be resolved" in dec.reason


def test_role_action_set_knows_whether_it_was_collected():
    assert effective.role_action_set({"roleName": "X"}).known is False
    assert effective.role_action_set({"roleName": "X", "actions": ["*"]}).known is True


# --------------------------------------------------------------------------- non-ARM surfaces
def test_an_entra_directory_role_does_not_make_an_arm_question_indeterminate():
    """Found on a live tenant: 5 of 12 answers were non-answers because principals held Entra
    directory roles. A directory role has no ARM role definition BY DESIGN — treating that as an
    unresolved role is a category error, not an honest uncertainty. "Cannot determine whether
    they can delete a VM, because the permissions of Global Reader were never collected" is
    simply the wrong sentence."""
    row = _row(surface=schema.SURFACE_ENTRA, roleName="Global Reader", scope=SUB)
    dec = _ev([row])
    assert dec.verdict == NOT_GRANTED
    assert dec.unknown_roles == []


def test_a_key_vault_access_policy_does_not_grant_control_plane_actions():
    """Queried AT the vault, so the row is genuinely in scope and the surface rule is what
    rejects it. Asking at subscription scope would pass for the wrong reason: the vault row sits
    BELOW the subscription and gets filtered out before the surface rule is ever consulted."""
    vault = f"{RG}/providers/Microsoft.KeyVault/vaults/kv"
    row = _row(
        surface=schema.SURFACE_KEY_VAULT, roleName="Access Policy: secrets(all)",
        scope=vault, roleHasDataActions=True,
    )
    dec = _ev([row], action="Microsoft.KeyVault/vaults/delete", scope=vault,
              plane=effective.PLANE_CONTROL)
    assert dec.verdict == NOT_GRANTED
    assert dec.unknown_roles == []


def test_a_key_vault_access_policy_does_grant_data_plane_access_to_its_vault():
    """It is a real data-plane grant that appears in no roleAssignment. Skipping it entirely
    would under-report Key Vault access, which is the reason the collector exists."""
    vault = f"{RG}/providers/Microsoft.KeyVault/vaults/kv"
    row = _row(
        surface=schema.SURFACE_KEY_VAULT, roleName="Access Policy: secrets(all)",
        scope=vault, roleHasDataActions=True,
    )
    dec = _ev([row], action="Microsoft.KeyVault/vaults/secrets/getSecret",
              scope=vault, plane=effective.PLANE_DATA)
    assert dec.verdict == ALLOWED
    assert dec.unknown_roles == []


@pytest.mark.parametrize("name", ["CoAdministrator", "Co-Administrator", "ServiceAdministrator", "co administrator"])
def test_a_classic_co_administrator_is_treated_as_owner_equivalent(name):
    """Classic administrators DO grant broad ARM access. Skipping them because they have no role
    definition would under-report exactly the legacy access this product exists to surface.

    Parameterised over the spellings because ARM returns them unseparated ("CoAdministrator")
    while every human writes them hyphenated — matching the literal string classified every real
    classic admin as granting nothing."""
    row = _row(surface=schema.SURFACE_CLASSIC, roleName=name, scope=SUB)
    assert _ev([row], action=VM_DELETE).verdict == ALLOWED


def test_an_unrecognised_classic_role_is_not_assumed_to_grant():
    row = _row(surface=schema.SURFACE_CLASSIC, roleName="Billing Reader", scope=SUB)
    assert _ev([row], action=VM_DELETE).verdict == NOT_GRANTED


def test_a_classic_administrator_does_not_grant_data_plane_access():
    row = _row(surface=schema.SURFACE_CLASSIC, roleName="CoAdministrator", scope=SUB)
    dec = _ev([row], action=BLOB_READ, plane=effective.PLANE_DATA)
    assert dec.verdict == NOT_GRANTED


def test_an_unresolved_AZURE_RBAC_role_is_still_indeterminate():
    """The category rule must not become an excuse to swallow real collection gaps."""
    row = _row(
        surface=schema.SURFACE_AZURE_RBAC, roleName="Some Custom Role",
        roleDefinitionId="/providers/Microsoft.Authorization/roleDefinitions/00000000-1111-2222-3333-444444444444",
        scope=SUB,
    )
    dec = _ev([row])
    assert dec.verdict == INDETERMINATE
    assert dec.unknown_roles == ["Some Custom Role"]


def test_grant_sets_classify_non_arm_surfaces_rather_than_calling_them_unknown():
    rows = [
        _row(surface=schema.SURFACE_ENTRA, roleName="Global Reader", scope=SUB),
        _row(surface=schema.SURFACE_KEY_VAULT, roleName="Access Policy: secrets(all)", scope=SUB),
        _row(surface=schema.SURFACE_CLASSIC, roleName="CoAdministrator", scope=SUB),
    ]
    out = effective.effective_actions(rows, ROLE_INDEX, principal_id="alice", scope=SUB)
    assert out["unknownRoles"] == []
    assert [r["roleName"] for r in out["data"]] == ["Access Policy: secrets(all)"]
    assert [r["roleName"] for r in out["control"]] == ["CoAdministrator"]


# --------------------------------------------------------------------------- PIM eligibility
def test_eligible_but_not_active_access_is_not_current_access():
    """"Can they do it" and "could they activate and then do it" are different questions.
    Answering the second when asked the first reports standing access that is not standing."""
    row = _grant(OWNER, "Owner", scope=SUB, assignmentState=schema.STATE_ELIGIBLE)
    assert _ev([row]).verdict == NOT_GRANTED


# --------------------------------------------------------------------------- groups
def test_group_derived_access_names_the_group_chain():
    row = _grant(
        OWNER, "Owner", scope=SUB,
        principalId="grp-1", effectivePrincipalId="alice",
        accessPath=schema.PATH_GROUP, sourceGroupId="grp-1", sourceGroupName="AZ-Prod-Admins",
    )
    dec = _ev([row])
    assert dec.verdict == ALLOWED
    assert dec.via_groups == [
        {"groupId": "grp-1", "groupName": "AZ-Prod-Admins", "assignmentId": "a1"}
    ]
    assert "AZ-Prod-Admins" in dec.reason


def test_another_principals_access_is_not_mine():
    dec = _ev([_grant(OWNER, "Owner", scope=SUB, effectivePrincipalId="bob")], principal="alice")
    assert dec.verdict == NOT_GRANTED
    assert "no access anywhere" in dec.reason


# --------------------------------------------------------------------------- decider choice
def test_the_narrowest_granting_scope_is_reported_as_the_decider():
    """The assignment an operator would actually edit is the specific one, not the broad one."""
    rows = [
        _grant(OWNER, "Owner", scope=SUB, assignmentId="wide"),
        _grant(OWNER, "Owner", scope=RG, assignmentId="narrow"),
    ]
    dec = _ev(rows, scope=RG)
    assert dec.decided_by["assignmentId"] == "narrow"
    assert len(dec.granting) == 2


def test_the_decider_is_deterministic_when_two_grants_tie():
    """Two roles at the same scope both grant the action. An arbitrary winner makes two
    identical queries disagree, which destroys trust in both answers."""
    rows = [
        _grant(OWNER, "Owner", scope=SUB, assignmentId="o"),
        _grant(CONTRIB, "Contributor", scope=SUB, assignmentId="c"),
    ]
    first = _ev(rows)
    second = _ev(list(reversed(rows)))
    assert first.decided_by["assignmentId"] == second.decided_by["assignmentId"]
    # …and every grant is still listed, so the decider being one of them loses nothing.
    assert {g["assignmentId"] for g in first.granting} == {"o", "c"}


def test_a_wildcard_role_grants_an_action_that_does_not_exist():
    """Documents real Azure semantics rather than an intuition: `actions: ["*"]` matches ANY
    action string. A test asserting the opposite would be testing a rule Azure does not have."""
    dec = _ev([_grant(OWNER, "Owner", scope=SUB)], action="Microsoft.Nonexistent/thing/do")
    assert dec.verdict == ALLOWED


# --------------------------------------------------------------------------- grant sets
def test_effective_actions_returns_roles_not_expanded_action_strings():
    """A tenant-wide expansion is tens of thousands of strings nobody reads."""
    rows = [
        _grant(OWNER, "Owner", scope=SUB),
        _grant(BLOB_READER, "Storage Blob Data Reader", scope=RG, assignmentId="b1"),
    ]
    out = effective.effective_actions(rows, ROLE_INDEX, principal_id="alice", scope=SUB)
    assert [r["roleName"] for r in out["control"]] == ["Owner", "Storage Blob Data Reader"]
    assert [r["roleName"] for r in out["data"]] == ["Storage Blob Data Reader"]
    assert out["control"][0]["actionCount"] == 1


def test_effective_actions_surfaces_denies_and_unknown_roles():
    rows = [
        _row(effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=SUB, roleName="lock"),
        _row(roleDefinitionId="/providers/Microsoft.Authorization/roleDefinitions/nope-0000-0000-0000-000000000000",
             roleName="Mystery", scope=SUB),
    ]
    out = effective.effective_actions(rows, ROLE_INDEX, principal_id="alice", scope=SUB)
    assert out["denies"] and out["unknownRoles"] == ["Mystery"]


# --------------------------------------------------------------------------- inverse pivot
def test_who_can_lists_allowed_principals():
    rows = [
        _grant(OWNER, "Owner", scope=SUB, effectivePrincipalId="alice",
               effectivePrincipalName="Alice", assignmentId="a"),
        _grant(READER, "Reader", scope=SUB, effectivePrincipalId="bob",
               effectivePrincipalName="Bob", assignmentId="b"),
    ]
    out = effective.who_can(rows, ROLE_INDEX, scope=RG, action=VM_DELETE)
    assert [p["principalId"] for p in out["allowed"]] == ["alice"]


def test_who_can_excludes_a_principal_blocked_by_a_deny():
    """The reason this runs the full evaluator instead of a "who holds a matching role" query:
    that query would list a denied principal as able."""
    rows = [
        _grant(OWNER, "Owner", scope=SUB, effectivePrincipalId="alice", assignmentId="a"),
        _row(effect=schema.EFFECT_DENY, surface=schema.SURFACE_DENY, scope=SUB,
             effectivePrincipalId="alice", roleName="lock", assignmentId="d"),
    ]
    out = effective.who_can(rows, ROLE_INDEX, scope=RG, action=VM_DELETE)
    assert out["allowed"] == []


def test_who_can_keeps_indeterminate_separate_from_allowed():
    """An unevaluated condition is not a yes, and a reader scanning a list of names will not
    notice a per-row qualifier."""
    rows = [
        _grant(OWNER, "Owner", scope=SUB, effectivePrincipalId="alice", assignmentId="a"),
        _grant(OWNER, "Owner", scope=SUB, effectivePrincipalId="carol", assignmentId="c",
               condition="@Resource[tag:env] StringEquals 'prod'"),
    ]
    out = effective.who_can(rows, ROLE_INDEX, scope=RG, action=VM_DELETE)
    assert [p["principalId"] for p in out["allowed"]] == ["alice"]
    assert [p["principalId"] for p in out["indeterminate"]] == ["carol"]


# --------------------------------------------------------------------------- property tests
@pytest.mark.parametrize("role", ROLE_DEFS)
def test_every_action_a_role_lists_is_allowed_unless_subtracted(role):
    """Property: for any role, each of its own actions is granted unless a notAction covers it,
    and the two lists are never both satisfied."""
    rset = effective.role_action_set(role)
    for act in rset.actions:
        if "*" in act:
            continue
        granted, excluded = rset.grants(act, effective.PLANE_CONTROL)
        assert granted, f"{rset.role_name} does not grant its own action {act}"
        if excluded:
            assert effective.action_matches(excluded, act)


@pytest.mark.parametrize("role", ROLE_DEFS)
def test_no_role_grants_a_data_action_it_does_not_list(role):
    rset = effective.role_action_set(role)
    granted, _ = rset.grants("Microsoft.Fake/provider/data/read", effective.PLANE_DATA)
    assert granted == ""


def test_a_role_with_no_permissions_grants_nothing():
    rset = effective.role_action_set({"roleName": "Empty"})
    assert rset.grants(VM_DELETE, effective.PLANE_CONTROL) == ("", "")
    assert rset.grants(BLOB_READ, effective.PLANE_DATA) == ("", "")


# --------------------------------------------------------------------------- role index
def test_build_role_index_keys_on_the_guid_case_insensitively():
    idx = effective.build_role_index(ROLE_DEFS)
    assert idx[OWNER].role_name == "Owner"
    assert effective._guid_of(
        f"/providers/Microsoft.Authorization/roleDefinitions/{OWNER.upper()}"
    ) == OWNER


def test_merge_permissions_unions_multiple_blocks():
    """ARM allows several permission blocks per definition and unions them. Reading only
    ``permissions[0]`` silently drops grants on any multi-block role."""
    from app.iam.collectors import _merge_permissions

    actions, not_actions, data_actions, not_data = _merge_permissions(
        [
            {"actions": ["A/read"], "notActions": ["A/secret"]},
            {"actions": ["B/write"], "dataActions": ["C/data"], "notDataActions": ["C/nodata"]},
        ]
    )
    assert actions == ["A/read", "B/write"]
    assert not_actions == ["A/secret"]
    assert data_actions == ["C/data"]
    assert not_data == ["C/nodata"]


def test_the_index_resolves_by_role_name_when_there_is_no_definition_id():
    """Imported scanner rows and the demo dataset carry a role NAME but no full
    ``roleDefinitionId``. Without a name key the engine answers `indeterminate` for all of them.
    Safe because Azure enforces role-name uniqueness within a tenant."""
    idx = effective.build_role_index(ROLE_DEFS)
    row = {"roleName": "Owner", "roleDefinitionId": ""}
    assert effective._lookup(idx, row) is not None
    assert effective._lookup(idx, row).role_name == "Owner"
    # A definition id still wins when present.
    assert effective._lookup(idx, {"roleName": "Reader", "roleDefinitionId": f"/x/{OWNER}"}).role_name == "Owner"


# --------------------------------------------------------------------------- end to end
@pytest.fixture()
def demo_tenant(tmp_path, monkeypatch):
    from app.iam import cache, demo

    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    demo.seed_demo("t1")
    return "t1"


def _demo_ctx(tenant: str):
    from app.iam import cache, compose

    rows = compose.build_master_rows(tenant)
    idx = effective.build_role_index(cache.read_directory(tenant).get("role_defs", []))
    return rows, idx


def test_the_demo_tenant_can_actually_answer_the_flagship_question(demo_tenant):
    """The engine is only useful if it is wired to real cached data. The demo dataset is the
    one estate present in every install, so if it answers `indeterminate` the feature is
    invisible to anyone evaluating the product."""
    rows, idx = _demo_ctx(demo_tenant)
    assert idx, "demo role definitions must carry action sets"

    owners = [
        r for r in rows
        if r.get("roleName") == "Owner" and r.get("effect") != schema.EFFECT_DENY
        and r.get("assignmentState") != schema.STATE_ELIGIBLE
    ]
    assert owners, "precondition: the demo estate has an Owner"
    row = owners[0]
    dec = effective.evaluate(
        rows, idx,
        principal_id=str(row["effectivePrincipalId"]),
        scope=str(row["scope"]),
        action=VM_DELETE,
    )
    assert dec.verdict == ALLOWED
    assert dec.decided_by["roleName"] == "Owner"


def test_the_demo_owner_still_cannot_read_blob_data(demo_tenant):
    """The plane rule, demonstrated on the shipped dataset rather than a synthetic one."""
    rows, idx = _demo_ctx(demo_tenant)
    row = next(
        r for r in rows
        if r.get("roleName") == "Owner" and r.get("effect") != schema.EFFECT_DENY
        and r.get("assignmentState") != schema.STATE_ELIGIBLE
    )
    dec = effective.evaluate(
        rows, idx, principal_id=str(row["effectivePrincipalId"]),
        scope=str(row["scope"]), action=BLOB_READ,
    )
    assert dec.verdict == NOT_GRANTED


async def test_the_agent_tool_answers_from_the_demo_tenant(demo_tenant):
    from app.iam.agent_tool import build_iam_tools

    tools = {t.name: t for t in build_iam_tools(demo_tenant)}
    rows, _idx = _demo_ctx(demo_tenant)
    row = next(
        r for r in rows
        if r.get("roleName") == "Owner" and r.get("effect") != schema.EFFECT_DENY
        and r.get("assignmentState") != schema.STATE_ELIGIBLE
    )
    res = await tools["can_principal_do"].handler(
        {}, {"principal": str(row["effectivePrincipalId"]), "action": VM_DELETE, "scope": str(row["scope"])}
    )
    assert res["isError"] is False
    assert res["content"][0].startswith("YES")
    assert "Deciding assignment" in res["content"][0]


async def test_the_agent_tool_refuses_to_guess_between_two_principals(demo_tenant):
    """Silently picking the first match and reporting his access as somebody else's is the kind
    of wrong answer that gets acted on."""
    from app.iam.agent_tool import build_iam_tools

    tools = {t.name: t for t in build_iam_tools(demo_tenant)}
    res = await tools["can_principal_do"].handler(
        {}, {"principal": "a", "action": VM_DELETE, "scope": "/subscriptions/x"}
    )
    assert res["isError"] is True


async def test_the_agent_tool_never_phrases_indeterminate_as_a_yes_or_no(demo_tenant):
    """A model asked for a verdict will round "probably" to "yes"."""
    from app.iam.agent_tool import build_iam_tools

    tools = {t.name: t for t in build_iam_tools(demo_tenant)}
    rows, _idx = _demo_ctx(demo_tenant)
    row = next(r for r in rows if r.get("effectivePrincipalId"))
    res = await tools["can_principal_do"].handler(
        {}, {"principal": str(row["effectivePrincipalId"]),
             "action": "Microsoft.Nonexistent/thing/do", "scope": str(row["scope"])}
    )
    text = res["content"][0]
    assert text.startswith(("YES", "NO", "UNKNOWN"))
    if text.startswith("UNKNOWN"):
        assert "cannot be determined" in text
