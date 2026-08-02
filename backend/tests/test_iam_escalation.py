"""Escalation graph, managed identities and federated credentials (P5).

The graph rules tested here are inherited from the Entra version, and every one of them was a
production defect first:

* one edge pointing at a missing node makes Cytoscape reject the WHOLE batch and blank the canvas
* one service principal produced 224 arrows, which is not a diagram
* a medium-confidence edge masked a high-confidence one and the operator read the weaker reason
* an escalation map that cannot see managed identities and says nothing reads as "no paths exist"

And the detection rules, each of which is a way to be confidently wrong:

* reporting an Owner as "can become an Owner" buries every real finding
* a wildcard federated-credential subject is a total compromise detectable by a string check
"""
from __future__ import annotations

import pytest

from app.iam import cache, demo, effective, escalation, schema

pytestmark = pytest.mark.anyio


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


SUB = "/subscriptions/11111111-1111-1111-1111-111111111111"
RG = f"{SUB}/resourceGroups/prod"
VM = f"{RG}/providers/Microsoft.Compute/virtualMachines/vm1"
UAMI = f"{RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami1"

# Role definitions carrying exactly the action each primitive keys on, so a test failure means
# the PRIMITIVE is wrong rather than the fixture.
ROLE_DEFS = [
    {"roleDefinitionId": "/rd/owner", "roleName": "Owner", "actions": ["*"]},
    {"roleDefinitionId": "/rd/reader", "roleName": "Reader", "actions": ["*/read"]},
    {"roleDefinitionId": "/rd/vmop", "roleName": "VM Operator",
     "actions": ["Microsoft.Compute/virtualMachines/runCommand/action"]},
    {"roleDefinitionId": "/rd/kvc", "roleName": "Key Vault Contributor",
     "actions": ["Microsoft.KeyVault/vaults/write"]},
    {"roleDefinitionId": "/rd/storage", "roleName": "Storage Lister",
     "actions": ["Microsoft.Storage/storageAccounts/listKeys/action"]},
    {"roleDefinitionId": "/rd/uaa", "roleName": "User Access Administrator",
     "actions": ["Microsoft.Authorization/roleAssignments/write"]},
    {"roleDefinitionId": "/rd/miwrite", "roleName": "Managed Identity Contributor",
     "actions": ["Microsoft.ManagedIdentity/userAssignedIdentities/write"]},
    {"roleDefinitionId": "/rd/lock", "roleName": "Lock Remover",
     "actions": ["Microsoft.Authorization/locks/delete"]},
    {"roleDefinitionId": "/rd/aks", "roleName": "AKS Admin Getter",
     "actions": ["Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action"]},
    {"roleDefinitionId": "/rd/lighthouse", "roleName": "Lighthouse Onboarder",
     "actions": ["Microsoft.ManagedServices/registrationAssignments/write"]},
    {"roleDefinitionId": "/rd/deny", "roleName": "Deny Editor",
     "actions": ["Microsoft.Authorization/denyAssignments/write"]},
]
IDX = effective.build_role_index(ROLE_DEFS)


def _row(principal: str, role_key: str, role_name: str, scope: str = SUB, **kw):
    base = {
        "surface": schema.SURFACE_AZURE_RBAC,
        "effect": schema.EFFECT_ALLOW,
        "assignmentState": schema.STATE_ACTIVE,
        "accessPath": schema.PATH_DIRECT,
        "principalId": principal,
        "effectivePrincipalId": principal,
        "effectivePrincipalName": principal,
        "roleDefinitionId": f"/rd/{role_key}",
        "roleName": role_name,
        "scope": scope,
        "scopeDisplayName": scope,
        "assignmentId": f"a-{principal}-{role_key}",
    }
    base.update(kw)
    return schema.make_row(**base)


VM_IDENTITY = {
    "mi-vm": {
        "principalId": "mi-vm",
        "identityKind": "SystemAssigned",
        "identityResourceId": VM,
        "identityName": "vm1",
        "attachedResourceIds": [VM],
        "attachedResourceCount": 1,
    }
}

