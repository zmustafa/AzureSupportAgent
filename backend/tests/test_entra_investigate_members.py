"""Group membership: the cached flat answer, and the live nested tree.

The properties worth protecting are the ones that make the difference between a tree and a
lie: a cycle must terminate, an unreadable branch must not read as an empty one, and the
caps must actually cap.
"""
from __future__ import annotations

import pytest

from app.entra import investigate_members as im


# --------------------------------------------------------------------------- kind mapping
def test_odata_types_map_onto_our_kind_vocabulary():
    assert im._odata_kind("#microsoft.graph.user") == im.TYPE_USER
    assert im._odata_kind("#microsoft.graph.group") == im.TYPE_GROUP
    assert im._odata_kind("#microsoft.graph.servicePrincipal") == im.TYPE_SP


def test_an_unknown_member_type_is_kept_not_discarded():
    """A device in a security group is unusual enough to be worth seeing."""
    assert im._odata_kind("#microsoft.graph.device") == "device"
    assert im._odata_kind("") == "unknown"


def test_only_a_group_is_expandable():
    assert im._node({"id": "g", "@odata.type": "#microsoft.graph.group"})["expandable"] is True
    assert im._node({"id": "u", "@odata.type": "#microsoft.graph.user"})["expandable"] is False


# --------------------------------------------------------------------------- cached (P1)
def test_a_group_that_was_never_expanded_is_unknown_not_empty(monkeypatch):
    """The distinction the whole section rests on: we did not look != nobody is there."""
    from app.iam import cache

    monkeypatch.setattr(cache, "read_directory", lambda _t: {"groups": {}})
    members, known, reason = im.cached_members("t", "missing-group")
    assert members == []
    assert known is False
    assert "not been expanded" in reason


def test_an_expanded_but_empty_group_is_known(monkeypatch):
    from app.iam import cache

    monkeypatch.setattr(cache, "read_directory", lambda _t: {"groups": {"g1": {"members": []}}})
    members, known, _ = im.cached_members("t", "g1")
    assert members == []
    assert known is True


def test_cached_members_are_projected_and_sorted(monkeypatch):
    from app.iam import cache

    monkeypatch.setattr(cache, "read_directory", lambda _t: {"groups": {"g1": {"members": [
        {"principalId": "u2", "principalType": "User", "principalDisplayName": "Zoe",
         "principalUserPrincipalName": "zoe@x"},
        {"principalId": "u1", "principalType": "User", "principalDisplayName": "Adam",
         "principalUserPrincipalName": "adam@x"},
    ]}}})
    members, known, _ = im.cached_members("t", "g1")
    assert known is True
    assert [m["display_name"] for m in members] == ["Adam", "Zoe"]
    assert members[0] == {"id": "u1", "kind": "user", "display_name": "Adam", "upn": "adam@x"}


def test_an_unreadable_cache_is_unreadable_not_empty(monkeypatch):
    from app.iam import cache

    def _boom(_t):
        raise OSError("disk gone")

    monkeypatch.setattr(cache, "read_directory", _boom)
    members, known, reason = im.cached_members("t", "g1")
    assert (members, known) == ([], False)
    assert "could not be read" in reason


# --------------------------------------------------------------------------- live tree (P2)
class _FakeGraph:
    """Stands in for `_graph_get`, recording every path asked for."""

    def __init__(self, by_path: dict[str, list[dict]], errors: dict[str, str] | None = None):
        self.by_path = by_path
        self.errors = errors or {}
        self.calls: list[str] = []

    async def __call__(self, _conn, path, _params, cap):
        self.calls.append(path)
        if path in self.errors:
            return [], self.errors[path]
        return self.by_path.get(path, [])[:cap], ""


def _u(i):
    return {"id": i, "displayName": i.upper(), "@odata.type": "#microsoft.graph.user"}


def _g(i):
    return {"id": i, "displayName": i.upper(), "@odata.type": "#microsoft.graph.group"}


@pytest.mark.asyncio
async def test_fetch_children_reads_direct_members_not_transitive(monkeypatch):
    """Transitive membership has the intermediate groups removed, so it cannot build a tree."""
    fake = _FakeGraph({"/groups/root/members": [_u("u1"), _g("g1")]})
    monkeypatch.setattr(im, "_graph_get", fake)
    kids, trunc, err = await im.fetch_children({}, "root")
    assert err == "" and trunc is False
    assert fake.calls == ["/groups/root/members"]
    assert "transitiveMembers" not in fake.calls[0]
    assert {k["id"] for k in kids} == {"u1", "g1"}


