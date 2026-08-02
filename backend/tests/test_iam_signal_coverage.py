"""Coverage for the signals the demo estate cannot exercise.

Measured before this file existed: **15 of 56 signals were never executed by anything** — they
do not fire on the demo dataset and no test named them. A signal with no coverage is not merely
untested, it is *unfalsifiable*: it renders as a pass on every screen whether it works or not,
and the two signals that mattered most here (`hyg.orphaned_assignment`,
`hyg.privileged_orphan`) had been structurally incapable of firing on real data for their
entire existence without a single test failing.

The first thing this file found: `ext.lighthouse_delegation` referenced
``schema.SURFACE_LIGHTHOUSE``, which did not exist. The signal never crashed only because its
`ctx.require` gate raised first — so the AttributeError was scheduled to appear on the day
Lighthouse collection was implemented, on any tenant with at least one assignment.

Each signal gets a POSITIVE case (fires on the shape it is about) and a NEGATIVE case (silent
on the shape it is not about, or honestly unmeasured when its input is missing).
"""
from __future__ import annotations

import pytest

from app.iam import cache, schema, signals as sig
from app.iam.signals import SignalContext, SignalUnavailable

SUB = "11111111-1111-1111-1111-111111111111"
SUB2 = "22222222-2222-2222-2222-222222222222"
MG = "/providers/Microsoft.Management/managementGroups/root"


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
        scopeType=schema.SCOPE_SUBSCRIPTION,
        subscriptionId=SUB,
        assignmentId="a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


def _ctx(rows=None, **kw) -> SignalContext:
    ctx = SignalContext(tenant_id="t1", rows=rows or [], kpis=kw.pop("kpis", {}),
                        scopes=kw.pop("scopes", []))
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _run(signal_id: str, ctx: SignalContext):
    spec = next(s for s in sig.all_signals() if s.id == signal_id)
    return spec.evaluate(ctx)


def _collector(name: str, status: str = schema.STATUS_SUCCEEDED) -> list[dict]:
    return [{"collectors": [{"collector": name, "status": status}]}]


# =========================================================================== hygiene
def test_orphaned_assignment_fires_for_a_deleted_principal():
    """Proven structurally dead on a live tenant: `_apply_principal_existence` marked every row
    `true`, so this signal could not fire at all. It had no test to notice."""
    rows = [_row(effectivePrincipalId="ghost", effectivePrincipalName="",
                 principalExists=schema.EXISTS_FALSE),
            _row(effectivePrincipalId="ghost2", effectivePrincipalName="",
                 principalExists=schema.EXISTS_FALSE, assignmentId="a2")]
    out = _run("hyg.orphaned_assignment", _ctx(rows))
    assert len(out) == 1, "aggregated per scope"
    assert out[0].count == 2
    assert out[0].subject == f"/subscriptions/{SUB}"


def test_orphaned_assignment_is_unmeasured_when_the_directory_could_not_be_read():
    """"Could not resolve" and "does not exist" are the same observation and opposite facts."""
    rows = [_row(principalExists=schema.EXISTS_UNKNOWN)]
    with pytest.raises(SignalUnavailable):
        _run("hyg.orphaned_assignment", _ctx(rows))


def test_orphaned_assignment_is_silent_when_every_principal_resolves():
    assert _run("hyg.orphaned_assignment", _ctx([_row()])) == []


def test_privileged_orphan_fires_only_for_privileged_deleted_principals():
    rows = [_row(effectivePrincipalId="ghost", principalExists=schema.EXISTS_FALSE,
                 roleName="Owner", roleIsPrivileged=True),
            _row(effectivePrincipalId="ghost2", principalExists=schema.EXISTS_FALSE,
                 roleName="Reader", assignmentId="a2")]
    out = _run("hyg.privileged_orphan", _ctx(rows))
    assert len(out) == 1
    assert out[0].count == 1, "the non-privileged orphan belongs to the other signal"
    assert out[0].severity == "error"


