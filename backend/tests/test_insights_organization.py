"""AI Insight Packs — Library organization (pin + collections) tests.

Covers the registry helpers behind the Library screen's grouping/organization features:
``set_pinned``, the collections CRUD (``create_collection`` / ``update_collection`` /
``delete_collection`` / ``list_collections``), and ``set_pack_collections`` membership —
including that deleting a collection cascades removal from every pack, and that pinned /
collection state survives a reseed on version bump.
"""
import importlib

import pytest

from app.insights import packfile


@pytest.fixture()
def registry(monkeypatch, tmp_path):
    """Point the registry at an isolated JSON store per test."""
    from app.insights import registry as reg
    importlib.reload(reg)
    monkeypatch.setattr(reg, "_PATH", tmp_path / "insight_packs.json")
    return reg


def _a_pack(registry):
    packs = registry.list_packs()
    assert packs, "seed should provide built-in packs"
    return packs[0]


# ------------------------------------------------------------------- pinning
def test_set_pinned_toggles_and_persists(registry):
    p = _a_pack(registry)
    assert p.get("pinned") is False
    out = registry.set_pinned(p["id"], True)
    assert out is not None and out["pinned"] is True
    assert registry.get_pack(p["id"])["pinned"] is True
    registry.set_pinned(p["id"], False)
    assert registry.get_pack(p["id"])["pinned"] is False


def test_set_pinned_unknown_pack_is_none(registry):
    assert registry.set_pinned("nope", True) is None


# ---------------------------------------------------------------- collections
def test_create_list_and_rename_collection(registry):
    col = registry.create_collection("Prod watchers")
    assert col and col["name"] == "Prod watchers" and col["id"]
    assert any(c["id"] == col["id"] for c in registry.list_collections())
    registry.update_collection(col["id"], name="Renamed")
    assert next(c for c in registry.list_collections() if c["id"] == col["id"])["name"] == "Renamed"


def test_create_collection_rejects_blank_name(registry):
    assert registry.create_collection("   ") is None


def test_set_pack_collections_validates_ids(registry):
    p = _a_pack(registry)
    col = registry.create_collection("Group A")
    out = registry.set_pack_collections(p["id"], [col["id"], "bogus"])
    assert out["collection_ids"] == [col["id"]]  # unknown id dropped


def test_delete_collection_cascades_from_packs(registry):
    p = _a_pack(registry)
    col = registry.create_collection("Temp")
    registry.set_pack_collections(p["id"], [col["id"]])
    assert col["id"] in registry.get_pack(p["id"])["collection_ids"]
    assert registry.delete_collection(col["id"]) is True
    assert registry.get_pack(p["id"])["collection_ids"] == []
    assert all(c["id"] != col["id"] for c in registry.list_collections())


def test_delete_unknown_collection_is_false(registry):
    assert registry.delete_collection("nope") is False


# ---------------------------------------------------- state survives a reseed
def test_pin_and_collections_survive_reseed(registry):
    p = _a_pack(registry)
    col = registry.create_collection("Keep me")
    registry.set_pinned(p["id"], True)
    registry.set_pack_collections(p["id"], [col["id"]])

    # Force a reseed by bumping the stored seed version, then re-read.
    data = registry._read()
    data["seed_version"] = -1
    registry._write(data)
    registry.list_packs()  # triggers _ensure_seeded

    after = registry.get_pack(p["id"])
    assert after["pinned"] is True
    assert after["collection_ids"] == [col["id"]]


# --------------------------------------------------- packfile normalize defaults
def test_normalize_defaults_pin_and_collections():
    p = packfile.normalize({"name": "x"})
    assert p["pinned"] is False
    assert p["collection_ids"] == []