@pytest.mark.asyncio
async def test_groups_sort_before_people(monkeypatch):
    """The branches are what you came to open."""
    monkeypatch.setattr(im, "_graph_get", _FakeGraph(
        {"/groups/root/members": [_u("aaa"), _g("zzz")]}))
    kids, _, _ = await im.fetch_children({}, "root")
    assert [k["kind"] for k in kids] == [im.TYPE_GROUP, im.TYPE_USER]


@pytest.mark.asyncio
async def test_upward_direction_asks_for_memberOf(monkeypatch):
    fake = _FakeGraph({"/groups/root/memberOf": [_g("parent")]})
    monkeypatch.setattr(im, "_graph_get", fake)
    kids, _, _ = await im.fetch_children({}, "root", direction="up")
    assert fake.calls == ["/groups/root/memberOf"]
    assert kids[0]["id"] == "parent"


@pytest.mark.asyncio
async def test_a_node_wider_than_the_cap_is_truncated_and_says_so(monkeypatch):
    wide = [_u(f"u{i}") for i in range(im.MAX_CHILDREN_PER_NODE + 50)]
    monkeypatch.setattr(im, "_graph_get", _FakeGraph({"/groups/root/members": wide}))
    kids, trunc, _ = await im.fetch_children({}, "root")
    assert trunc is True
    assert len(kids) == im.MAX_CHILDREN_PER_NODE


@pytest.mark.asyncio
async def test_expand_returns_one_level_per_branch_asked_for(monkeypatch):
    fake = _FakeGraph({
        "/groups/root/members": [_g("g1"), _u("u1")],
        "/groups/g1/members": [_u("u2")],
    })
    monkeypatch.setattr(im, "_graph_get", fake)
    out = await im.expand({}, "root", expand_ids=["g1"])
    assert set(out["nodes"]) == {"root", "g1"}
    assert [n["id"] for n in out["nodes"]["g1"]] == ["u2"]


@pytest.mark.asyncio
async def test_a_branch_is_never_walked_deeper_than_asked(monkeypatch):
    """Laziness is the cost model: one call per branch OPENED, never a full walk."""
    fake = _FakeGraph({
        "/groups/root/members": [_g("g1")],
        "/groups/g1/members": [_g("g2")],
        "/groups/g2/members": [_u("deep")],
    })
    monkeypatch.setattr(im, "_graph_get", fake)
    await im.expand({}, "root", expand_ids=[])
    assert fake.calls == ["/groups/root/members"]


