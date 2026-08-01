"""Identity blast-radius graph: escalation primitives and the dangling-edge invariant.

The first test in this file is the one that matters operationally. Cytoscape rejects an
entire element batch when a single edge references a node that is not present, which blanks
the canvas — a whole-screen failure caused by one orphan. Every scope builder is checked
against that invariant, on empty data and on populated data.

The rest of the file proves each escalation primitive one at a time, positively and
negatively, because a derived edge nobody can justify is worse than a missing one.
"""
from __future__ import annotations

import pytest

from app.entra import blastradius as br

GA_ID = "rd-ga"
APP_ADMIN = "Application Administrator"


def _definitions():
    return [
        {"id": GA_ID, "display_name": "Global Administrator", "tier": "tier0",
         "privileged": True, "is_built_in": True},
        {"id": "rd-appadmin", "display_name": APP_ADMIN, "tier": "tier1",
         "privileged": True, "is_built_in": True},
        {"id": "rd-reader", "display_name": "Global Reader", "tier": "tier2",
         "privileged": False, "is_built_in": True},
    ]


def _assignment(pid, rid, name, *, ptype="User", privileged=True, **kw):
    base = {"id": f"a-{pid}-{rid}", "principal_id": pid, "principal_type": ptype,
            "principal_name": name, "role_id": rid, "role_name": _role_name(rid),
            "role_privileged": privileged, "role_tier": "tier0", "permanent": True}
    base.update(kw)
    return base


def _role_name(rid):
    return {d["id"]: d["display_name"] for d in _definitions()}.get(rid, rid)


def _data(*, users=(), groups=(), sps=(), applications=(), assignments=(), derived=(),
          eligible=()):
    return {
        "people": {"users": list(users), "groups": list(groups)},
        "apps": {"service_principals": list(sps), "applications": list(applications)},
        "roles": {"definitions": _definitions(), "assignments": list(assignments),
                  "group_derived": list(derived), "eligible": list(eligible)},
    }


def _sp(oid, name, permissions=()):
    return {"object_id": oid, "display_name": name, "app_id": f"app-{oid}",
            "sp_type": "Application", "enabled": True, "owner_ids": [],
            "granted_app_permissions": [{"permission": p, "tier": "critical"} for p in permissions]}


def _assert_no_dangling(result):
    present = {n["id"] for n in result["nodes"]}
    for edge in result["edges"]:
        assert edge["source"] in present, f"dangling source {edge['source']}"
        assert edge["target"] in present, f"dangling target {edge['target']}"
        assert edge["source"] != edge["target"], "self-loop"


# =============================================== the invariant that blanks the canvas
@pytest.mark.parametrize("scope_kind,scope_id", [
    ("privileged", ""), ("escalation", ""), ("principal", "u1"),
    ("application", "sp1"), ("role", GA_ID), ("policy", "p1"), ("nonsense", "x"),
])
def test_no_scope_ever_emits_a_dangling_edge_on_empty_data(scope_kind, scope_id):
    result = br.build(_data(), {}, scope_kind=scope_kind, scope_id=scope_id)
    _assert_no_dangling(result)


@pytest.mark.parametrize("scope_kind,scope_id", [
    ("privileged", ""), ("escalation", ""), ("principal", "u1"),
    ("application", "sp1"), ("role", GA_ID),
])
def test_no_scope_ever_emits_a_dangling_edge_on_populated_data(scope_kind, scope_id):
    data = _data(
        users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True},
               {"id": "u2", "upn": "b@x", "display_name": "Bob", "enabled": True}],
        groups=[{"id": "g1", "display_name": "Admins", "is_assignable_to_role": True,
                 "owner_ids": ["u2"], "owners_known": True}],
        sps=[_sp("sp1", "CI Deploy", ["RoleManagement.ReadWrite.Directory"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "display_name": "CI Deploy",
                       "owner_ids": ["u1"]}],
        assignments=[_assignment("u1", GA_ID, "Alice"),
                     _assignment("g1", GA_ID, "Admins", ptype="Group")],
        derived=[_assignment("u2", GA_ID, "", source_group_id="g1", source_group_name="Admins")],
    )
    result = br.build(data, {}, scope_kind=scope_kind, scope_id=scope_id)
    _assert_no_dangling(result)


