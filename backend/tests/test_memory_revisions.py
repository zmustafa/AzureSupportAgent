"""Tests for architecture memory revision history + restore."""
import app.architectures.memory as mem
import app.architectures.memory_revisions as rev


def _isolate(tmp_path, monkeypatch):
    """Point both registries at temp files so tests don't touch real data."""
    monkeypatch.setattr(mem, "_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(rev, "_PATH", tmp_path / "memory_rev.json")


def test_first_upsert_snapshots_created(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    revs = rev.list_revisions("arch1")
    assert len(revs) == 1
    assert revs[0]["reason"] == "Created"


def test_dedup_skips_identical_snapshot(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    # Same content again → no new revision.
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    assert len(rev.list_revisions("arch1")) == 1


def test_each_change_adds_a_revision_newest_first(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v2"}], actor="me")
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v3"}], actor="me")
    revs = rev.list_revisions("arch1")
    assert len(revs) == 3
    # Newest first.
    assert revs[0]["created_at"] >= revs[1]["created_at"] >= revs[2]["created_at"]


def test_restore_brings_back_old_content_and_snapshots_current(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v2"}], actor="me")
    revs = rev.list_revisions("arch1")  # [v2, v1]
    v1_id = revs[-1]["id"]
    restored = mem.restore_revision("arch1", v1_id, actor="me")
    assert restored is not None
    assert restored["sections"][0]["content"] == "v1"
    # A new "Restored from history" revision now tops the list (v2 was snapshotted before).
    revs2 = rev.list_revisions("arch1")
    assert revs2[0]["reason"] == "Restored from history"


def test_delete_memory_drops_revisions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    assert len(rev.list_revisions("arch1")) == 1
    mem.delete_memory("arch1")
    assert rev.list_revisions("arch1") == []


def test_restore_unknown_returns_none(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem.upsert_memory("arch1", sections=[{"key": "overview", "content": "v1"}], actor="me")
    assert mem.restore_revision("arch1", "nope", actor="me") is None
    assert mem.restore_revision("missing-arch", "x", actor="me") is None