def test_privileged_orphan_is_silent_when_the_orphan_held_nothing_privileged():
    rows = [_row(effectivePrincipalId="ghost", principalExists=schema.EXISTS_FALSE)]
    assert _run("hyg.privileged_orphan", _ctx(rows)) == []


def test_unresolved_principals_reports_guids_without_calling_them_deleted():
    rows = [_row(effectivePrincipalId="guid-1", effectivePrincipalName="",
                 principalDisplayName="", principalExists=schema.EXISTS_UNKNOWN)]
    out = _run("hyg.unresolved_principals", _ctx(rows))
    assert len(out) == 1
    assert out[0].severity == "info"
    assert "NOT necessarily orphaned" in out[0].detail


def test_unresolved_principals_is_silent_when_names_resolved():
    assert _run("hyg.unresolved_principals", _ctx([_row()])) == []


def test_stale_scan_fires_only_for_scopes_past_their_ttl():
    fresh = _ctx([], scopes=[{"scope": "/s/1", "stale": False}])
    assert _run("hyg.stale_scan", fresh) == []
    stale = _ctx([], scopes=[{"scope": "/s/1", "stale": True, "displayName": "sub-1"}])
    out = _run("hyg.stale_scan", stale)
    assert len(out) == 1 and out[0].count == 1


# =========================================================================== external
def test_external_footprint_counts_distinct_guests():
    rows = [_row(effectivePrincipalId="g1",
                 effectivePrincipalUserPrincipalName="a_contoso.com#EXT#@x.onmicrosoft.com"),
            _row(effectivePrincipalId="g1", assignmentId="a2",
                 effectivePrincipalUserPrincipalName="a_contoso.com#EXT#@x.onmicrosoft.com"),
            _row(effectivePrincipalId="u1",
                 effectivePrincipalUserPrincipalName="staff@x.onmicrosoft.com", assignmentId="a3")]
    out = _run("ext.external_footprint", _ctx(rows))
    assert len(out) == 1
    assert out[0].count == 1, "two assignments held by one guest are one external identity"


def test_external_footprint_is_unmeasured_when_no_name_resolved():
    """Guest detection keys on #EXT# in the UPN, so with no names it cannot be measured — and
    reporting zero external identities would be the most reassuring possible wrong answer."""
    with pytest.raises(SignalUnavailable):
        _run("ext.external_footprint", _ctx([_row(effectivePrincipalUserPrincipalName="",
                                                  principalUserPrincipalName="")]))


def test_lighthouse_is_unmeasured_until_its_collector_runs():
    with pytest.raises(SignalUnavailable):
        _run("ext.lighthouse_delegation", _ctx([_row()]))


def test_lighthouse_delegation_fires_once_the_collector_has_run():
    """This is the test that caught `schema.SURFACE_LIGHTHOUSE` not existing.

    The reference sat inside a list comprehension guarded by `ctx.require`, so it was
    unreachable until the collector existed — an AttributeError scheduled for the exact moment
    somebody implemented the feature."""
    rows = [
        _row(surface=schema.SURFACE_LIGHTHOUSE, managingTenantId="managing-tenant",
             roleName="Contributor", roleIsPrivileged=True),
        _row(surface=schema.SURFACE_LIGHTHOUSE, managingTenantId="managing-tenant",
             roleName="Reader", assignmentId="a2"),
        _row(),  # ordinary RBAC, must not be attributed to a managing tenant
    ]
    out = _run("ext.lighthouse_delegation",
               _ctx(rows, scopes=_collector("AzureLighthouseDelegations")))
    assert len(out) == 1
    assert out[0].subject == "managing-tenant"
    assert out[0].count == 2
    assert out[0].severity == "error", "a privileged delegated role is not a warning"


def test_lighthouse_reports_a_run_that_found_nothing_as_nothing():
    out = _run("ext.lighthouse_delegation",
               _ctx([_row()], scopes=_collector("AzureLighthouseDelegations")))
    assert out == []