def test_dropped_edges_are_counted_rather_than_hidden():
    result = br._finish(  # noqa: SLF001
        [br._node("a", br.KIND_USER, "A")],  # noqa: SLF001
        [br._edge("a", "ghost", br.EDGE_OWNS)],  # noqa: SLF001
    )
    assert result["edges"] == []
    assert result["stats"]["dropped_edges"] == 1


def test_node_cap_is_enforced_and_reported():
    nodes = [br._node(f"n{i}", br.KIND_USER, str(i)) for i in range(br.MAX_NODES + 50)]  # noqa: SLF001
    result = br._finish(nodes, [])  # noqa: SLF001
    assert result["stats"]["node_count"] == br.MAX_NODES
    assert result["truncated"] is True


# ================================================= escalation primitives, one by one
def test_app_owner_of_a_role_writing_sp_escalates_to_global_admin():
    data = _data(
        users=[{"id": "u1", "upn": "owner@x", "display_name": "Owner", "enabled": True}],
        sps=[_sp("sp1", "CI Deploy", ["RoleManagement.ReadWrite.Directory"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "display_name": "CI Deploy",
                       "owner_ids": ["u1"]}],
    )
    edges = br.escalation_edges(data)
    owner_edges = [e for e in edges if e["source"] == br.user_id("u1")]
    assert len(owner_edges) == 1
    assert owner_edges[0]["target"] == br.role_id(GA_ID)
    assert owner_edges[0]["data"]["primitive"] == "app_owner_role_write"
    assert "grant itself Global Administrator" in owner_edges[0]["data"]["reason"]


def test_owning_a_harmless_application_is_not_an_escalation():
    data = _data(
        users=[{"id": "u1", "upn": "owner@x", "display_name": "Owner", "enabled": True}],
        sps=[_sp("sp1", "Report Reader", ["User.Read.All"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "owner_ids": ["u1"]}],
    )
    assert [e for e in br.escalation_edges(data) if e["source"] == br.user_id("u1")] == []


def test_consent_grant_permission_escalates_to_global_admin():
    data = _data(sps=[_sp("sp1", "Grantor", ["AppRoleAssignment.ReadWrite.All"])])
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "consent_grant"]
    assert len(edges) == 1
    assert edges[0]["source"] == br.sp_id("sp1")


def test_application_administrator_can_seize_a_powerful_service_principal():
    data = _data(
        users=[{"id": "u1", "upn": "appadmin@x", "display_name": "App Admin", "enabled": True}],
        sps=[_sp("sp1", "CI Deploy", ["RoleManagement.ReadWrite.Directory"])],
        assignments=[{"principal_id": "u1", "principal_type": "User",
                      "principal_name": "App Admin", "role_id": "rd-appadmin",
                      "role_name": APP_ADMIN, "role_privileged": True}],
    )
    edges = [e for e in br.escalation_edges(data)
             if e["data"]["primitive"] == "app_admin_credential_write"]
    assert len(edges) == 1
    assert edges[0]["source"] == br.user_id("u1")
    assert edges[0]["target"] == br.sp_id("sp1")


def test_application_administrator_with_no_powerful_sp_escalates_nowhere():
    data = _data(
        users=[{"id": "u1", "upn": "appadmin@x", "display_name": "App Admin", "enabled": True}],
        sps=[_sp("sp1", "Harmless", ["User.Read.All"])],
        assignments=[{"principal_id": "u1", "principal_type": "User",
                      "principal_name": "App Admin", "role_id": "rd-appadmin",
                      "role_name": APP_ADMIN, "role_privileged": True}],
    )
    assert [e for e in br.escalation_edges(data)
            if e["data"]["primitive"] == "app_admin_credential_write"] == []


def test_owner_of_a_role_assignable_group_inherits_its_roles():
    data = _data(
        users=[{"id": "u2", "upn": "groupowner@x", "display_name": "Owner", "enabled": True}],
        groups=[{"id": "g1", "display_name": "Tenant Admins", "is_assignable_to_role": True,
                 "owner_ids": ["u2"], "owners_known": True}],
        assignments=[_assignment("g1", GA_ID, "Tenant Admins", ptype="Group")],
    )
    edges = [e for e in br.escalation_edges(data)
             if e["data"]["primitive"] == "group_owner_role"]
    assert len(edges) == 1
    assert edges[0]["source"] == br.user_id("u2")
    assert edges[0]["target"] == br.role_id(GA_ID)


def test_owner_of_a_non_assignable_group_is_not_an_escalation():
    data = _data(
        users=[{"id": "u2", "upn": "groupowner@x", "display_name": "Owner", "enabled": True}],
        groups=[{"id": "g1", "display_name": "Engineering", "is_assignable_to_role": False,
                 "owner_ids": ["u2"], "owners_known": True}],
        assignments=[_assignment("g1", GA_ID, "Engineering", ptype="Group")],
    )
    assert [e for e in br.escalation_edges(data)
            if e["data"]["primitive"] == "group_owner_role"] == []


def test_application_write_permission_can_seize_another_powerful_application():
    """Found by live testing: a real tenant's service principal held Application.ReadWrite.All
    and the escalation map showed nothing at all."""
    data = _data(sps=[
        _sp("sp1", "CI Deploy", ["Application.ReadWrite.All"]),
        _sp("sp2", "Role Writer", ["RoleManagement.ReadWrite.Directory"]),
    ])
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "application_write"]
    assert len(edges) == 1
    assert edges[0]["source"] == br.sp_id("sp1")
    assert edges[0]["target"] == br.sp_id("sp2")
    assert "authenticate as it" in edges[0]["data"]["reason"]


