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
    assert im.summarise([
        {"kind": "user"}, {"kind": "user"}, {"kind": "group"},
    ]) == {"user": 2, "group": 1}
