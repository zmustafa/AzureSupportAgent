"""Tests for management-group root detection (arm._roots_from_entities) — the logic that keeps
only ROOT management groups at the top of the scope tree so nested MGs are revealed by
expansion instead of listed flat alongside their parent.
"""
from app.azure.arm import _roots_from_entities, _skiptoken

_MG = "Microsoft.Management/managementGroups"


def _ent(name: str, parent: str | None = None, display: str | None = None) -> dict:
    props: dict = {"displayName": display or name}
    if parent is not None:
        props["parent"] = {"id": f"/providers/Microsoft.Management/managementGroups/{parent}"}
    return {"name": name, "type": _MG, "properties": props}


def test_single_root_with_nested_child_returns_only_root():
    # Tenant Root Group with a child "MG" → only the root is top-level.
    entities = [
        _ent("TenantRoot", parent=None, display="Tenant Root Group"),
        _ent("MG", parent="TenantRoot", display="MG"),
    ]
    roots = _roots_from_entities(entities)
    assert [r["id"] for r in roots] == ["TenantRoot"]
    assert roots[0]["name"] == "Tenant Root Group"


def test_deeply_nested_only_returns_top_root():
    entities = [
        _ent("Root"),
        _ent("L1", parent="Root"),
        _ent("L2", parent="L1"),
    ]
    assert [r["id"] for r in _roots_from_entities(entities)] == ["Root"]


def test_child_visible_without_parent_is_treated_as_root():
    # User can see "MG" but NOT its parent "TenantRoot" → MG is the root of the visible forest.
    entities = [_ent("MG", parent="TenantRoot", display="MG")]
    roots = _roots_from_entities(entities)
    assert [r["id"] for r in roots] == ["MG"]


def test_multiple_roots_preserved():
    entities = [
        _ent("RootA", parent=None, display="Root A"),
        _ent("RootB", parent=None, display="Root B"),
        _ent("ChildA", parent="RootA"),
    ]
    ids = {r["id"] for r in _roots_from_entities(entities)}
    assert ids == {"RootA", "RootB"}


def test_subscriptions_in_entities_are_ignored():
    entities = [
        _ent("Root"),
        {"name": "sub-1", "type": "/subscriptions", "properties": {"displayName": "Sub 1"}},
    ]
    assert [r["id"] for r in _roots_from_entities(entities)] == ["Root"]


def test_display_name_falls_back_to_id():
    entities = [{"name": "Root", "type": _MG, "properties": {}}]
    roots = _roots_from_entities(entities)
    assert roots == [{"id": "Root", "name": "Root"}]


def test_empty_entities_returns_empty():
    assert _roots_from_entities([]) == []


def test_skiptoken_parsing():
    assert _skiptoken("") == ""
    assert _skiptoken("https://management.azure.com/x?$skiptoken=abc123&api-version=2020-05-01") == "abc123"
    assert _skiptoken("https://management.azure.com/x?api-version=2020-05-01") == ""