def test_application_write_alone_is_still_reported_against_the_role():
    """The capability is the finding even when nothing powerful exists to seize yet."""
    data = _data(sps=[_sp("sp1", "CI Deploy", ["Application.ReadWrite.All"])])
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "application_write"]
    assert len(edges) == 1
    assert edges[0]["target"] == br.role_id(GA_ID)


def test_an_application_cannot_seize_itself():
    """Holding both permissions is one path to Global Administrator, not two arrows and a
    self-loop."""
    data = _data(sps=[_sp("sp1", "Both", ["Application.ReadWrite.All",
                                          "RoleManagement.ReadWrite.Directory"])])
    edges = br.escalation_edges(data)
    assert all(e["source"] != e["target"] for e in edges)
    to_ga = [e for e in edges if e["source"] == br.sp_id("sp1") and e["target"] == br.role_id(GA_ID)]
    assert len(to_ga) == 1


def test_group_write_permission_reaches_the_roles_a_group_confers():
    data = _data(
        sps=[_sp("sp1", "Provisioner", ["Group.ReadWrite.All"])],
        groups=[{"id": "g1", "display_name": "Tenant Admins", "is_assignable_to_role": True,
                 "owner_ids": [], "owners_known": True}],
        assignments=[_assignment("g1", GA_ID, "Tenant Admins", ptype="Group")],
    )
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "group_write"]
    assert len(edges) == 1
    assert edges[0]["target"] == br.role_id(GA_ID)