UAMI_IDENTITY = {
    "mi-uami": {
        "principalId": "mi-uami",
        "identityKind": "UserAssigned",
        "identityResourceId": UAMI,
        "identityName": "uami1",
        "attachedResourceIds": [f"{RG}/providers/Microsoft.Web/sites/app1"],
        "attachedResourceCount": 1,
    }
}


def _detect(rows, **kw):
    return escalation.detect(rows, IDX, **kw)


def _primitives_in(graph) -> set[str]:
    return {e["data"]["primitive"] for e in graph["edges"]}


# --------------------------------------------------------------------------- primitives
@pytest.mark.parametrize(
    ("role_key", "role_name", "primitive"),
    [
        ("uaa", "User Access Administrator", "role_write"),
        ("deny", "Deny Editor", "deny_write"),
        ("kvc", "Key Vault Contributor", "keyvault_pivot"),
        ("storage", "Storage Lister", "storage_key"),
        ("aks", "AKS Admin Getter", "aks_admin"),
        ("lighthouse", "Lighthouse Onboarder", "lighthouse"),
        ("lock", "Lock Remover", "lock_delete"),
    ],
)
def test_each_primitive_is_detected_from_its_action(role_key, role_name, primitive):
    g = _detect([_row("alice", role_key, role_name)])
    assert primitive in _primitives_in(g)


@pytest.mark.parametrize(
    "primitive",
    ["role_write", "deny_write", "keyvault_pivot", "storage_key", "aks_admin", "lighthouse", "lock_delete"],
)
def test_no_primitive_fires_for_a_reader(primitive):
    """The negative half. Reader holds `*/read` and nothing else; any primitive that fires here
    is matching on something other than the action it claims to."""
    g = _detect([_row("carol", "reader", "Reader")])
    assert primitive not in _primitives_in(g)


def test_a_primitive_is_detected_from_a_CUSTOM_role_not_a_role_name():
    """The whole reason the effective-permission engine had to exist first: a custom role that
    grants roleAssignments/write is exactly as dangerous as Owner, and name-matching misses it."""
    custom = [{"roleDefinitionId": "/rd/sneaky", "roleName": "Helpdesk Support",
               "actions": ["Microsoft.Authorization/roleAssignments/write"]}]
    idx = effective.build_role_index(custom)
    g = escalation.detect([_row("mallory", "sneaky", "Helpdesk Support")], idx)
    assert "role_write" in _primitives_in(g)


def test_identity_hijack_needs_the_identity_inventory():
    """Without it there is nothing to point at — and the graph must SAY so rather than imply
    that no such path exists."""
    rows = [_row("frank", "vmop", "VM Operator", scope=RG)]
    without = _detect(rows)
    assert "identity_hijack_vm" not in _primitives_in(without)
    assert any("identity-hijack" in lim or "Managed identities" in lim for lim in without["limitations"])

    with_ids = _detect(rows, identities=VM_IDENTITY)
    assert "identity_hijack_vm" in _primitives_in(with_ids)


def test_identity_hijack_chains_to_owner_when_the_identity_is_privileged():
    """The half that matters. "You can become the VM" is not a finding; "…and the VM can assign
    roles" is. Without the second hop the graph stops one step short of the point."""
    rows = [
        _row("frank", "vmop", "VM Operator", scope=RG),
        _row("mi-vm", "uaa", "User Access Administrator", scope=SUB),
    ]
    g = _detect(rows, identities=VM_IDENTITY)
    paths = [p for p in g["paths"] if p["from"] == escalation.principal_node("frank")]
    assert paths, "frank must have a path to full control"
    assert paths[0]["length"] == 2
    assert [h["primitive"] for h in paths[0]["hops"]] == ["identity_hijack_vm", "role_write"]


