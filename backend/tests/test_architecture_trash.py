"""Tests for the architectures Trash (soft-delete → restore → purge) lifecycle."""
from pathlib import Path

import pytest

from app.architectures import activity, registry, revisions


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point every architecture JSON store at a temp dir so tests don't touch real data."""
    monkeypatch.setattr(registry, "_PATH", tmp_path / "architectures.json")
    monkeypatch.setattr(revisions, "_PATH", tmp_path / "architecture_revisions.json")
    monkeypatch.setattr(activity, "_PATH", tmp_path / "architecture_activity.json")
    yield


def _make(name="App", tenant="t1"):
    return registry.upsert_architecture({"name": name, "tenant_id": tenant}, actor="alice")


def test_delete_is_soft_and_hidden_from_active_list():
    a = _make()
    assert registry.delete_architecture(a["id"], actor="alice") is True
    # Hidden from the active list + get, but present when include_deleted.
    assert registry.list_architectures("t1") == []
    assert registry.get_architecture(a["id"]) is None
    assert registry.get_architecture(a["id"], include_deleted=True) is not None
    trashed = registry.list_trashed_architectures("t1")
    assert [t["id"] for t in trashed] == [a["id"]]
    assert trashed[0]["deleted_at"]


def test_delete_twice_returns_false():
    a = _make()
    assert registry.delete_architecture(a["id"]) is True
    assert registry.delete_architecture(a["id"]) is False  # already trashed


def test_restore_brings_it_back():
    a = _make()
    registry.delete_architecture(a["id"])
    restored = registry.restore_architecture(a["id"], actor="bob")
    assert restored is not None
    assert not restored["deleted_at"]
    assert registry.get_architecture(a["id"]) is not None
    assert registry.list_trashed_architectures("t1") == []


def test_restore_when_not_trashed_returns_none():
    a = _make()
    assert registry.restore_architecture(a["id"]) is None  # active, not in trash


def test_soft_delete_preserves_revisions_restore_is_lossless():
    a = _make()
    # upsert created an initial revision; edit to add another.
    registry.upsert_architecture({"id": a["id"], "name": "App v2"}, actor="alice", reason="Edited")
    before = revisions.list_revisions(a["id"])
    assert len(before) >= 1
    registry.delete_architecture(a["id"])
    # Revisions survive a soft delete (lossless restore).
    assert revisions.list_revisions(a["id"]) == before
    registry.restore_architecture(a["id"])
    assert revisions.list_revisions(a["id"]) == before


def test_purge_is_permanent_and_drops_history():
    a = _make()
    registry.delete_architecture(a["id"])
    assert registry.purge_architecture(a["id"]) is True
    assert registry.get_architecture(a["id"], include_deleted=True) is None
    assert registry.list_trashed_architectures("t1") == []
    assert revisions.list_revisions(a["id"]) == []
    assert registry.purge_architecture(a["id"]) is False  # gone


def test_empty_trash_only_removes_trashed_and_is_tenant_scoped():
    keep = _make("Keep", tenant="t1")          # active, stays
    gone1 = _make("Gone1", tenant="t1")
    gone2 = _make("Gone2", tenant="t1")
    other = _make("Other", tenant="t2")        # different tenant
    registry.delete_architecture(gone1["id"])
    registry.delete_architecture(gone2["id"])
    registry.delete_architecture(other["id"])

    removed = registry.empty_architecture_trash("t1")
    assert removed == 2
    # Active one untouched; t2's trashed item untouched by a t1-scoped empty.
    assert registry.get_architecture(keep["id"]) is not None
    assert registry.get_architecture(gone1["id"], include_deleted=True) is None
    assert registry.get_architecture(other["id"], include_deleted=True) is not None


def test_trash_and_restore_log_activity():
    a = _make()
    registry.delete_architecture(a["id"], actor="alice")
    events = [e["event"] for e in activity.list_activity(a["id"])]
    assert activity.TRASHED in events
    registry.restore_architecture(a["id"], actor="alice")
    events = [e["event"] for e in activity.list_activity(a["id"])]
    assert activity.RESTORED in events