def test_group_write_with_no_role_assignable_group_escalates_nowhere():
    data = _data(
        sps=[_sp("sp1", "Provisioner", ["Group.ReadWrite.All"])],
        groups=[{"id": "g1", "display_name": "Engineering", "is_assignable_to_role": False,
                 "owner_ids": [], "owners_known": True}],
        assignments=[_assignment("g1", GA_ID, "Engineering", ptype="Group")],
    )
    assert [e for e in br.escalation_edges(data)
            if e["data"]["primitive"] == "group_write"] == []


def test_password_write_permission_is_reported_at_medium_confidence():
    data = _data(sps=[_sp("sp1", "Helpdesk Bot", ["User-PasswordProfile.ReadWrite.All"])])
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "password_write"]
    assert len(edges) == 1
    assert edges[0]["data"]["confidence"] == "medium", (
        "role restrictions may prevent this, so it must not claim high confidence")


def test_fan_out_is_bounded_but_the_true_total_is_kept():
    """Found on a real 20,000-user tenant: one service principal produced 224 arrows.

    The 225th arrow adds no information and costs the legibility the view exists for, so
    the extras become a count on the edges that were drawn."""
    data = _data(sps=[
        _sp("attacker", "Provisioner", ["Application.ReadWrite.All"]),
        *[_sp(f"victim{i}", f"App {i}", ["RoleManagement.ReadWrite.Directory"])
          for i in range(40)],
    ])
    edges = [e for e in br.escalation_edges(data)
             if e["source"] == br.sp_id("attacker")
             and e["data"]["primitive"] == "application_write"]
    assert len(edges) == br.MAX_FAN_OUT
    assert edges[0]["data"]["fan_out_total"] == 40, "the real number must survive the cap"


def test_a_small_fan_out_is_drawn_in_full():
    data = _data(sps=[
        _sp("attacker", "Provisioner", ["Application.ReadWrite.All"]),
        _sp("victim1", "App 1", ["RoleManagement.ReadWrite.Directory"]),
        _sp("victim2", "App 2", ["RoleManagement.ReadWrite.Directory"]),
    ])
    edges = [e for e in br.escalation_edges(data)
             if e["source"] == br.sp_id("attacker")
             and e["data"]["primitive"] == "application_write"]
    assert len(edges) == 2


def test_the_stronger_primitive_wins_when_two_reach_the_same_target():
    """Found by live testing: a medium-confidence password-reset path was masking a
    high-confidence application-takeover path to the same role."""
    data = _data(sps=[_sp("sp1", "Bot", ["User-PasswordProfile.ReadWrite.All",
                                         "Application.ReadWrite.All"])])
    edges = [e for e in br.escalation_edges(data)
             if e["source"] == br.sp_id("sp1") and e["target"] == br.role_id(GA_ID)]
    assert len(edges) == 1
    assert edges[0]["data"]["confidence"] == "high"
    assert edges[0]["data"]["primitive"] == "application_write"
    assert "password_write" in edges[0]["data"]["also_via"], (
        "the weaker path must stay visible in the evidence, not be discarded")


def test_a_weaker_primitive_never_overwrites_a_stronger_one():
    data = _data(sps=[_sp("sp1", "Bot", ["RoleManagement.ReadWrite.Directory",
                                         "User-PasswordProfile.ReadWrite.All"])])
    edges = [e for e in br.escalation_edges(data)
             if e["source"] == br.sp_id("sp1") and e["target"] == br.role_id(GA_ID)]
    assert len(edges) == 1
    assert edges[0]["data"]["confidence"] == "high"


def test_privileged_authentication_administrator_reaches_global_admin():
    definitions = _definitions() + [
        {"id": "rd-pauth", "display_name": "Privileged Authentication Administrator",
         "tier": "tier0", "privileged": True}]
    data = _data(users=[{"id": "u3", "upn": "pauth@x", "display_name": "P Auth", "enabled": True}])
    data["roles"]["definitions"] = definitions
    data["roles"]["assignments"] = [{
        "principal_id": "u3", "principal_type": "User", "principal_name": "P Auth",
        "role_id": "rd-pauth", "role_name": "Privileged Authentication Administrator",
        "role_privileged": True}]
    edges = [e for e in br.escalation_edges(data) if e["data"]["primitive"] == "priv_auth_admin"]
    assert len(edges) == 1
    assert edges[0]["target"] == br.role_id(GA_ID)