def test_a_principal_who_is_already_tier0_is_flagged_as_such():
    """Reporting an Owner as "can become an Owner" buries every real finding under the tenant's
    entire administrator list."""
    g = _detect([_row("alice", "owner", "Owner")])
    node = next(n for n in g["nodes"] if n["id"] == escalation.principal_node("alice"))
    assert node["alreadyTier0"] is True

    g2 = _detect([_row("frank", "vmop", "VM Operator", scope=RG)], identities=VM_IDENTITY)
    node2 = next(n for n in g2["nodes"] if n["id"] == escalation.principal_node("frank"))
    assert node2["alreadyTier0"] is False


# --------------------------------------------------------------------------- graph invariants
@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row("alice", "owner", "Owner")],
        [_row("frank", "vmop", "VM Operator", scope=RG), _row("carol", "reader", "Reader")],
    ],
)
@pytest.mark.parametrize("scope_filter", ["", SUB, RG, "/subscriptions/other"])
def test_no_edge_ever_points_at_a_missing_node(rows, scope_filter):
    """THE property test. Cytoscape rejects the entire batch when one edge points at a node that
    is not in the payload, which blanks the canvas — so a single stray edge costs the whole view.
    Parameterised over empty and populated data and every scope value, exactly as the plan says."""
    g = _detect(rows, identities=VM_IDENTITY, scope_filter=scope_filter)
    present = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source"] in present, f"dangling source {e['source']}"
        assert e["target"] in present, f"dangling target {e['target']}"
        assert e["source"] != e["target"], "self-loop"


def test_dangling_edges_are_counted_not_silently_dropped():
    g = escalation._finish(
        [{"id": "a", "kind": "principal", "label": "a"}],
        [
            {"id": "e1", "source": "a", "target": "ghost", "kind": "escalates_to", "data": {}},
            {"id": "e2", "source": "a", "target": "a", "kind": "escalates_to", "data": {}},
        ],
        limitations=[], fan_out_total={},
    )
    assert g["edges"] == []
    assert g["dropped_edges"] == 2


def test_fan_out_is_capped_with_the_true_total_reported():
    """One service principal produced 224 arrows in the Entra version. The 225th adds no
    information and costs the legibility that is the entire point of the view."""
    idents = {
        f"mi-{i}": {
            "principalId": f"mi-{i}", "identityKind": "SystemAssigned",
            "identityResourceId": f"{RG}/providers/Microsoft.Compute/virtualMachines/vm{i}",
            "identityName": f"vm{i}",
            "attachedResourceIds": [f"{RG}/providers/Microsoft.Compute/virtualMachines/vm{i}"],
        }
        for i in range(40)
    }
    g = _detect([_row("frank", "vmop", "VM Operator", scope=RG)], identities=idents)
    hijacks = [e for e in g["edges"] if e["data"]["primitive"] == "identity_hijack_vm"]
    assert len(hijacks) == escalation.MAX_FAN_OUT
    key = f"{escalation.principal_node('frank')}|identity_hijack_vm"
    assert g["fan_out_total"][key] > escalation.MAX_FAN_OUT


def test_the_higher_confidence_edge_wins_and_the_loser_is_kept(monkeypatch):
    """A medium-confidence edge once masked a high-confidence one to the same target and the
    operator read the weaker explanation. The loser goes in `also_via`, not the bin.

    The primitive order is REVERSED here on purpose. `PRIMITIVES` currently happens to list every
    high-confidence primitive before the medium ones, so the weak-arrives-first branch is
    unreachable in practice — which means shipping it broken would go unnoticed until somebody
    reordered the registry. The rule must hold regardless of order, so the test forces the order
    that exercises it."""
    reordered = sorted(escalation.PRIMITIVES, key=lambda p: escalation._CONFIDENCE_RANK[p["confidence"]])
    monkeypatch.setattr(escalation, "PRIMITIVES", reordered)

    rows = [
        _row("alice", "lock", "Lock Remover"),        # medium, targets the scope
        _row("alice", "kvc", "Key Vault Contributor"),  # high, same scope
    ]
    g = _detect(rows)
    to_scope = [e for e in g["edges"] if e["target"] == escalation.scope_node(SUB)]
    assert len(to_scope) == 1, "one edge per (source, target)"
    assert to_scope[0]["data"]["confidence"] == "high", "the stronger explanation must win"
    assert "lock_delete" in (to_scope[0]["data"].get("also_via") or []), "the loser is kept, not dropped"