@pytest.mark.asyncio
async def test_the_same_group_asked_for_twice_is_fetched_once(monkeypatch):
    """Within one request, dedup. The VISUAL cycle guard (a -> b -> a across two requests)
    lives in the tree component, which is the only side that knows the ancestor path."""
    fake = _FakeGraph({
        "/groups/a/members": [_g("b")],
        "/groups/b/members": [_g("a")],
    })
    monkeypatch.setattr(im, "_graph_get", fake)
    out = await im.expand({}, "a", expand_ids=["b", "a"])
    assert fake.calls.count("/groups/a/members") == 1
    assert set(out["nodes"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_an_unreadable_branch_is_reported_not_rendered_as_empty(monkeypatch):
    monkeypatch.setattr(im, "_graph_get", _FakeGraph(
        {"/groups/root/members": [_g("secret")]},
        errors={"/groups/secret/members": "denied by Graph — reading membership needs "
                                          "GroupMember.Read.All or Directory.Read.All."},
    ))
    out = await im.expand({}, "root", expand_ids=["secret"])
    assert out["nodes"]["secret"] == []
    assert any("secret" in n and "denied" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_expansion_count_is_capped_and_the_cap_is_reported(monkeypatch):
    ids = [f"g{i}" for i in range(im.MAX_EXPANSIONS_PER_REQUEST + 10)]
    fake = _FakeGraph({f"/groups/{g}/members": [] for g in ["root", *ids]})
    monkeypatch.setattr(im, "_graph_get", fake)
    out = await im.expand({}, "root", expand_ids=ids)
    assert len(fake.calls) == im.MAX_EXPANSIONS_PER_REQUEST
    assert any("throttling" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_no_connection_is_answered_rather_than_crashing():
    out = await im.expand(None, "root", expand_ids=[])
    assert out["nodes"] == {}
    assert any("No Azure connection" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_one_failing_branch_does_not_lose_the_others(monkeypatch):
    class _Boom(_FakeGraph):
        async def __call__(self, _conn, path, _params, cap):
            self.calls.append(path)
            if path == "/groups/bad/members":
                raise RuntimeError("socket reset")
            return self.by_path.get(path, []), ""

    monkeypatch.setattr(im, "_graph_get", _Boom({"/groups/root/members": [_u("u1")]}))
    out = await im.expand({}, "root", expand_ids=["bad"])
    assert [n["id"] for n in out["nodes"]["root"]] == ["u1"]
    assert any("could not be read" in n for n in out["notes"])


def test_summarise_counts_by_kind():
    assert im.summarize([
        {"kind": "user"}, {"kind": "user"}, {"kind": "group"},
    ]) == {"user": 2, "group": 1}


# ==================================================================== memberships (the way up)
# The mirror of `members`, and the reason the section exists at all: until this landed a user
# dossier could not say which groups the user was in. Its own honesty problem is the opposite
# one — the answer is never wrong, it is always INCOMPLETE, and a short list that reads as a
# complete one is how somebody concludes "this account is in no privileged group".
@pytest.fixture
def iam_groups(monkeypatch):
    """Point the IAM directory read at a dict the test controls."""
    from app.iam import cache

    state: dict[str, dict] = {}
    monkeypatch.setattr(cache, "read_directory", lambda _t: {"groups": state})
    return state


def _roles(group_members=None, assignments=None):
    return {"group_members": group_members or {}, "assignments": assignments or [],
            "eligible": []}


def test_memberships_union_three_sources_and_keep_why(iam_groups):
    """Each source answers a different 'why', and merging them away would lose the point."""
    iam_groups["g-azure"] = {"name": "Azure Contributors",
                             "members": [{"principalId": "u1"}, {"principalId": "u2"}]}
    rows, readable, note = im.cached_memberships(
        "t", "u1",
        stamp="s1",
        roles_data=_roles({"g-role": ["u1"], "g-azure": ["u1"]}),
        ca_data={"group_members": {"g-ca": ["u1"]}},
    )
    assert readable is True
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"g-azure", "g-role", "g-ca"}
    # A group reached two ways carries both reasons, not the first one found.
    assert by_id["g-azure"]["sources"] == [im.SOURCE_AZURE_RBAC, im.SOURCE_DIRECTORY_ROLE]
    assert by_id["g-role"]["sources"] == [im.SOURCE_DIRECTORY_ROLE]
    assert by_id["g-ca"]["sources"] == [im.SOURCE_CA_TARGET]
    assert "floor, not the complete list" in note


def test_another_principals_groups_are_not_returned(iam_groups):
    iam_groups["g1"] = {"name": "G1", "members": [{"principalId": "u2"}]}
    rows, readable, _ = im.cached_memberships(
        "t", "u1", stamp="s1", roles_data=_roles({"g2": ["u2"]}))
    assert readable is True
    assert rows == []


def test_an_empty_answer_is_a_floor_never_a_total(iam_groups):
    """The single most dangerous misreading this section can invite."""
    iam_groups["g1"] = {"name": "G1", "members": [{"principalId": "u2"}]}
    rows, readable, note = im.cached_memberships("t", "nobody", stamp="s1",
                                                 roles_data=_roles())
    assert rows == []
    # Readable: we DID look. It is simply not the whole directory.
    assert readable is True
    assert "floor, not the complete list" in note


def test_no_source_at_all_is_unreadable_not_empty(monkeypatch):
    from app.iam import cache

    def _boom(_t):
        raise OSError("disk gone")

    monkeypatch.setattr(cache, "read_directory", _boom)
    rows, readable, note = im.cached_memberships("t", "u1", stamp="s1", roles_data=_roles())
    assert rows == []
    assert readable is False
    assert "could not be read" in note


def test_a_dead_azure_cache_still_answers_from_the_directory_collection(monkeypatch):
    """Partial coverage is reported as partial, not thrown away."""
    from app.iam import cache

    def _boom(_t):
        raise OSError("disk gone")

    monkeypatch.setattr(cache, "read_directory", _boom)
    rows, readable, note = im.cached_memberships(
        "t", "u1", stamp="s1", roles_data=_roles({"g-role": ["u1"]}))
    assert readable is True
    assert [r["id"] for r in rows] == ["g-role"]
    assert "could not be read" in note and "floor" in note


def test_a_source_that_was_never_expanded_is_named(iam_groups):
    """'No CA group contains you' and 'no CA group was expanded' are opposite facts."""
    iam_groups["g1"] = {"name": "G1", "members": [{"principalId": "u1"}]}
    _rows, _readable, note = im.cached_memberships("t", "u1", stamp="s1", roles_data=_roles())
    assert im.SOURCE_LABEL[im.SOURCE_CA_TARGET] in note
    assert im.SOURCE_LABEL[im.SOURCE_DIRECTORY_ROLE] in note
    assert im.SOURCE_LABEL[im.SOURCE_AZURE_RBAC] not in note


def test_role_assignable_groups_lead_the_list(iam_groups):
    """Membership of one is a privilege-escalation path — it is why the section is opened."""
    iam_groups.update({
        "g-a": {"name": "Aaa ordinary", "members": [{"principalId": "u1"}]},
        "g-z": {"name": "Zzz privileged", "members": [{"principalId": "u1"}]},
    })
    people = [{"id": "g-a", "display_name": "Aaa ordinary"},
              {"id": "g-z", "display_name": "Zzz privileged", "is_assignable_to_role": True}]
    rows, _, _ = im.cached_memberships("t", "u1", stamp="s1", roles_data=_roles(),
                                       people_groups=people)
    assert [r["id"] for r in rows] == ["g-z", "g-a"]
    assert rows[0]["role_assignable"] is True


def test_group_facts_come_from_the_people_collection(iam_groups):
    """Dynamic and on-prem-synced change what the reader may DO about a membership."""
    iam_groups["g1"] = {"name": "stale name", "members": [{"principalId": "u1"}]}
    rows, _, _ = im.cached_memberships(
        "t", "u1", stamp="s1", roles_data=_roles(),
        people_groups=[{"id": "g1", "display_name": "Real Name", "dynamic": True,
                        "membership_rule": '(user.department -eq "IT")',
                        "on_prem_synced": True}])
    assert rows[0]["display_name"] == "Real Name"
    assert rows[0]["dynamic"] is True
    assert rows[0]["on_prem_synced"] is True
    assert rows[0]["membership_rule"] == '(user.department -eq "IT")'


def test_a_group_name_falls_back_to_its_role_assignment_row(iam_groups):  # noqa: ARG001
    """A raw GUID where a name belongs is a product bug, not a data limitation."""
    rows, _, _ = im.cached_memberships(
        "t", "u1", stamp="s1",
        roles_data=_roles({"g-role": ["u1"]},
                          [{"principal_id": "g-role", "principal_type": "Group",
                            "principal_name": "Helpdesk Admins"}]))
    assert rows[0]["display_name"] == "Helpdesk Admins"


def test_the_index_is_built_once_per_snapshot_and_rebuilt_for_a_new_one(monkeypatch):
    """It reads and parses the whole directory blob; Investigate is linked from dozens of
    places and the recents strip alone re-resolves several principals."""
    from app.iam import cache

    calls = []

    def _count(_t):
        calls.append(1)
        return {"groups": {"g1": {"name": "G1", "members": [{"principalId": "u1"}]}}}

    monkeypatch.setattr(cache, "read_directory", _count)
    for _ in range(5):
        im.cached_memberships("t", "u1", stamp="s1", roles_data=_roles())
    assert len(calls) == 1
    im.cached_memberships("t", "u1", stamp="s2", roles_data=_roles())
    assert len(calls) == 2, "a new collection must never be answered from the old index"


def test_two_tenants_never_share_an_index(monkeypatch):
    from app.iam import cache

    blobs = {
        "t1": {"groups": {"g1": {"name": "G1", "members": [{"principalId": "u1"}]}}},
        "t2": {"groups": {}},
    }
    monkeypatch.setattr(cache, "read_directory", lambda t: blobs[t])
    assert im.cached_memberships("t1", "u1", stamp="s", roles_data=_roles())[0]
    assert im.cached_memberships("t2", "u1", stamp="s", roles_data=_roles())[0] == []


# ----------------------------------------------------------------- live memberships (P2)
@pytest.mark.asyncio
async def test_a_user_is_read_from_the_users_collection(monkeypatch):
    """`memberOf` is declared on the concrete types, not on directoryObject, so the segment
    has to match the kind or Graph rejects the path."""
    fake = _FakeGraph({"/users/u1/memberOf": [_g("g1")]})
    monkeypatch.setattr(im, "_graph_get", fake)
    kids, _, err = await im.fetch_children({}, "u1", direction="up", kind="user")
    assert err == ""
    assert fake.calls == ["/users/u1/memberOf"]
    assert kids[0]["id"] == "g1"


@pytest.mark.asyncio
async def test_a_guest_reads_from_users_and_a_managed_identity_from_service_principals(monkeypatch):
    fake = _FakeGraph({"/users/x/memberOf": [], "/servicePrincipals/y/memberOf": []})
    monkeypatch.setattr(im, "_graph_get", fake)
    await im.fetch_children({}, "x", direction="up", kind="guest")
    await im.fetch_children({}, "y", direction="up", kind="managedIdentity")
    assert fake.calls == ["/users/x/memberOf", "/servicePrincipals/y/memberOf"]


@pytest.mark.asyncio
async def test_transitive_uses_the_other_navigation(monkeypatch):
    fake = _FakeGraph({"/users/u1/transitiveMemberOf": [_g("g1")]})
    monkeypatch.setattr(im, "_graph_get", fake)
    await im.fetch_children({}, "u1", direction="up", kind="user", transitive=True)
    assert fake.calls == ["/users/u1/transitiveMemberOf"]


@pytest.mark.asyncio
async def test_the_upward_read_sends_no_select(monkeypatch):
    """memberOf returns a heterogeneous directoryObject collection. Naming
    `userPrincipalName` on it is rejected outright and loses the whole answer to save a few
    hundred bytes."""
    seen: dict = {}

    async def _capture(_conn, path, params, cap):  # noqa: ARG001
        seen[path] = params
        return [], ""

    monkeypatch.setattr(im, "_graph_get", _capture)
    await im.fetch_children({}, "u1", direction="up", kind="user")
    await im.fetch_children({}, "g1", direction="down")
    assert "$select" not in seen["/users/u1/memberOf"]
    # Downward is a homogeneous members collection, where the select is both valid and worth it.
    assert "$select" in seen["/groups/g1/members"]


@pytest.mark.asyncio
async def test_a_directory_role_held_through_a_group_is_kept_and_leads(monkeypatch):
    """The most privileged thing the answer can contain. Dropping it as 'not a group' would
    silently discard the finding."""
    role = {"id": "r1", "displayName": "Global Administrator",
            "@odata.type": "#microsoft.graph.directoryRole"}
    monkeypatch.setattr(im, "_graph_get", _FakeGraph(
        {"/users/u1/memberOf": [_g("aaa"), role]}))
    kids, _, _ = await im.fetch_children({}, "u1", direction="up", kind="user")
    assert [k["kind"] for k in kids] == [im.TYPE_DIRECTORY_ROLE, im.TYPE_GROUP]
    assert kids[0]["expandable"] is False


@pytest.mark.asyncio
async def test_something_that_cannot_be_in_a_group_is_answered_with_a_sentence(monkeypatch):
    fake = _FakeGraph({})
    monkeypatch.setattr(im, "_graph_get", fake)
    kids, _, err = await im.fetch_children({}, "p1", direction="up", kind="platform")
    assert kids == [] and fake.calls == []
    assert "cannot belong to a group" in err


@pytest.mark.asyncio
async def test_only_the_root_carries_the_kind_and_the_transitive_flag(monkeypatch):
    """Every node the reader can open below the root is a group, and a transitive level
    below it would repeat what the next click already shows while destroying the shape."""
    fake = _FakeGraph({
        "/users/u1/transitiveMemberOf": [_g("g1")],
        "/groups/g1/memberOf": [_g("g2")],
    })
    monkeypatch.setattr(im, "_graph_get", fake)
    out = await im.expand({}, "u1", expand_ids=["g1"], direction="up",
                          root_kind="user", transitive=True)
    assert sorted(fake.calls) == ["/groups/g1/memberOf", "/users/u1/transitiveMemberOf"]
    assert set(out["nodes"]) == {"u1", "g1"}


@pytest.mark.asyncio
async def test_the_default_root_kind_keeps_the_group_behaviour(monkeypatch):
    """Regression guard: the existing group tree must not move when the root gains a kind."""
    fake = _FakeGraph({"/groups/root/memberOf": [_g("parent")]})
    monkeypatch.setattr(im, "_graph_get", fake)
    await im.expand({}, "root", expand_ids=[], direction="up")
    assert fake.calls == ["/groups/root/memberOf"]
