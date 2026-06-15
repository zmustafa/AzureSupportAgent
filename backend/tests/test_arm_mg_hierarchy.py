"""Tests for management-group root detection (arm._roots_from_entities) — the logic that keeps
only ROOT management groups at the top of the scope tree so nested MGs are revealed by
expansion instead of listed flat alongside their parent.
"""
from app.azure.arm import _flatten_entities, _roots_from_entities, _skiptoken

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


# --------------------------------------------------------------------------- flat hierarchy
def test_flatten_nests_children_under_root_with_depth():
    # Tenant Root Group with a nested child — the flat picker must list BOTH, child indented.
    entities = [
        _ent("TenantRoot", parent=None, display="Tenant Root Group"),
        _ent("Platform", parent="TenantRoot", display="Platform"),
    ]
    flat = _flatten_entities(entities)
    assert [(g["id"], g["depth"]) for g in flat] == [("TenantRoot", 0), ("Platform", 1)]
    assert flat[0]["name"] == "Tenant Root Group"


def test_flatten_deep_hierarchy_depths_and_order():
    entities = [
        _ent("Root", display="Root"),
        _ent("L1", parent="Root", display="L1"),
        _ent("L2", parent="L1", display="L2"),
        _ent("Sibling", parent="Root", display="Sibling"),
    ]
    flat = _flatten_entities(entities)
    # Parent precedes its children; siblings are alphabetized (L1 before Sibling).
    assert [(g["id"], g["depth"]) for g in flat] == [
        ("Root", 0), ("L1", 1), ("L2", 2), ("Sibling", 1),
    ]


def test_flatten_includes_every_management_group():
    # The whole point of the fix: a nested MG ("inside" the root) must be present + selectable.
    entities = [
        _ent("TenantRoot", parent=None, display="Tenant Root Group"),
        _ent("Prod", parent="TenantRoot"),
        _ent("Dev", parent="TenantRoot"),
        _ent("ProdApps", parent="Prod"),
    ]
    ids = {g["id"] for g in _flatten_entities(entities)}
    assert ids == {"TenantRoot", "Prod", "Dev", "ProdApps"}


def test_flatten_child_visible_without_parent_is_root():
    entities = [_ent("MG", parent="TenantRoot", display="MG")]
    flat = _flatten_entities(entities)
    assert [(g["id"], g["depth"]) for g in flat] == [("MG", 0)]


def test_flatten_ignores_subscriptions_and_handles_empty():
    entities = [
        _ent("Root"),
        {"name": "sub-1", "type": "/subscriptions", "properties": {"displayName": "Sub 1"}},
    ]
    assert [g["id"] for g in _flatten_entities(entities)] == ["Root"]
    assert _flatten_entities([]) == []


def test_flatten_survives_a_cycle():
    # Defensive: a parent loop must not infinite-recurse; every node still appears once.
    entities = [
        _ent("A", parent="B"),
        _ent("B", parent="A"),
    ]
    flat = _flatten_entities(entities)
    assert {g["id"] for g in flat} == {"A", "B"}