def test_every_escalation_edge_states_its_rule_and_confidence():
    data = _data(
        users=[{"id": "u1", "upn": "owner@x", "display_name": "Owner", "enabled": True}],
        sps=[_sp("sp1", "CI Deploy", ["RoleManagement.ReadWrite.Directory"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "owner_ids": ["u1"]}],
    )
    for edge in br.escalation_edges(data):
        assert edge["data"]["rule"], "an edge nobody can justify should not exist"
        assert edge["data"]["confidence"] in ("high", "medium", "low")
        assert edge["data"]["primitive"] in br.PRIMITIVE_BY_KEY


def test_escalation_edges_are_deduplicated():
    """Two paths to the same conclusion is one edge, not two overlapping arrows."""
    data = _data(
        users=[{"id": "u1", "upn": "owner@x", "display_name": "Owner", "enabled": True}],
        sps=[_sp("sp1", "A", ["RoleManagement.ReadWrite.Directory"]),
             _sp("sp2", "B", ["RoleManagement.ReadWrite.Directory"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "owner_ids": ["u1"]},
                      {"object_id": "app2", "sp_object_id": "sp2", "owner_ids": ["u1"]}],
    )
    owner_edges = [e for e in br.escalation_edges(data) if e["source"] == br.user_id("u1")]
    assert len(owner_edges) == 1
    assert len({e["id"] for e in br.escalation_edges(data)}) == len(br.escalation_edges(data))


def test_no_escalation_edges_without_a_global_administrator_role_definition():
    """The primitives target a real role; inventing one would be worse than staying silent."""
    data = _data(sps=[_sp("sp1", "Grantor", ["AppRoleAssignment.ReadWrite.All"])])
    data["roles"]["definitions"] = [d for d in _definitions() if d["id"] != GA_ID]
    assert [e for e in br.escalation_edges(data)
            if e["data"]["primitive"] == "consent_grant"] == []


# ======================================================================= scopes
def test_privileged_overview_excludes_the_service_principal_takeover_mesh():
    """Found in the browser: on a real tenant this view drew 547 edges over 119 nodes and
    was an unreadable hairball, because every SP-can-seize-SP edge landed in it. This view
    answers 'who can end up privileged'; the seizure mesh is the escalation map's job."""
    data = _data(
        sps=[_sp("sp1", "Provisioner", ["Application.ReadWrite.All"]),
             *[_sp(f"v{i}", f"App {i}", ["RoleManagement.ReadWrite.Directory"])
               for i in range(10)]],
    )
    result = br.privileged_overview(data)
    sp_to_sp = [e for e in result["edges"]
                if e["kind"] == br.EDGE_ESCALATES_TO and e["target"].startswith("esp:")]
    assert sp_to_sp == []
    # But the paths that DO reach a role are still drawn.
    to_role = [e for e in result["edges"]
               if e["kind"] == br.EDGE_ESCALATES_TO and e["target"].startswith("er:")]
    assert to_role, "paths into a privileged role must survive"


def test_the_escalation_map_still_shows_the_full_mesh():
    data = _data(
        sps=[_sp("sp1", "Provisioner", ["Application.ReadWrite.All"]),
             *[_sp(f"v{i}", f"App {i}", ["RoleManagement.ReadWrite.Directory"])
               for i in range(10)]],
    )
    result = br.escalation_map(data)
    sp_to_sp = [e for e in result["edges"] if e["target"].startswith("esp:")]
    assert sp_to_sp, "the dedicated map is where the takeover chains belong"


def test_privileged_overview_excludes_non_privileged_roles():
    data = _data(
        users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True},
               {"id": "u2", "upn": "b@x", "display_name": "Bob", "enabled": True}],
        assignments=[_assignment("u1", GA_ID, "Alice"),
                     _assignment("u2", "rd-reader", "Bob", privileged=False)],
    )
    result = br.privileged_overview(data)
    labels = {n["label"] for n in result["nodes"]}
    assert "Alice" in labels
    assert "Bob" not in labels, "a tier-2 role holder is not part of the privileged overview"


