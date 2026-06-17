"""Whole-tenant Backup & Restore — manifest format + section registry.

Produces a portable JSON manifest (``azsupagent.backup`` v1) of a tenant's
configuration and operational data, and packages it into a ZIP archive for export.
The ZIP can also carry an export-only nested chats archive rendered as HTML.
Restores work from the manifest data on the same (or a rebuilt) instance.
Mirrors :mod:`app.automations.portability`: a versioned manifest, a registry of sections
(each knows how to count / collect / restore itself), secret redaction on export, and
conflict handling on import (``skip`` | ``overwrite`` | ``merge``).

Two storage layers are covered:

- **File-backed JSON registries** under ``backend/.data/`` (config + reference sets).
  Each file is either an *id-keyed collection* (``{collection_key: {id: obj}}``) or a
  single *document* (a settings blob). Collections merge per id; documents are replaced
  (``overwrite``) or shallow-merged (``merge``).
- **Tenant-scoped DB rows** (scheduled tasks, assessment waivers, finding state,
  notification rules). Upserted by primary key; local-only rows are never deleted.

Secrets are **never** exported: secret-bearing fields are redacted to ``None`` and listed
under ``meta.secrets_required`` so an operator can re-enter them after import. On restore
an existing local secret is never overwritten with a redacted blank, so restoring a
backup onto a live instance keeps its working credentials.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import DateTime, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    AssessmentFindingState,
    AssessmentWaiver,
    Chat,
    NotificationRule,
    Message,
    ScheduledTask,
)

logger = logging.getLogger("app.backup.registry")

BACKUP_FORMAT = "azsupagent.backup"
BACKUP_VERSION = 1
CONFLICT_MODES = ("skip", "overwrite", "merge")

DATA_DIR = Path(__file__).resolve().parents[2] / ".data"


# ---------------------------------------------------------------------------- specs
@dataclass(frozen=True)
class FileSection:
    """A file-backed registry section under ``.data/``."""

    id: str
    label: str
    tier: str  # "config" | "reference" | "secrets"
    filename: str
    # For an id-keyed collection ``{collection_key: {id: obj}}``. None => single document.
    collection_key: str | None = None
    # Redaction strategy for secret-bearing files: "" | "azure_connections" | "connectors"
    # | "llm_config". Determines how secrets are stripped on export and preserved on
    # restore (an existing local secret is never clobbered with a redacted blank).
    secret_kind: str = ""
    kind: str = "file"


@dataclass(frozen=True)
class DbSection:
    """A tenant-scoped DB table section."""

    id: str
    label: str
    tier: str
    model: Any  # ORM class
    kind: str = "db"


# Curated set of backup-able sections. Adding a new one is a single entry here.
FILE_SECTIONS: tuple[FileSection, ...] = (
    # --- Core configuration (no secrets) -----------------------------------------
    FileSection("app_settings", "Application settings", "config", "app_settings.json"),
    FileSection("auth_settings", "Security policy settings", "config", "auth_settings.json"),
    FileSection("ai_prompts", "System prompt overrides", "config", "ai_prompts.json"),
    FileSection("custom_agents", "Sub agents", "config", "custom_agents.json", "agents"),
    FileSection("workbooks", "Workbooks", "config", "workbooks.json", "workbooks"),
    FileSection("playbooks", "Playbooks", "config", "playbooks.json", "playbooks"),
    FileSection("architectures", "Architectures", "config", "architectures.json", "architectures"),
    FileSection(
        "architecture_collections", "Architecture collections", "config",
        "architecture_collections.json", "collections",
    ),
    FileSection("workloads", "Workloads", "config", "workloads.json", "workloads"),
    FileSection("assessment_checks", "Custom assessment checks", "config", "assessment_checks.json"),
    FileSection("monitor_dashboards", "Monitor dashboards", "config", "monitor_dashboards.json", "dashboards"),
    FileSection("policy", "Policy snapshots & drafts", "config", "policy.json"),
    # --- Reference sets (no secrets) ---------------------------------------------
    FileSection("amba_reference", "AMBA reference set", "reference", "amba_reference.json"),
    FileSection("backupdr_reference", "Backup/DR reference set", "reference", "backupdr_reference.json"),
    FileSection("radar_reference", "Retirement Radar reference", "reference", "radar_reference.json"),
    FileSection("telemetry_reference", "Telemetry reference set", "reference", "telemetry_reference.json"),
    # --- Secret-bearing config (redacted on export) ------------------------------
    FileSection("llm_config", "AI providers", "secrets", "llm_config.json", secret_kind="llm_config"),
    FileSection("connectors", "Connectors", "secrets", "connectors.json", "connectors", secret_kind="connectors"),
    FileSection(
        "azure_connections", "Azure tenant connections", "secrets",
        "azure_connections.json", "connections", secret_kind="azure_connections",
    ),
)

DB_SECTIONS: tuple[DbSection, ...] = (
    DbSection("scheduled_tasks", "Scheduled tasks", "data", ScheduledTask),
    DbSection("assessment_waivers", "Assessment waivers", "data", AssessmentWaiver),
    DbSection("assessment_finding_state", "Assessment finding state", "data", AssessmentFindingState),
    DbSection("notification_rules", "Notification rules", "data", NotificationRule),
)

_FILE_BY_ID = {s.id: s for s in FILE_SECTIONS}
_DB_BY_ID = {s.id: s for s in DB_SECTIONS}
ALL_SECTION_IDS = tuple(s.id for s in FILE_SECTIONS) + tuple(s.id for s in DB_SECTIONS)


# ------------------------------------------------------------------------ file utils
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_path(filename: str) -> Path:
    return DATA_DIR / filename


def _read_json(filename: str) -> Any:
    """Parse a ``.data`` JSON file, or None when missing/unreadable."""
    path = _data_path(filename)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Backup: could not read %s: %s", filename, exc)
        return None


def _atomic_write_json(filename: str, data: Any) -> None:
    """Write ``data`` to a ``.data`` file atomically (temp file + os.replace)."""
    path = _data_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _collection(data: Any, key: str) -> dict[str, Any]:
    """Pull the id-keyed dict out of a collection file's parsed content."""
    if isinstance(data, dict) and isinstance(data.get(key), dict):
        return data[key]
    return {}