# =========================================================================== escalation
def test_fic_unknown_issuer_is_unmeasured_without_its_collector():
    with pytest.raises(SignalUnavailable):
        _run("esc.fic_unknown_issuer", _ctx([_row()]))


def test_fic_unknown_issuer_fires_for_an_unrecognised_issuer():
    ctx = _ctx([_row()], scopes=_collector("FederatedIdentityCredentials"),
               federated=[{"issuer": "https://evil.example.com", "subject": "repo:x/y:ref:main"},
                          {"issuer": "https://token.actions.githubusercontent.com",
                           "subject": "repo:org/repo:ref:refs/heads/main"}])
    out = _run("esc.fic_unknown_issuer", ctx)
    assert len(out) == 1
    assert out[0].count == 1, "GitHub Actions is a recognised issuer"
    assert "evil.example.com" in str(out[0].evidence)


def test_fic_unknown_issuer_is_silent_when_every_issuer_is_recognised():
    ctx = _ctx([_row()], scopes=_collector("FederatedIdentityCredentials"),
               federated=[{"issuer": "https://token.actions.githubusercontent.com"}])
    assert _run("esc.fic_unknown_issuer", ctx) == []


def test_escalation_from_guest_is_unmeasured_when_no_upn_resolved():
    with pytest.raises(SignalUnavailable):
        _run("esc.escalation_from_guest",
             _ctx([_row(effectivePrincipalUserPrincipalName="", principalUserPrincipalName="")]))


def test_escalation_from_guest_is_silent_when_the_guest_has_no_path():
    """A guest with only Reader must not appear. The signal is critical severity, so a false
    positive here is expensive."""
    rows = [_row(effectivePrincipalId="g1",
                 effectivePrincipalUserPrincipalName="a_contoso.com#EXT#@x.onmicrosoft.com")]
    assert _run("esc.escalation_from_guest", _ctx(rows)) == []


# =========================================================================== least privilege
def test_data_plane_breadth_fires_at_subscription_scope_and_wider():
    rows = [_row(roleName="Storage Blob Data Contributor", roleHasDataActions=True,
                 scopeType=schema.SCOPE_SUBSCRIPTION)]
    out = _run("lp.data_plane_breadth", _ctx(rows))
    assert len(out) == 1 and out[0].count == 1