def test_privileged_overview_shows_the_nested_group_chain():
    data = _data(
        users=[{"id": "u2", "upn": "b@x", "display_name": "Bob", "enabled": True}],
        groups=[{"id": "g1", "display_name": "Tenant Admins", "is_assignable_to_role": True,
                 "owner_ids": [], "owners_known": True}],
        derived=[_assignment("u2", GA_ID, "", source_group_id="g1",
                             source_group_name="Tenant Admins")],
    )
    result = br.privileged_overview(data)
    kinds = {e["kind"] for e in result["edges"]}
    assert br.EDGE_MEMBER_OF in kinds, "the group that confers the role must be on the path"
    _assert_no_dangling(result)


def test_eligible_assignments_are_drawn_differently_from_active_ones():
    data = _data(
        users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True}],
        eligible=[_assignment("u1", GA_ID, "Alice", permanent=False)],
    )
    result = br.privileged_overview(data)
    assert {e["kind"] for e in result["edges"]} == {br.EDGE_ELIGIBLE_FOR}


def test_focus_application_lists_permissions_and_owners():
    data = _data(
        users=[{"id": "u1", "upn": "owner@x", "display_name": "Owner", "enabled": True}],
        sps=[_sp("sp1", "CI Deploy", ["Mail.ReadWrite", "Directory.ReadWrite.All"])],
        applications=[{"object_id": "app1", "sp_object_id": "sp1", "owner_ids": ["u1"]}],
    )
    result = br.focus_application(data, "sp1")
    kinds = {n["kind"] for n in result["nodes"]}
    assert br.KIND_PERMISSION in kinds
    assert br.EDGE_OWNS in {e["kind"] for e in result["edges"]}
    _assert_no_dangling(result)


def test_focus_application_on_an_unknown_id_is_empty_and_says_so():
    result = br.focus_application(_data(), "nope")
    assert result["nodes"] == []
    assert "No such application" in result["note"]


def test_focus_role_finds_holders_through_every_path():
    data = _data(
        users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True},
               {"id": "u2", "upn": "b@x", "display_name": "Bob", "enabled": True}],
        assignments=[_assignment("u1", GA_ID, "Alice")],
        eligible=[_assignment("u2", GA_ID, "Bob")],
    )
    result = br.focus_role(data, GA_ID)
    kinds = {e["kind"] for e in result["edges"]}
    assert kinds == {br.EDGE_ACTIVE_IN, br.EDGE_ELIGIBLE_FOR}


def test_focus_role_accepts_a_display_name():
    data = _data(users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True}],
                 assignments=[_assignment("u1", GA_ID, "Alice")])
    assert br.focus_role(data, "Global Administrator")["nodes"]


def test_focus_policy_separates_covered_from_excluded():
    data = _data(users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True},
                        {"id": "u2", "upn": "b@x", "display_name": "Bob", "enabled": True}])
    analysis = {"policies": [{"id": "p1", "display_name": "Require MFA", "state": "enabled",
                              "is_enforced": True, "controls": ["mfa"],
                              "effective_ids": ["u1"], "excluded_ids": ["u2"]}]}
    result = br.focus_policy(data, analysis, "p1")
    kinds = {e["kind"] for e in result["edges"]}
    assert kinds == {br.EDGE_PROTECTED_BY, br.EDGE_EXCLUDED_FROM}
    _assert_no_dangling(result)