def _safe_slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_")
    return slug or fallback


def _message_html(message: Message) -> str:
    meta_bits = [message.role]
    if message.provider:
        meta_bits.append(f"provider={escape(message.provider)}")
    if message.model:
        meta_bits.append(f"model={escape(message.model)}")
    if message.duration_ms is not None:
        meta_bits.append(f"{message.duration_ms} ms")
    body = escape(message.content or "")
    extra: list[str] = []
    if message.activity_json:
        extra.append(
            f"<details><summary>Activity</summary><pre>{escape(json.dumps(message.activity_json, indent=2, ensure_ascii=False))}</pre></details>"
        )
    if message.images_json:
        extra.append(
            f"<details><summary>Images</summary><pre>{escape(json.dumps(message.images_json, indent=2, ensure_ascii=False))}</pre></details>"
        )
    if getattr(message, "investigation_json", None):
        extra.append(
            f"<details><summary>Investigation</summary><pre>{escape(json.dumps(message.investigation_json, indent=2, ensure_ascii=False))}</pre></details>"
        )
    return f"""
      <article class=\"message role-{escape(message.role or 'unknown')}\">
        <div class=\"message-meta\">{' · '.join(meta_bits)}</div>
        <pre class=\"message-content\">{body}</pre>
        {''.join(extra)}
      </article>
    """