def test_data_plane_breadth_is_silent_at_resource_scope():
    """The whole point is breadth. A data role on ONE storage account is normal."""
    rows = [_row(roleName="Storage Blob Data Contributor", roleHasDataActions=True,
                 scopeType=schema.SCOPE_RESOURCE,
                 scope=f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa")]
    assert _run("lp.data_plane_breadth", _ctx(rows)) == []


def test_data_plane_breadth_ignores_control_plane_roles_at_the_same_scope():
    assert _run("lp.data_plane_breadth", _ctx([_row(roleName="Contributor")])) == []


def test_direct_assignment_cluster_fires_at_the_threshold():
    from app.iam.signal_defs import lp

    rows = [_row(effectivePrincipalId=f"u{i}", effectivePrincipalName=f"U{i}",
                 assignmentId=f"a{i}", roleName="Contributor")
            for i in range(lp._GROUP_CLUSTER)]
    out = _run("lp.direct_assignment_cluster", _ctx(rows))
    assert len(out) == 1
    assert out[0].count == lp._GROUP_CLUSTER


def test_direct_assignment_cluster_is_silent_below_the_threshold():
    from app.iam.signal_defs import lp

    rows = [_row(effectivePrincipalId=f"u{i}", assignmentId=f"a{i}", roleName="Contributor")
            for i in range(lp._GROUP_CLUSTER - 1)]
    assert _run("lp.direct_assignment_cluster", _ctx(rows)) == []


def test_direct_assignment_cluster_ignores_service_principals():
    """Grouping service principals for unrelated workloads would couple them — the remediation
    this signal proposes would be actively wrong."""
    from app.iam.signal_defs import lp

    rows = [_row(effectivePrincipalId=f"sp{i}", assignmentId=f"a{i}", roleName="Contributor",
                 effectivePrincipalType="ServicePrincipal")
            for i in range(lp._GROUP_CLUSTER + 2)]
    assert _run("lp.direct_assignment_cluster", _ctx(rows)) == []


def test_direct_assignment_cluster_ignores_group_derived_rows():
    """Group-expanded rows ARE the fix this signal recommends. Counting them would make a
    correctly-grouped tenant look like the problem."""
    from app.iam.signal_defs import lp

    rows = [_row(effectivePrincipalId=f"u{i}", assignmentId=f"a{i}", roleName="Contributor",
                 accessPath=schema.PATH_GROUP)
            for i in range(lp._GROUP_CLUSTER + 2)]
    assert _run("lp.direct_assignment_cluster", _ctx(rows)) == []


# =========================================================================== privileged
def test_keyvault_dual_grant_model_is_unmeasured_without_its_collector():
    with pytest.raises(SignalUnavailable):
        _run("priv.keyvault_dual_grant_model", _ctx([_row()]))


def test_keyvault_dual_grant_model_fires_only_when_both_models_are_present():
    vault = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v1"
    both = [
        _row(surface=schema.SURFACE_KEY_VAULT, scope=vault, resourceName="v1"),
        _row(scope=vault, roleHasDataActions=True, resourceType="Microsoft.KeyVault/vaults",
             roleName="Key Vault Secrets User", assignmentId="a2"),
    ]
    out = _run("priv.keyvault_dual_grant_model",
               _ctx(both, scopes=_collector("KeyVaultAccessPolicies")))
    assert len(out) == 1 and out[0].subject == vault


def test_keyvault_with_policies_only_is_not_a_dual_model():
    vault = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v1"
    rows = [_row(surface=schema.SURFACE_KEY_VAULT, scope=vault, resourceName="v1")]
    assert _run("priv.keyvault_dual_grant_model",
                _ctx(rows, scopes=_collector("KeyVaultAccessPolicies"))) == []


# =========================================================================== structure
def test_assignment_limit_headroom_fires_near_the_ceiling():
    from app.iam.signal_defs import structure

    n = int(structure.MAX_ASSIGNMENTS_PER_SUBSCRIPTION * structure._WARN_AT) + 1
    rows = [_row(assignmentId=f"a{i}") for i in range(n)]
    out = _run("str.assignment_limit_headroom", _ctx(rows))
    assert len(out) == 1
    assert out[0].evidence.get("used") == n or out[0].count == n


def test_assignment_limit_headroom_is_silent_with_room_to_spare():
    rows = [_row(assignmentId=f"a{i}") for i in range(10)]
    assert _run("str.assignment_limit_headroom", _ctx(rows)) == []


def test_assignment_limit_headroom_counts_stored_assignments_not_effective_access():
    """The Azure limit counts stored objects. Counting group-expanded rows would report a
    tenant as near its ceiling because it uses groups correctly."""
    from app.iam.signal_defs import structure

    n = structure.MAX_ASSIGNMENTS_PER_SUBSCRIPTION + 50
    rows = [_row(assignmentId="shared", effectivePrincipalId=f"u{i}",
                 accessPath=schema.PATH_GROUP) for i in range(n)]
    assert _run("str.assignment_limit_headroom", _ctx(rows)) == []


def test_flat_scope_hierarchy_fires_when_every_subscription_hangs_off_the_root():
    scopes = [{"scopeType": schema.SCOPE_SUBSCRIPTION, "scope": f"/subscriptions/s{i}"}
              for i in range(3)]
    out = _run("str.flat_scope_hierarchy", _ctx([], scopes=scopes))
    assert len(out) == 1


def test_flat_scope_hierarchy_is_silent_when_management_groups_are_in_use():
    scopes = [{"scopeType": schema.SCOPE_SUBSCRIPTION, "scope": f"/subscriptions/s{i}",
               "managementGroupId": "mg-prod" if i else "mg-dev"} for i in range(3)]
    assert _run("str.flat_scope_hierarchy", _ctx([], scopes=scopes)) == []


def test_flat_scope_hierarchy_is_silent_for_a_small_estate():
    """Two subscriptions off the root is not a design problem worth reporting."""
    scopes = [{"scopeType": schema.SCOPE_SUBSCRIPTION, "scope": f"/subscriptions/s{i}"}
              for i in range(2)]
    assert _run("str.flat_scope_hierarchy", _ctx([], scopes=scopes)) == []


def test_flat_scope_hierarchy_is_unmeasured_with_no_scopes():
    with pytest.raises(SignalUnavailable):
        _run("str.flat_scope_hierarchy", _ctx([], scopes=[]))


# =========================================================================== bypass
def test_family_unreadable_is_unmeasured_before_the_sweep_runs():
    with pytest.raises(SignalUnavailable):
        _run("byp.family_unreadable", _ctx([], bypass_summary={}))


def test_family_unreadable_fires_for_a_family_that_could_not_be_read():
    summary = {"by_family": [
        {"family": "storage", "status": schema.STATUS_SUCCEEDED},
        {"family": "cosmos", "status": schema.STATUS_UNAUTHORIZED},
        {"family": "aks", "status": schema.STATUS_FAILED},
    ]}
    out = _run("byp.family_unreadable", _ctx([], bypass_summary=summary))
    assert len(out) == 1
    assert out[0].count == 2
    assert "cosmos" in out[0].detail and "aks" in out[0].detail


def test_family_unreadable_is_silent_when_every_family_was_read():
    summary = {"by_family": [{"family": "storage", "status": schema.STATUS_SUCCEEDED}]}
    assert _run("byp.family_unreadable", _ctx([], bypass_summary=summary)) == []


# =========================================================================== governance
def test_privileged_scope_unowned_is_unmeasured_when_no_ownership_is_recorded(monkeypatch):
    """Ownership is optional. With an empty registry every scope looks unowned, which would
    flag the entire estate on the strength of a feature nobody turned on."""
    from app.ownership import registry as ownership_registry

    monkeypatch.setattr(ownership_registry, "list_assignments", lambda t: [], raising=False)
    with pytest.raises(SignalUnavailable):
        _run("gov.privileged_scope_unowned", _ctx([_row(roleIsPrivileged=True)]))


def test_privileged_scope_unowned_fires_for_an_unowned_privileged_scope(monkeypatch):
    from app.ownership import registry as ownership_registry

    owned = f"/subscriptions/{SUB2}"
    monkeypatch.setattr(ownership_registry, "list_assignments",
                        lambda t: [{"resource_id": owned}], raising=False)
    rows = [
        _row(roleName="Owner", roleIsPrivileged=True, scope=f"/subscriptions/{SUB}"),
        _row(roleName="Owner", roleIsPrivileged=True, scope=owned, assignmentId="a2"),
    ]
    out = _run("gov.privileged_scope_unowned", _ctx(rows))
    assert [f.subject for f in out] == [f"/subscriptions/{SUB}"]


def test_privileged_scope_unowned_ignores_unprivileged_scopes(monkeypatch):
    from app.ownership import registry as ownership_registry

    monkeypatch.setattr(ownership_registry, "list_assignments",
                        lambda t: [{"resource_id": f"/subscriptions/{SUB2}"}], raising=False)
    assert _run("gov.privileged_scope_unowned", _ctx([_row()])) == []


# =========================================================================== cross-plane
def _entra(**kw):
    base = dict(surface=schema.SURFACE_ENTRA, scope="/", scopeType=schema.SCOPE_DIRECTORY)
    base.update(kw)
    return _row(**base)


def _dir_scope(*collectors: str):
    return [{"scope": "directory", "synthetic": True,
             "collectors": [{"collector": c, "status": schema.STATUS_SUCCEEDED} for c in collectors]}]


def test_global_admin_elevation_is_unmeasured_without_entra_roles():
    with pytest.raises(SignalUnavailable):
        _run("esc.global_admin_azure_elevation", _ctx([_row()]))


def test_global_admin_elevation_fires_for_each_global_administrator():
    """The most under-reported path in a tenant. A reviewer reading the Azure plane sees a short
    Owner list and certifies it, while a directory role invisible on that screen confers
    strictly more power than everything on it."""
    rows = [
        _entra(effectivePrincipalId="ga1", effectivePrincipalName="Ada",
               roleName="Global Administrator"),
        _entra(effectivePrincipalId="ga2", effectivePrincipalName="Bo",
               roleName="Global Administrator", assignmentId="a2"),
        _entra(effectivePrincipalId="r1", effectivePrincipalName="Cy",
               roleName="Global Reader", assignmentId="a3"),
    ]
    out = _run("esc.global_admin_azure_elevation",
               _ctx(rows, scopes=_dir_scope("EntraRoleAssignments")))
    assert {f.subject_label for f in out} == {"Ada", "Bo"}
    assert all(f.severity == "critical" for f in out)


def test_the_legacy_company_administrator_name_is_recognised():
    """Graph still returns `Company Administrator` on directories created before the rename.
    Matching only the modern name misses the most powerful role in the tenant."""
    rows = [_entra(effectivePrincipalId="ga1", effectivePrincipalName="Ada",
                   roleName="Company Administrator")]
    out = _run("esc.global_admin_azure_elevation",
               _ctx(rows, scopes=_dir_scope("EntraRoleAssignments")))
    assert len(out) == 1


def test_a_global_admin_who_already_elevated_is_still_reported_but_labelled():
    """The elevation stays available whether or not it has been used, so holding the root
    assignment already does not make the path go away."""
    rows = [
        _entra(effectivePrincipalId="ga1", effectivePrincipalName="Ada",
               roleName="Global Administrator"),
        _row(effectivePrincipalId="ga1", scope="/", scopeType=schema.SCOPE_TENANT,
             roleName="User Access Administrator", assignmentId="a9"),
    ]
    out = _run("esc.global_admin_azure_elevation",
               _ctx(rows, scopes=_dir_scope("EntraRoleAssignments")))
    assert len(out) == 1
    assert out[0].evidence["alreadyElevated"] is True
    assert "already hold" in out[0].detail


def test_sp_owner_signal_is_unmeasured_without_the_owners_collector():
    rows = [_entra(effectivePrincipalId="sp1", effectivePrincipalType="ServicePrincipal",
                   roleName="Application Administrator", roleIsPrivileged=True)]
    with pytest.raises(SignalUnavailable):
        _run("esc.sp_owner_to_directory_role",
             _ctx(rows, scopes=_dir_scope("EntraRoleAssignments")))


def test_sp_owner_to_directory_role_fires_for_an_owned_privileged_application():
    """Owning an application is being it: an owner can add a credential and authenticate as it,
    so the privilege transfers through a relationship no role-assignment screen shows."""
    rows = [
        _entra(effectivePrincipalId="sp1", effectivePrincipalName="deploy-app",
               effectivePrincipalType="ServicePrincipal",
               roleName="Application Administrator", roleIsPrivileged=True),
        # An ownership row: principalId is the OWNED application, effectivePrincipalId the human.
        _entra(principalId="sp1", principalDisplayName="deploy-app",
               effectivePrincipalId="u1", effectivePrincipalName="Dana",
               effectivePrincipalType="User", accessPath=schema.PATH_OWNER,
               roleName="Service Principal Owner", assignmentId="o1"),
    ]
    out = _run("esc.sp_owner_to_directory_role",
               _ctx(rows, scopes=_dir_scope("EntraRoleAssignments", "ServicePrincipalOwners")))
    assert len(out) == 1
    assert out[0].subject == "sp1"
    assert out[0].count == 1
    assert "Dana" in out[0].detail


def test_sp_owner_signal_ignores_an_application_with_no_privileged_role():
    rows = [
        _entra(effectivePrincipalId="sp1", effectivePrincipalType="ServicePrincipal",
               roleName="Directory Readers"),
        _entra(principalId="sp1", effectivePrincipalId="u1", effectivePrincipalName="Dana",
               accessPath=schema.PATH_OWNER, roleName="Service Principal Owner",
               assignmentId="o1"),
    ]
    assert _run("esc.sp_owner_to_directory_role",
                _ctx(rows, scopes=_dir_scope("EntraRoleAssignments", "ServicePrincipalOwners"))) == []


def test_an_ownership_row_is_not_mistaken_for_the_role_grant_itself():
    """When a service principal owns another service principal, the ownership row carries
    `effectivePrincipalType="ServicePrincipal"` and the privilege of what it owns. Counting it
    as a role grant would invent a second privileged application out of the relationship — and
    then report that application as owning itself."""
    rows = [
        _entra(effectivePrincipalId="sp-target", effectivePrincipalName="privileged-app",
               effectivePrincipalType="ServicePrincipal",
               roleName="Application Administrator", roleIsPrivileged=True),
        # sp-owner OWNS sp-target. This row is a relationship, not a grant — but it carries
        # `ServicePrincipal` and the privilege of what it owns, so without the guard it reads
        # as "sp-owner holds a privileged directory role".
        _entra(principalId="sp-target", principalDisplayName="privileged-app",
               effectivePrincipalId="sp-owner", effectivePrincipalName="ci-runner",
               effectivePrincipalType="ServicePrincipal", accessPath=schema.PATH_OWNER,
               roleName="Service Principal Owner", roleIsPrivileged=True, assignmentId="o1"),
        # …and a human owns sp-owner, which is what makes the mistake visible: a second finding
        # would appear for an application that holds no directory role at all.
        _entra(principalId="sp-owner", principalDisplayName="ci-runner",
               effectivePrincipalId="u1", effectivePrincipalName="Dana",
               effectivePrincipalType="User", accessPath=schema.PATH_OWNER,
               roleName="Service Principal Owner", assignmentId="o2"),
    ]
    out = _run("esc.sp_owner_to_directory_role",
               _ctx(rows, scopes=_dir_scope("EntraRoleAssignments", "ServicePrincipalOwners")))
    assert [f.subject for f in out] == ["sp-target"], (
        "only the application that actually holds the directory role is a finding"
    )
    assert "ci-runner" in out[0].detail


def test_sp_owner_signal_is_silent_when_a_privileged_application_has_no_owners():
    """No owner means no credential-write path. Reporting it anyway would make every
    correctly-managed application a finding."""
    rows = [_entra(effectivePrincipalId="sp1", effectivePrincipalType="ServicePrincipal",
                   roleName="Application Administrator", roleIsPrivileged=True)]
    assert _run("esc.sp_owner_to_directory_role",
                _ctx(rows, scopes=_dir_scope("EntraRoleAssignments", "ServicePrincipalOwners"))) == []


# =========================================================================== the guard itself
def test_every_collector_a_signal_requires_is_one_something_actually_emits():
    """A signal gated on a collector name nothing produces is permanently unmeasured.

    It never fires, never errors, and shows up only as a grey "not collected" row that looks
    like a permissions problem on the customer's side. Measured on the live `lu` tenant before
    this guard existed: **six signals were unmeasurable on every real tenant** — the
    managed-identity and federated-credential escalation checks, which are the most
    security-relevant detections in the product.

    Two separate causes, both invisible without this test:
      - the collector ran in the DIRECTORY layer, whose statuses never reached the signal
        context at all;
      - the collector exists under a different name on the Resource Graph path
        (`ArgKeyVaultAccessPolicies` vs `KeyVaultAccessPolicies`).
    """
    import pathlib
    import re

    from app.iam.signals import COLLECTOR_ALIASES

    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "iam"
    emitted: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        emitted |= set(re.findall(r'CollectorStatus\(\s*"([^"]+)"', text))
        # The demo dataset and the bulk-sweep shims write collector entries as plain dicts.
        emitted |= set(re.findall(r'"collector":\s*"([^"]+)"', text))

    asked: set[str] = set()
    for path in (root / "signal_defs").glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for call in re.finditer(r"collector_ran\(([^)]*)\)", text, re.S):
            asked |= set(re.findall(r'"([^"]+)"', call.group(1)))

    assert asked, "no signal requires a collector — the regex above has stopped matching"
    phantom = sorted(
        n for n in asked
        if n not in emitted and not (COLLECTOR_ALIASES.get(n, set()) & emitted)
    )
    assert not phantom, (
        "these signals require collectors that nothing emits, so they can never be measured: "
        f"{phantom}"
    )


def test_directory_layer_collectors_are_visible_to_signals(isolated_cache):
    """`ctx.collector_ran` walks `ctx.scopes`, and the directory layer is not a scope.

    Everything it collects — Entra directory roles, federated credentials, managed identities,
    service-principal owners — was therefore invisible to every signal."""
    from app.iam import cache as iam_cache
    from app.iam import findings

    iam_cache.write_directory(
        "t1",
        meta={"status": schema.STATUS_SUCCEEDED, "collectors": [
            {"collector": "EntraRoleAssignments", "status": schema.STATUS_SUCCEEDED},
            {"collector": "FederatedIdentityCredentials", "status": schema.STATUS_SUCCEEDED},
        ]},
        rows=[], role_defs=[], principals=[], groups={},
    )
    ctx = findings.build_context("t1")
    assert ctx.collector_ran("EntraRoleAssignments")
    assert ctx.collector_ran("FederatedIdentityCredentials")
    assert not ctx.collector_ran("SomethingNobodyRuns")


def test_a_collector_that_failed_in_the_directory_layer_is_not_treated_as_having_run(isolated_cache):
    from app.iam import cache as iam_cache
    from app.iam import findings

    iam_cache.write_directory(
        "t1",
        meta={"status": schema.STATUS_PARTIAL, "collectors": [
            {"collector": "EntraRoleAssignments", "status": schema.STATUS_UNAUTHORIZED},
        ]},
        rows=[], role_defs=[], principals=[], groups={},
    )
    assert not findings.build_context("t1").collector_ran("EntraRoleAssignments")


def test_an_arg_swept_tenant_satisfies_a_signal_that_names_the_arm_collector(isolated_cache):
    """`priv.keyvault_dual_grant_model` asks for `KeyVaultAccessPolicies`; the Resource Graph
    sweep emits `ArgKeyVaultAccessPolicies`. Every ARG-swept tenant reported that check as
    unmeasured — which is most of them, because ARG is the default path."""
    from app.iam import cache as iam_cache
    from app.iam import findings

    iam_cache.write_scope(
        "t1", "/subscriptions/s1", rows=[],
        meta={"scopeType": schema.SCOPE_SUBSCRIPTION, "displayName": "sub-1",
              "collectors": [{"collector": "ArgKeyVaultAccessPolicies",
                              "status": schema.STATUS_SUCCEEDED}]},
    )
    assert findings.build_context("t1").collector_ran("KeyVaultAccessPolicies")


def test_every_signal_is_now_exercised_by_something(isolated_cache):
    """The invariant this file exists to establish, asserted rather than assumed.

    A signal that neither fires on the demo estate nor appears in any test is unfalsifiable: it
    reads as a pass on every screen whether or not it works. This test fails when a new signal
    is added without either demo coverage or a test naming it."""
    import pathlib

    from app.iam import demo, findings

    demo.seed_demo("t1")
    fired = {r.spec.id for r in findings.evaluate("t1") if r.findings}
    suite = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in pathlib.Path(__file__).parent.glob("test_iam*.py")
    )
    unexercised = sorted(
        s.id for s in sig.all_signals() if s.id not in fired and s.id not in suite
    )
    assert not unexercised, (
        "these signals are never executed by anything — they cannot fail, so they cannot be "
        f"trusted: {unexercised}"
    )
