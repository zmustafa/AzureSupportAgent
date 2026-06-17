"""Tests for whole-tenant Backup & Restore (app.backup.registry).

Covers file-section round-trips, conflict modes (skip/overwrite/merge), secret redaction
and secret-preservation on restore, manifest validation, and a DB-section upsert with
tenant remapping.
"""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backup import registry as backup
from app.models import Base, Chat, Message, ScheduledTask


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point the backup module at an isolated .data directory."""
    monkeypatch.setattr(backup, "DATA_DIR", tmp_path)
    return tmp_path


def _write(data_dir: Path, filename: str, obj) -> None:
    (data_dir / filename).write_text(json.dumps(obj), encoding="utf-8")


def _read(data_dir: Path, filename: str):
    return json.loads((data_dir / filename).read_text(encoding="utf-8"))


def _export(section_ids):
    # File-only export never touches the DB, so a None session is safe.
    return asyncio.run(backup.build_backup(section_ids, "default", None))


# ----------------------------------------------------------------- collection sections
def test_collection_roundtrip_restores_identically(data_dir):
    _write(data_dir, "custom_agents.json", {"agents": {"a1": {"name": "Agent One", "instructions": "do x"}}})
    manifest = _export(["custom_agents"])
    assert manifest["format"] == backup.BACKUP_FORMAT
    assert manifest["sections"]["custom_agents"]["data"]["agents"]["a1"]["name"] == "Agent One"

    # Wipe, then restore.
    (data_dir / "custom_agents.json").unlink()
    res = asyncio.run(backup.apply_import(manifest, "default", "overwrite", None))
    counts = next(s for s in res["sections"] if s["id"] == "custom_agents")
    assert counts["created"] == 1
    assert _read(data_dir, "custom_agents.json")["agents"]["a1"]["instructions"] == "do x"


def test_collection_skip_preserves_existing(data_dir):
    _write(data_dir, "custom_agents.json", {"agents": {"a1": {"name": "Local"}}})
    manifest = _export(["custom_agents"])
    # Mutate local, then re-import with skip → local wins.
    _write(data_dir, "custom_agents.json", {"agents": {"a1": {"name": "Local edited"}, "a2": {"name": "Extra"}}})
    res = asyncio.run(backup.apply_import(manifest, "default", "skip", None))
    counts = next(s for s in res["sections"] if s["id"] == "custom_agents")
    assert counts["skipped"] == 1 and counts["updated"] == 0
    agents = _read(data_dir, "custom_agents.json")["agents"]
    assert agents["a1"]["name"] == "Local edited"  # untouched
    assert agents["a2"]["name"] == "Extra"  # local-only preserved


def test_collection_overwrite_updates_and_keeps_local_only(data_dir):
    _write(data_dir, "workbooks.json", {"workbooks": {"w1": {"name": "v1"}}})
    manifest = _export(["workbooks"])
    _write(data_dir, "workbooks.json", {"workbooks": {"w1": {"name": "local"}, "w2": {"name": "keep"}}})
    asyncio.run(backup.apply_import(manifest, "default", "overwrite", None))
    wbs = _read(data_dir, "workbooks.json")["workbooks"]
    assert wbs["w1"]["name"] == "v1"  # imported won
    assert wbs["w2"]["name"] == "keep"  # local-only survived


# ------------------------------------------------------------------- document sections
def test_document_overwrite_and_merge(data_dir):
    _write(data_dir, "app_settings.json", {"max_tokens": 4096, "auto_title": True})
    manifest = _export(["app_settings"])
    # Local diverges; merge keeps local-only keys, imported keys win.
    _write(data_dir, "app_settings.json", {"max_tokens": 8000, "extra": "local"})
    asyncio.run(backup.apply_import(manifest, "default", "merge", None))
    doc = _read(data_dir, "app_settings.json")
    assert doc["max_tokens"] == 4096  # imported won
    assert doc["auto_title"] is True  # imported key added
    assert doc["extra"] == "local"  # local-only preserved


def test_document_skip_leaves_local_untouched(data_dir):
    _write(data_dir, "app_settings.json", {"max_tokens": 4096})
    manifest = _export(["app_settings"])
    _write(data_dir, "app_settings.json", {"max_tokens": 9999})
    asyncio.run(backup.apply_import(manifest, "default", "skip", None))
    assert _read(data_dir, "app_settings.json")["max_tokens"] == 9999


# --------------------------------------------------------------------------- secrets
def test_azure_connection_secrets_redacted_and_preserved(data_dir):
    _write(
        data_dir,
        "azure_connections.json",
        {"connections": {"c1": {"display_name": "Contoso", "client_id": "abc", "client_secret": "enc:v1:SECRET"}}},
    )
    manifest = _export(["azure_connections"])
    exported = manifest["sections"]["azure_connections"]["data"]["connections"]["c1"]
    assert exported["client_secret"] is None  # redacted
    assert exported["client_id"] == "abc"  # non-secret kept
    assert any("Contoso" in s for s in manifest["meta"]["secrets_required"])

    # Restore onto a live local file that still holds the real secret → not clobbered.
    asyncio.run(backup.apply_import(manifest, "default", "overwrite", None))
    restored = _read(data_dir, "azure_connections.json")["connections"]["c1"]
    assert restored["client_secret"] == "enc:v1:SECRET"  # preserved
    assert restored["client_id"] == "abc"


def test_connector_secret_keys_redacted(data_dir, monkeypatch):
    monkeypatch.setattr(backup, "_connector_secret_keys", lambda rec: {"api_token"})
    _write(
        data_dir,
        "connectors.json",
        {"connectors": {"k1": {"name": "Jira", "type": "jira", "mode": "cloud", "api_token": "enc:tok", "base_url": "https://x"}}},
    )
    manifest = _export(["connectors"])
    exported = manifest["sections"]["connectors"]["data"]["connectors"]["k1"]
    assert exported["api_token"] is None
    assert exported["base_url"] == "https://x"


def test_llm_config_api_keys_redacted_and_preserved(data_dir):
    _write(
        data_dir,
        "llm_config.json",
        {"providers": {"openai": {"api_key": "sk-REAL", "model": "gpt-5"}, "ollama": {"api_key": "ollama"}}},
    )
    manifest = _export(["llm_config"])
    prov = manifest["sections"]["llm_config"]["data"]["providers"]
    assert prov["openai"]["api_key"] == ""  # redacted
    assert any("openai" in s for s in manifest["meta"]["secrets_required"])
    assert all("ollama" not in s for s in manifest["meta"]["secrets_required"])  # sentinel ignored

    # Restore onto a live file → the real key is preserved (blank doesn't clobber).
    asyncio.run(backup.apply_import(manifest, "default", "overwrite", None))
    restored = _read(data_dir, "llm_config.json")["providers"]
    assert restored["openai"]["api_key"] == "sk-REAL"
    assert restored["openai"]["model"] == "gpt-5"


# ----------------------------------------------------------------------- validation
def test_import_rejects_foreign_or_versioned_manifest(data_dir):
    with pytest.raises(ValueError):
        asyncio.run(backup.apply_import({"format": "something-else"}, "default", "merge", None))
    with pytest.raises(ValueError):
        asyncio.run(
            backup.apply_import(
                {"format": backup.BACKUP_FORMAT, "version": 999, "sections": {}}, "default", "merge", None
            )
        )


# --------------------------------------------------------------------------- DB section
def test_db_section_roundtrip_and_tenant_remap(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backup.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(ScheduledTask(id="t1", tenant_id="src", name="Nightly", schedule_kind="daily"))
            await s.commit()
            manifest = await backup.build_backup(["scheduled_tasks"], "src", s)
            # Drop the row so the restore exercises the create (insert) path.
            await s.delete(await s.get(ScheduledTask, "t1"))
            await s.commit()

        # Restore into a different tenant; the row should be remapped + inserted.
        async with Session() as s:
            res = await backup.apply_import(manifest, "dest", "overwrite", s)
            await s.commit()
            counts = next(x for x in res["sections"] if x["id"] == "scheduled_tasks")
            got = await s.get(ScheduledTask, "t1")
            await engine.dispose()
            return counts, got

    counts, got = asyncio.run(run())
    assert counts["created"] == 1
    assert got is not None and got.tenant_id == "dest" and got.name == "Nightly"


def test_chat_archive_exports_html_pages(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat-export.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            chat = Chat(tenant_id="src", user_id="u1", title="Exported Chat", provider="openai", model="gpt-5")
            s.add(chat)
            await s.flush()
            s.add(Message(chat_id=chat.id, role="user", content="Hello export"))
            await s.commit()

            archive = await backup.build_chat_archive("src", s)
            await engine.dispose()
            return chat.id, archive

    chat_id, archive = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = set(zf.namelist())
        assert "index.html" in names
        html_name = f"chats/chat-0001-{chat_id}.html"
        assert html_name in names
        html = zf.read(html_name).decode("utf-8")
        assert "Exported Chat" in html
        assert "Hello export" in html


def test_preview_reports_counts_without_writing(data_dir):
    _write(data_dir, "custom_agents.json", {"agents": {"a1": {"name": "Local"}}})
    manifest = _export(["custom_agents"])
    _write(data_dir, "custom_agents.json", {"agents": {"a1": {"name": "Local"}, "a2": {"name": "New local"}}})
    # Manifest has a1 only; preview against current local (a1 exists) in merge mode.
    preview = asyncio.run(backup.preview_import(manifest, "default", "merge", None))
    sec = next(s for s in preview["sections"] if s["id"] == "custom_agents")
    assert sec["incoming"] == 1 and sec["update"] == 1 and sec["create"] == 0
    # Preview must not have changed the file.
    assert "a2" in _read(data_dir, "custom_agents.json")["agents"]