def test_the_stronger_edge_still_wins_when_it_arrives_first():
    """The other direction, in the registry's real order."""
    rows = [
        _row("alice", "kvc", "Key Vault Contributor"),
        _row("alice", "lock", "Lock Remover"),
    ]
    g = _detect(rows)
    to_scope = [e for e in g["edges"] if e["target"] == escalation.scope_node(SUB)]
    assert len(to_scope) == 1
    assert to_scope[0]["data"]["confidence"] == "high"
    assert "lock_delete" in (to_scope[0]["data"].get("also_via") or [])


def test_limitations_are_always_published():
    """An escalation map that cannot see policy identities must say so. Silence reads as
    "there are none", which is the opposite of the truth."""
    assert _detect([])["limitations"]
    assert _detect([_row("alice", "owner", "Owner")], identities=VM_IDENTITY, federated=[{"x": 1}])["limitations"]


def test_no_federated_credentials_is_a_limitation_only_when_there_could_have_been_some():
    """Federated credentials exist only on USER-ASSIGNED identities. If the inventory ran and
    found none of those, "no federated credentials" is a complete answer rather than a blind
    spot — and calling it a limitation trains the reader to ignore the list, which is the one
    thing that must not happen to it."""
    only_system = _detect([], identities=VM_IDENTITY, federated=[])
    assert not any("Federated" in lim for lim in only_system["limitations"])

    has_uami = _detect([], identities=UAMI_IDENTITY, federated=[])
    assert any("Federated" in lim for lim in has_uami["limitations"])

    # And when identities were never collected at all, that limitation dominates.
    nothing = _detect([], identities={}, federated=[])
    assert any("Managed identities" in lim for lim in nothing["limitations"])


def test_min_confidence_filters_primitives():
    g_all = _detect([_row("alice", "lock", "Lock Remover")])
    g_high = _detect([_row("alice", "lock", "Lock Remover")], min_confidence=escalation.CONF_HIGH)
    assert "lock_delete" in _primitives_in(g_all)
    assert "lock_delete" not in _primitives_in(g_high)


def test_filtering_to_one_principal_returns_only_their_paths():
    rows = [_row("alice", "uaa", "User Access Administrator"), _row("bob", "uaa", "User Access Administrator")]
    g = _detect(rows, principal_id="alice")
    assert {n["id"] for n in g["nodes"] if n["kind"] == "principal"} == {escalation.principal_node("alice")}


# --------------------------------------------------------------------------- paths
def test_shortest_path_is_reported_not_just_that_one_exists():
    """A one-hop path from an ordinary user is a very different finding from a three-hop one,
    and reporting only "a path exists" loses the distinction that decides whether anyone acts."""
    rows = [
        _row("frank", "vmop", "VM Operator", scope=RG),
        _row("mi-vm", "uaa", "User Access Administrator", scope=SUB),
        _row("direct", "uaa", "User Access Administrator", scope=SUB),
    ]
    g = _detect(rows, identities=VM_IDENTITY)
    by_from = {p["from"]: p for p in g["paths"]}
    assert by_from[escalation.principal_node("direct")]["length"] == 1
    assert by_from[escalation.principal_node("frank")]["length"] == 2


def test_paths_carry_the_weakest_confidence_in_the_chain():
    """A chain is only as trustworthy as its least trustworthy hop; reporting the strongest
    would overstate the whole path."""
    rows = [
        _row("frank", "vmop", "VM Operator", scope=RG),
        _row("mi-vm", "uaa", "User Access Administrator", scope=SUB),
    ]
    g = _detect(rows, identities=VM_IDENTITY)
    p = next(p for p in g["paths"] if p["from"] == escalation.principal_node("frank"))
    assert p["min_confidence"] == "high"  # both hops are high


def test_no_paths_when_nothing_reaches_tier0():
    g = _detect([_row("carol", "reader", "Reader")])
    assert g["paths"] == []


