"""Unit tests for Evidence Locker soft-delete / trash."""
from __future__ import annotations

import importlib
from pathlib import Path


def _fresh(tmp_path, monkeypatch):
    from app.evidence import registry as reg

    importlib.reload(reg)
    monkeypatch.setattr(reg, "_INDEX", Path(tmp_path) / "evidence_locker.json")
    monkeypatch.setattr(reg, "_BLOB_DIR", Path(tmp_path) / "evidence")
    return reg


def _mk(reg, tenant="t1", name="snap"):
    return reg.create_snapshot(
        tenant_id=tenant, name=name, scope={"kind": "workload", "id": "w1", "resource_ids": []},
        included=["inventory"], retention_class="standard", tags=[],
        content={"inventory": {"resources": [{"id": "/r/a", "name": "a"}]}},
        created_by="dev", finding_links=[],
    )


def test_soft_delete_hides_from_list_and_shows_in_trash(tmp_path, monkeypatch):
    reg = _fresh(tmp_path, monkeypatch)
    a = _mk(reg)
    assert len(reg.list_snapshots("t1")) == 1
    reg.soft_delete("t1", a["id"], actor="dev")
    assert len(reg.list_snapshots("t1")) == 0  # hidden by default
    assert len(reg.list_snapshots("t1", include_deleted=True)) == 1
    trash = reg.list_trashed("t1")
    assert len(trash) == 1 and trash[0]["deleted_by"] == "dev" and trash[0]["deleted_at"]
    # blob + SHA preserved while trashed
    assert reg.get_content(a["id"]) is not None
    assert reg.verify_sha(reg.list_snapshots("t1", include_deleted=True)[0])


def test_restore(tmp_path, monkeypatch):
    reg = _fresh(tmp_path, monkeypatch)
    a = _mk(reg)
    reg.soft_delete("t1", a["id"])
    m = reg.restore("t1", a["id"])
    assert m and "deleted_at" not in m
    assert len(reg.list_snapshots("t1")) == 1
    assert len(reg.list_trashed("t1")) == 0


def test_purge_removes_blob(tmp_path, monkeypatch):
    reg = _fresh(tmp_path, monkeypatch)
    a = _mk(reg)
    reg.soft_delete("t1", a["id"])
    assert reg.purge("t1", a["id"]) is True
    assert reg.get_content(a["id"]) is None
    assert reg.get_meta("t1", a["id"]) is None
    assert len(reg.list_trashed("t1")) == 0


def test_empty_trash_only_trashed(tmp_path, monkeypatch):
    reg = _fresh(tmp_path, monkeypatch)
    a = _mk(reg, name="keep")
    b = _mk(reg, name="trash1")
    c = _mk(reg, name="trash2")
    reg.soft_delete("t1", b["id"])
    reg.soft_delete("t1", c["id"])
    n = reg.empty_trash("t1")
    assert n == 2
    assert len(reg.list_snapshots("t1")) == 1  # 'keep' survives
    assert reg.get_meta("t1", a["id"]) is not None


def test_tenant_isolation(tmp_path, monkeypatch):
    reg = _fresh(tmp_path, monkeypatch)
    a = _mk(reg, tenant="t1")
    assert reg.soft_delete("t2", a["id"]) is None
    assert reg.restore("t2", a["id"]) is None
    assert reg.purge("t2", a["id"]) is False