def test_managed_identities_get_their_own_node_kind():
    data = _data(sps=[{**_sp("mi1", "aks-identity"), "sp_type": "ManagedIdentity"}])
    result = br.focus_application(data, "mi1")
    assert result["nodes"][0]["kind"] == br.KIND_MANAGED_IDENTITY


def test_scopes_and_primitives_are_published_for_the_ui():
    assert {s["kind"] for s in br.SCOPES} >= {"privileged", "escalation", "principal", "role",
                                              "federation"}
    for primitive in br.ESCALATION_PRIMITIVES:
        assert primitive["name"] and primitive["rule"] and primitive["confidence"]


# ------------------------------------------------------------------------- federation
# "If that provider is compromised, whose privilege does the attacker inherit?" is a
# question no other scope answers, and a wrong answer here understates a tenant-wide risk.
def _federated(*, users=(), assignments=(), readable=True, trusts=None):
    data = _data(users=users, assignments=assignments)
    data["tenant"] = {"identity_fabric": {
        "readable": readable,
        "federation": list(trusts if trusts is not None else [{
            "domain": "contoso.com",
            "vendor": {"label": "PingFederate"},
            "issuer_uri": "http://contoso.com/PingFederate",
            "mfa_behaviour": {"value": "acceptIfMfaDoneByFederatedIdp", "trusted": True},
        }]),
    }}
    return data


def test_federation_draws_the_provider_and_the_privileged_it_can_impersonate():
    data = _federated(
        users=[{"id": "u1", "upn": "admin@contoso.com", "display_name": "Admin", "enabled": True},
               {"id": "u2", "upn": "helper@contoso.com", "display_name": "Helper", "enabled": True}],
        assignments=[_assignment("u1", GA_ID, "Admin")],
    )
    result = br.federation_map(data)
    kinds = [n["kind"] for n in result["nodes"]]
    assert kinds.count(br.KIND_FEDERATED_DOMAIN) == 1
    # The unprivileged user on the same domain is not drawn: thousands of identical nodes
    # would say one thing, and the tier-0 holder is the answer.
    assert [n["label"] for n in result["nodes"] if n["kind"] != br.KIND_FEDERATED_DOMAIN] == ["Admin"]
    assert {e["kind"] for e in result["edges"]} == {br.EDGE_AUTHENTICATES}
    assert result["edges"][0]["data"]["mfa_trusted"] is True
    _assert_no_dangling(result)


def test_a_privileged_user_on_a_managed_domain_is_not_reachable_from_the_provider():
    data = _federated(
        users=[{"id": "u1", "upn": "admin@contoso.onmicrosoft.com", "display_name": "Cloud",
                "enabled": True}],
        assignments=[_assignment("u1", GA_ID, "Cloud")],
    )
    result = br.federation_map(data)
    assert result["edges"] == []


def test_a_cloud_only_tenant_says_so_instead_of_drawing_nothing():
    result = br.federation_map(_federated(trusts=[]))
    assert result["nodes"] == []
    assert "No domain is federated" in result["note"]


def test_an_unreadable_domain_list_is_unknown_not_clean():
    result = br.federation_map(_federated(readable=False))
    assert result["nodes"] == []
    assert "could not be read" in result["note"]


def test_build_routes_the_federation_scope():
    data = _federated(
        users=[{"id": "u1", "upn": "admin@contoso.com", "display_name": "Admin", "enabled": True}],
        assignments=[_assignment("u1", GA_ID, "Admin")],
    )
    assert br.build(data, {}, scope_kind="federation") == br.federation_map(data)


def test_build_falls_back_to_the_privileged_overview():
    data = _data(users=[{"id": "u1", "upn": "a@x", "display_name": "Alice", "enabled": True}],
                 assignments=[_assignment("u1", GA_ID, "Alice")])
    fallback = br.build(data, {}, scope_kind="does-not-exist")
    assert fallback["nodes"] == br.privileged_overview(data)["nodes"]