def _chat_html(chat: Chat) -> str:
    messages = list(chat.messages or [])
    title = escape(chat.title or "New Chat")
    meta = [
        f"Chat ID: {escape(chat.id)}",
        f"User: {escape(chat.user_id)}",
        f"Created: {escape(chat.created_at.isoformat())}",
        f"Archived: {'yes' if chat.archived else 'no'}",
        f"Pinned: {'yes' if chat.pinned else 'no'}",
    ]
    if chat.provider:
        meta.append(f"Provider: {escape(chat.provider)}")
    if chat.model:
        meta.append(f"Model: {escape(chat.model)}")
    if chat.connection_id:
        meta.append(f"Connection: {escape(chat.connection_id)}")
    if chat.thinking_level:
        meta.append(f"Thinking: {escape(chat.thinking_level)}")
    if chat.agent_id:
        meta.append(f"Agent: {escape(chat.agent_id)}")
    if chat.workload_id:
        meta.append(f"Workload: {escape(chat.workload_id)}")

    system_prompt = (
        f"<section class=\"system-prompt\"><h2>System prompt</h2><pre>{escape(chat.system_prompt or '')}</pre></section>"
        if chat.system_prompt
        else ""
    )
    messages_html = "".join(_message_html(message) for message in messages)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f7f7fb; color: #101828; }}
    .page {{ max-width: 980px; margin: 0 auto; background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 24px; box-shadow: 0 14px 40px rgba(15, 23, 42, 0.08); }}
    header h1 {{ margin: 0 0 12px; font-size: 28px; line-height: 1.15; }}
    .meta {{ display: grid; gap: 6px; margin-bottom: 18px; font-size: 12px; color: #475467; }}
    section {{ margin-top: 20px; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; }}
    pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.5; }}
    .message {{ border: 1px solid #d0d5dd; border-radius: 14px; padding: 14px; margin-top: 12px; background: #fcfcfd; }}
    .message.role-assistant {{ border-color: #b2ddff; background: #f8fbff; }}
    .message.role-user {{ border-color: #d6bbfb; background: #fbf8ff; }}
    .message.role-system, .message.role-tool {{ border-color: #d0d5dd; }}
    .message-meta {{ margin-bottom: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #667085; }}
    details {{ margin-top: 10px; }}
    summary {{ cursor: pointer; font-size: 12px; font-weight: 600; color: #344054; }}
  </style>
</head>
<body>
  <div class=\"page\">
    <header>
      <h1>{title}</h1>
      <div class=\"meta\">{''.join(f'<div>{item}</div>' for item in meta)}</div>
    </header>
    {system_prompt}
    <section>
      <h2>Messages</h2>
      {messages_html or '<div>No messages in this chat.</div>'}
    </section>
  </div>
</body>
</html>"""


async def build_chat_archive(tenant_id: str, db: AsyncSession) -> bytes:
    """Build a ZIP archive of all tenant chats as HTML pages plus an index."""
    result = await db.execute(
        select(Chat)
        .options(selectinload(Chat.messages))
        .where(Chat.tenant_id == tenant_id)
        .order_by(Chat.created_at, Chat.id)
    )
    chats = list(result.scalars().all())
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as zf:
        index_lines = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8" />',
            '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
            "  <title>Chats export</title>",
            "  <style>",
            "    body { margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f7f7fb; color: #101828; }",
            "    .page { max-width: 980px; margin: 0 auto; background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 24px; }",
            "    .chat { margin-top: 14px; padding: 14px; border: 1px solid #e5e7eb; border-radius: 12px; background: #f9fafb; }",
            "    a { color: #175cd3; text-decoration: none; }",
            "    a:hover { text-decoration: underline; }",
            "    .meta { margin-top: 4px; font-size: 12px; color: #667085; }",
            "  </style>",
            "</head>",
            "<body>",
            '  <div class="page">',
            "    <h1>Chats export</h1>",
            f"    <p>Tenant {escape(tenant_id)} · {len(chats)} chat{'s' if len(chats) != 1 else ''}</p>",
        ]
        if not chats:
            index_lines.append('    <div class="chat">No chats were found for this tenant.</div>')
        for idx, chat in enumerate(chats, start=1):
            filename = f"chats/chat-{idx:04d}-{chat.id}.html"
            zf.writestr(filename, _chat_html(chat))
            index_lines.extend(
                [
                    '    <div class="chat">',
                    f'      <a href="{escape(filename)}">{escape(chat.title or "New Chat")}</a>',
                    f'      <div class="meta">{escape(chat.id)} · {escape(chat.created_at.isoformat())} · {len(chat.messages or [])} message{"s" if len(chat.messages or []) != 1 else ""}</div>',
                    '    </div>',
                ]
            )
        index_lines.extend(["  </div>", "</body>", "</html>"])
        zf.writestr("index.html", "\n".join(index_lines))
    return buffer.getvalue()


# --------------------------------------------------------------------------- secrets
def _connector_secret_keys(record: dict[str, Any]) -> set[str]:
    from app.connectors.registry import _secret_keys

    return _secret_keys(record.get("type", ""), record.get("mode", ""))


# Fixed secret fields for the Azure-connections records.
_AZ_CONN_SECRETS = (
    "client_secret",
    "certificate_pem",
    "access_token",
    "refresh_token",
    "graph_access_token",
)


def _redact_record(spec: FileSection, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a redacted copy of one collection record + whether it carried a secret."""
    out = dict(record)
    had_secret = False
    if spec.secret_kind == "azure_connections":
        keys: set[str] = set(_AZ_CONN_SECRETS)
    elif spec.secret_kind == "connectors":
        keys = _connector_secret_keys(record)
    else:
        keys = set()
    for k in keys:
        if out.get(k):
            had_secret = True
        if k in out:
            out[k] = None
    return out, had_secret


def _redact_llm_config(data: Any) -> tuple[Any, list[str]]:
    """Redact ``api_key`` from every provider in an llm_config document."""
    refs: list[str] = []
    if not isinstance(data, dict):
        return data, refs
    out = json.loads(json.dumps(data))  # deep copy
    providers = out.get("providers")
    if isinstance(providers, dict):
        for name, prov in providers.items():
            if isinstance(prov, dict) and prov.get("api_key"):
                # Local providers use a base URL / sentinel, not a real secret.
                if str(prov.get("api_key")) not in ("ollama",):
                    refs.append(f"AI provider: {name}")
                prov["api_key"] = ""
    return out, refs


# ----------------------------------------------------------------- file collect/restore
def _collect_file(spec: FileSection) -> tuple[Any, list[str]]:
    """Read + redact a file section. Returns ``(payload, secrets_required)``."""
    data = _read_json(spec.filename)
    if data is None:
        return None, []
    secrets: list[str] = []
    if spec.secret_kind == "llm_config":
        data, secrets = _redact_llm_config(data)
        return data, secrets
    if spec.collection_key and spec.secret_kind:
        coll = _collection(data, spec.collection_key)
        redacted: dict[str, Any] = {}
        for cid, record in coll.items():
            if isinstance(record, dict):
                red, had = _redact_record(spec, record)
                redacted[cid] = red
                if had:
                    label = record.get("name") or record.get("display_name") or cid
                    secrets.append(f"{spec.label[:-1] if spec.label.endswith('s') else spec.label}: {label}")
            else:
                redacted[cid] = record
        out = dict(data) if isinstance(data, dict) else {}
        out[spec.collection_key] = redacted
        return out, secrets
    return data, secrets


def _count_file(spec: FileSection) -> int:
    data = _read_json(spec.filename)
    if data is None:
        return 0
    if spec.collection_key:
        return len(_collection(data, spec.collection_key))
    return 1


def _preserve_secrets(spec: FileSection, incoming: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Copy local (encrypted) secret values into an incoming record so a redacted blank
    never clobbers a working credential on restore."""
    if spec.secret_kind == "azure_connections":
        keys: set[str] = set(_AZ_CONN_SECRETS)
    elif spec.secret_kind == "connectors":
        keys = _connector_secret_keys({**local, **incoming})
    else:
        keys = set()
    out = dict(incoming)
    for k in keys:
        if not out.get(k) and local.get(k):
            out[k] = local[k]
    return out


def _restore_file(spec: FileSection, payload: Any, mode: str) -> dict[str, int]:
    """Apply a file section's payload to disk under the chosen conflict mode."""
    if payload is None:
        return {"created": 0, "updated": 0, "skipped": 0}
    local = _read_json(spec.filename)
    local_exists = local is not None

    # ---- Document section (whole-file blob) -------------------------------------
    if not spec.collection_key:
        if spec.secret_kind == "llm_config":
            return _restore_llm_config(spec, payload, mode, local)
        if local_exists and mode == "skip":
            return {"created": 0, "updated": 0, "skipped": 1}
        if mode == "merge" and isinstance(local, dict) and isinstance(payload, dict):
            merged = {**local, **payload}
            _atomic_write_json(spec.filename, merged)
        else:  # overwrite (or no local yet)
            _atomic_write_json(spec.filename, payload)
        return {"created": 0 if local_exists else 1, "updated": 1 if local_exists else 0, "skipped": 0}

    # ---- Collection section (id-keyed) ------------------------------------------
    incoming = _collection(payload, spec.collection_key)
    base = local if isinstance(local, dict) else {}
    current = dict(_collection(base, spec.collection_key))
    created = updated = skipped = 0
    for cid, record in incoming.items():
        exists = cid in current
        if exists and mode == "skip":
            skipped += 1
            continue
        if isinstance(record, dict) and spec.secret_kind and exists and isinstance(current.get(cid), dict):
            record = _preserve_secrets(spec, record, current[cid])
        current[cid] = record
        if exists:
            updated += 1
        else:
            created += 1
    out = dict(base)
    out[spec.collection_key] = current
    _atomic_write_json(spec.filename, out)
    return {"created": created, "updated": updated, "skipped": skipped}


def _restore_llm_config(spec: FileSection, payload: Any, mode: str, local: Any) -> dict[str, int]:
    """Restore the llm_config document, preserving locally-stored provider api keys."""
    if not isinstance(payload, dict):
        return {"created": 0, "updated": 0, "skipped": 0}
    local_exists = local is not None
    if local_exists and mode == "skip":
        return {"created": 0, "updated": 0, "skipped": 1}
    base = local if isinstance(local, dict) else {}
    if mode == "merge":
        merged = {**base, **payload}
    else:
        merged = dict(payload)
    # Preserve local provider api keys (never overwrite a real key with a blank).
    local_providers = base.get("providers") if isinstance(base.get("providers"), dict) else {}
    in_providers = merged.get("providers") if isinstance(merged.get("providers"), dict) else {}
    for name, prov in in_providers.items():
        if isinstance(prov, dict) and not prov.get("api_key"):
            lp = local_providers.get(name)
            if isinstance(lp, dict) and lp.get("api_key"):
                prov["api_key"] = lp["api_key"]
    _atomic_write_json(spec.filename, merged)
    return {"created": 0 if local_exists else 1, "updated": 1 if local_exists else 0, "skipped": 0}


# ------------------------------------------------------------------- db collect/restore
def _datetime_columns(model: Any) -> set[str]:
    return {c.name for c in model.__table__.columns if isinstance(c.type, DateTime)}


def _row_to_dict(row: Any, model: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in model.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        out[col.name] = val
    return out


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _collect_db(spec: DbSection, tenant_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(select(spec.model).where(spec.model.tenant_id == tenant_id))
    ).scalars().all()
    return [_row_to_dict(r, spec.model) for r in rows]


async def _count_db(spec: DbSection, tenant_id: str, db: AsyncSession) -> int:
    return int(
        (await db.execute(
            select(func.count(spec.model.id)).where(spec.model.tenant_id == tenant_id)
        )).scalar()
        or 0
    )


async def _restore_db(
    spec: DbSection, rows: list[dict[str, Any]], mode: str, tenant_id: str, db: AsyncSession
) -> dict[str, int]:
    """Upsert DB rows by primary key. Local-only rows are never deleted. ``skip`` only
    inserts new ids; ``overwrite``/``merge`` update existing rows too."""
    if not isinstance(rows, list):
        return {"created": 0, "updated": 0, "skipped": 0}
    dt_cols = _datetime_columns(spec.model)
    col_names = {c.name for c in spec.model.__table__.columns}
    created = updated = skipped = 0
    for raw in rows:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        # Force the row into the importing tenant so a backup restores under the
        # current tenant even if exported from a differently-named one.
        values = {k: v for k, v in raw.items() if k in col_names}
        values["tenant_id"] = tenant_id
        for k in dt_cols:
            if k in values:
                values[k] = _parse_dt(values[k])
        existing = await db.get(spec.model, raw["id"])
        if existing is not None:
            if mode == "skip":
                skipped += 1
                continue
            for k, v in values.items():
                if k == "id":
                    continue
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(spec.model(**values))
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------- public API
def list_sections_meta(tenant_id: str, db: AsyncSession | None = None) -> list[dict[str, Any]]:
    """Section catalog with file counts (DB counts added by :func:`list_sections`)."""
    meta: list[dict[str, Any]] = []
    for s in FILE_SECTIONS:
        meta.append(
            {
                "id": s.id,
                "label": s.label,
                "tier": s.tier,
                "kind": "collection" if s.collection_key else "document",
                "secret_bearing": bool(s.secret_kind),
                "count": _count_file(s),
            }
        )
    for s in DB_SECTIONS:
        meta.append(
            {"id": s.id, "label": s.label, "tier": s.tier, "kind": "db", "secret_bearing": False, "count": 0}
        )
    return meta


async def list_sections(tenant_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    """Full section catalog with counts (files + tenant-scoped DB rows)."""
    meta = list_sections_meta(tenant_id, db)
    by_id = {m["id"]: m for m in meta}
    for s in DB_SECTIONS:
        by_id[s.id]["count"] = await _count_db(s, tenant_id, db)
    return meta


async def build_backup(
    section_ids: list[str] | None, tenant_id: str, db: AsyncSession
) -> dict[str, Any]:
    """Assemble a backup manifest for the chosen sections (or all when None/empty)."""
    wanted = set(section_ids) if section_ids else set(ALL_SECTION_IDS)
    sections: dict[str, Any] = {}
    secrets_required: list[str] = []
    for s in FILE_SECTIONS:
        if s.id not in wanted:
            continue
        payload, secrets = _collect_file(s)
        if payload is None:
            continue
        sections[s.id] = {"kind": s.kind, "collection_key": s.collection_key, "data": payload}
        secrets_required.extend(secrets)
    for s in DB_SECTIONS:
        if s.id not in wanted:
            continue
        rows = await _collect_db(s, tenant_id, db)
        sections[s.id] = {"kind": s.kind, "data": rows}
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": _now_iso(),
        "meta": {
            "tenant_id": tenant_id,
            "sections": sorted(sections.keys()),
            "secrets_required": sorted(set(secrets_required)),
        },
        "sections": sections,
    }


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("Backup file is not a JSON object.")
    if manifest.get("format") != BACKUP_FORMAT:
        raise ValueError("Not an Azure Support Agent backup file.")
    if manifest.get("version") != BACKUP_VERSION:
        raise ValueError(
            f"Unsupported backup version {manifest.get('version')!r}; expected {BACKUP_VERSION}."
        )
    sections = manifest.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("Backup file has no sections.")
    return sections


def _section_payload(entry: Any) -> Any:
    return entry.get("data") if isinstance(entry, dict) else None


async def preview_import(
    manifest: Any, tenant_id: str, mode: str, db: AsyncSession
) -> dict[str, Any]:
    """Dry-run: report per-section create/update/skip counts. Writes nothing."""
    sections = _validate_manifest(manifest)
    mode = mode if mode in CONFLICT_MODES else "merge"
    items: list[dict[str, Any]] = []
    for sid, entry in sections.items():
        payload = _section_payload(entry)
        if sid in _FILE_BY_ID:
            spec = _FILE_BY_ID[sid]
            if spec.collection_key:
                incoming = _collection(payload, spec.collection_key)
                local = _collection(_read_json(spec.filename) or {}, spec.collection_key)
                create = sum(1 for cid in incoming if cid not in local)
                overlap = sum(1 for cid in incoming if cid in local)
                items.append(
                    {
                        "id": sid, "label": spec.label, "tier": spec.tier, "kind": "collection",
                        "incoming": len(incoming),
                        "create": create,
                        "update": 0 if mode == "skip" else overlap,
                        "skip": overlap if mode == "skip" else 0,
                    }
                )
            else:
                exists = _read_json(spec.filename) is not None
                items.append(
                    {
                        "id": sid, "label": spec.label, "tier": spec.tier, "kind": "document",
                        "incoming": 1,
                        "create": 0 if exists else 1,
                        "update": 1 if (exists and mode != "skip") else 0,
                        "skip": 1 if (exists and mode == "skip") else 0,
                    }
                )
        elif sid in _DB_BY_ID:
            spec = _DB_BY_ID[sid]
            rows = payload if isinstance(payload, list) else []
            create = update = skip = 0
            for raw in rows:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                exists = await db.get(spec.model, raw["id"]) is not None
                if not exists:
                    create += 1
                elif mode == "skip":
                    skip += 1
                else:
                    update += 1
            items.append(
                {
                    "id": sid, "label": spec.label, "tier": spec.tier, "kind": "db",
                    "incoming": len(rows), "create": create, "update": update, "skip": skip,
                }
            )
        else:
            items.append({"id": sid, "label": sid, "tier": "unknown", "kind": "unknown", "incoming": 0,
                          "create": 0, "update": 0, "skip": 0, "ignored": True})
    return {
        "mode": mode,
        "exported_at": manifest.get("exported_at"),
        "source_tenant": (manifest.get("meta") or {}).get("tenant_id"),
        "secrets_required": (manifest.get("meta") or {}).get("secrets_required") or [],
        "sections": items,
    }


async def apply_import(
    manifest: Any, tenant_id: str, mode: str, db: AsyncSession, section_ids: list[str] | None = None
) -> dict[str, Any]:
    """Apply a backup. File sections write atomically; DB sections upsert in the caller's
    transaction (commit is the caller's responsibility). Returns per-section counts."""
    sections = _validate_manifest(manifest)
    mode = mode if mode in CONFLICT_MODES else "merge"
    wanted = set(section_ids) if section_ids else None
    results: list[dict[str, Any]] = []
    for sid, entry in sections.items():
        if wanted is not None and sid not in wanted:
            continue
        payload = _section_payload(entry)
        if sid in _FILE_BY_ID:
            counts = _restore_file(_FILE_BY_ID[sid], payload, mode)
            results.append({"id": sid, **counts})
        elif sid in _DB_BY_ID:
            counts = await _restore_db(_DB_BY_ID[sid], payload if isinstance(payload, list) else [], mode, tenant_id, db)
            results.append({"id": sid, **counts})
    return {
        "mode": mode,
        "secrets_required": (manifest.get("meta") or {}).get("secrets_required") or [],
        "sections": results,
    }