# --------------------------------------------------------------------------- federated creds
@pytest.mark.parametrize(
    ("subject", "loose"),
    [
        ("repo:contoso/*:ref:refs/heads/main", True),
        ("repo:contoso/platform:*", True),
        ("repo:contoso/platform:pull_request", True),
        ("", True),
        ("repo:contoso/platform:ref:refs/heads/main", False),
        ("repo:contoso/platform:environment:prod", False),
        ("system:serviceaccount:ns:sa", False),
    ],
)
def test_loose_federated_subjects_are_detected(subject, loose):
    """Detection is a string check; the impact is total. A wildcard or pull-request subject means
    any fork, or any contributor who can open a PR, can assume the identity — no secret, no
    expiry, no unusual sign-in."""
    assert bool(escalation.loose_subject_reason(subject)) is loose


@pytest.mark.parametrize(
    ("issuer", "unknown"),
    [
        ("https://token.actions.githubusercontent.com", False),
        ("https://vstoken.dev.azure.com/abc", False),
        ("https://evil.example.com", True),
        ("", True),
    ],
)
def test_unknown_federated_issuers_are_flagged(issuer, unknown):
    assert escalation.unknown_issuer(issuer) is unknown


# --------------------------------------------------------------------------- demo estate
def test_the_demo_estate_exercises_the_graph_with_no_connection(isolated_cache):
    """The plan's acceptance criterion. If the shipped dataset cannot demonstrate the flagship
    feature, nobody evaluating the product ever sees it work."""
    from app.iam import compose

    demo.seed_demo("t1")
    rows = compose.build_master_rows("t1")
    directory = cache.read_directory("t1")
    idx = effective.build_role_index(directory["role_defs"])
    g = escalation.detect(
        rows, idx,
        identities=directory["identities"],
        federated=directory["federated"],
    )
    assert g["dropped_edges"] == 0
    assert g["paths"], "the demo estate must contain at least one escalation path"
    # …and specifically the identity-hijack path, which is the one that is invisible in every
    # Azure-native view and the reason this feature exists.
    assert "identity_hijack_vm" in _primitives_in(g)


def test_the_demo_estate_has_a_two_hop_path_from_a_non_privileged_principal(isolated_cache):
    from app.iam import compose

    demo.seed_demo("t1")
    directory = cache.read_directory("t1")
    g = escalation.detect(
        compose.build_master_rows("t1"),
        effective.build_role_index(directory["role_defs"]),
        identities=directory["identities"],
        federated=directory["federated"],
    )
    non_owner_paths = [
        p for p in g["paths"]
        if not next(n for n in g["nodes"] if n["id"] == p["from"]).get("alreadyTier0")
    ]
    assert non_owner_paths, "someone who is NOT already privileged must have a path"


def test_the_demo_estate_carries_a_loose_federated_credential(isolated_cache):
    demo.seed_demo("t1")
    fics = cache.read_directory("t1")["federated"]
    assert any(escalation.loose_subject_reason(str(f.get("subject", ""))) for f in fics)
    # …and a well-formed one alongside it, so the signal is discriminating rather than
    # flagging everything it sees.
    assert any(not escalation.loose_subject_reason(str(f.get("subject", ""))) for f in fics)


def test_the_demo_estate_has_a_shared_user_assigned_identity(isolated_cache):
    demo.seed_demo("t1")
    ids = cache.read_directory("t1")["identities"]
    shared = [
        i for i in ids.values()
        if i.get("identityKind") == "UserAssigned" and len(i.get("attachedResourceIds") or []) > 1
    ]
    assert shared, "a compromise-in-dev-reaches-prod fixture must exist"


# --------------------------------------------------------------------------- registry loading
def test_every_signal_defs_module_is_loaded():
    """The loader used to hardcode its import list, so adding a pillar file did NOTHING — the
    module was never imported, the pillar kept reporting `not_implemented`, and no error
    anywhere explained why. Discovery makes forgetting impossible.

    Written first as "module name == pillar prefix", which held only by coincidence: `structure`
    supplies the `str` pillar, and `drift` supplies `gov` signals. The invariant that actually
    matters is that every module on disk declares signals and every one of them reaches the
    registry — which does not care what the file is called."""
    import importlib
    import pkgutil

    from app.iam import signal_defs, signals

    registered = {s.id for s in signals.all_signals()}
    on_disk = [m.name for m in pkgutil.iter_modules(signal_defs.__path__) if not m.name.startswith("_")]
    assert on_disk, "no signal modules were discovered at all"

    for name in on_disk:
        module = importlib.import_module(f"app.iam.signal_defs.{name}")
        declared = getattr(module, "SIGNALS", None)
        assert declared, f"signal_defs/{name}.py declares no SIGNALS and would be silently inert"
        for spec in declared:
            assert spec.id in registered, f"{spec.id} is declared in {name}.py but never reached the registry"

    loaded_pillars = {s.id.split(".", 1)[0] for s in signals.all_signals()}
    assert "esc" in loaded_pillars, "the escalation pillar must be registered"


# --------------------------------------------------------------------------- graph memo
def test_the_tenant_graph_memo_is_keyed_on_the_cache_version_not_on_a_proxy():
    """`/iam/escalation`, `/iam/findings` and `/iam/score` all want the same unfiltered graph,
    which costs ~30s to build on a real tenant. Memoising it is what makes those three screens
    usable — but the key has to identify the SNAPSHOT.

    An earlier attempt keyed on (cache version, row count, identity count). Row count is a proxy
    for the rows, not the rows, so two different snapshots of the same size returned each
    other's graph. Keyed on (tenant, cache fingerprint) the rows ARE the cache at that version,
    and any write bumps it. The fingerprint rather than the bare version because the version is
    a process-global counter starting at zero, so two different cache STORES both read as
    version 0 and would collide."""
    from app.iam import cache as iam_cache

    escalation._GRAPH_CACHE.clear()
    version = [7]
    monkey_fp = iam_cache.cache_fingerprint
    monkey_ver = iam_cache.cache_version
    try:
        # A real write moves both: the memo keys on the fingerprint (it is process-global and
        # spans stores) and the persisted copy on the version (it lives inside one store, so the
        # store identity is already implicit in its location). Stubbing only one would leave the
        # other serving a stale answer.
        iam_cache.cache_fingerprint = lambda: ("store", version[0])  # type: ignore[assignment]
        iam_cache.cache_version = lambda: version[0]  # type: ignore[assignment]

        first = escalation.graph_for_tenant("t1", [_row("alice", "uaa", "User Access Administrator")], IDX)
        assert "role_write" in _primitives_in(first)

        # Same tenant + same version -> the memo answers, even for different rows.
        assert escalation.graph_for_tenant("t1", [], IDX) is first

        # A different tenant must never read another tenant's graph.
        other = escalation.graph_for_tenant("t2", [], IDX)
        assert other is not first and other["edges"] == []

        # A cache write bumps the version, which must rebuild.
        version[0] = 8
        rebuilt = escalation.graph_for_tenant("t2", [_row("bob", "uaa", "User Access Administrator")], IDX)
        assert rebuilt is not other
        assert "role_write" in _primitives_in(rebuilt)
    finally:
        iam_cache.cache_fingerprint = monkey_fp  # type: ignore[assignment]
        iam_cache.cache_version = monkey_ver  # type: ignore[assignment]
        escalation._GRAPH_CACHE.clear()


def test_a_filtered_escalation_query_is_never_served_from_the_memo():
    """A per-principal or per-scope graph is a different question with a cheap answer; serving
    it from the tenant-wide memo would answer the wrong one."""
    escalation._GRAPH_CACHE.clear()
    rows = [_row("alice", "uaa", "User Access Administrator"), _row("bob", "uaa", "User Access Administrator")]
    everyone = escalation.detect(rows, IDX)
    just_alice = escalation.detect(rows, IDX, principal_id="alice")
    assert len(just_alice["nodes"]) < len(everyone["nodes"])
    assert escalation._GRAPH_CACHE == {}
