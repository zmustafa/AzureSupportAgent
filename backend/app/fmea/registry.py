"""FMEA document registry (JSON, no secrets → no encryption).

An *FMEA* (Failure Mode and Effects Analysis) is a risk worksheet transformed from an
architecture's Memory. A workload (via its architecture) can have MULTIPLE FMEA documents —
e.g. a published baseline plus drafts — so records are keyed by their own ``id`` (not by
architecture). Each record links back to its source ``architecture_id`` / ``workload_id``
and holds MULTIPLE tables, each a list of scored rows.

Persisted under ``backend/.data/fmea.json`` via the cached ``jsonstore`` helper. Each save
auto-snapshots a revision; soft-delete (Trash) + restore + purge are supported. The Risk
Priority Number on every row is re-derived on every read/write (see ``compute``).

Record shape::

    { id, architecture_id, workload_id, workload_name, tenant_id, connection_id,
      title, scope_note, tables:[{id,name,scope_ref,rows:[{...}]}],
      status:'draft'|'in_review'|'published'|'archived', source:'ai'|'edited'|'hybrid',
      ai:{...}, created_by, created_at, updated_at, updated_by, deleted_at, deleted_by }
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore
from app.fmea import compute

_PATH = Path(__file__).resolve().parents[2] / ".data" / "fmea.json"

_STATUSES = ("draft", "in_review", "published", "archived")

# The qualitative (free-text) row columns + the three scored factors and their post-
# mitigation twins. RPN columns are derived, never stored authoritatively.
_TEXT_FIELDS = (
    "item", "function", "failure_mode", "effects", "causes",
    "control_prevention", "control_detection", "recommended_actions",
    "owner", "date_due", "action_results", "date_completed",
)
_FACTOR_FIELDS = (
    "severity", "occurrence", "detection",
    "severity_post", "occurrence_post", "detection_post",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"fmea": {}})
    if not isinstance(data, dict) or "fmea" not in data:
        return {"fmea": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


def _clean_row(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    row: dict[str, Any] = {"id": str(raw.get("id") or uuid.uuid4())}
    for f in _TEXT_FIELDS:
        row[f] = str(raw.get(f) or "")
    for f in _FACTOR_FIELDS:
        row[f] = compute.normalize_factor(raw.get(f))
    compute.recompute_row(row)
    return row


def _clean_tables(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in rows or []:
        if not isinstance(t, dict):
            continue
        out.append({
            "id": str(t.get("id") or uuid.uuid4()),
            "name": str(t.get("name") or "Untitled table"),
            "scope_ref": str(t.get("scope_ref") or ""),
            "rows": [_clean_row(r) for r in (t.get("rows") or [])],
        })
    return out


def get_fmea(fmea_id: str) -> dict[str, Any] | None:
    """Fetch one FMEA document by its id (RPNs recomputed), or None."""
    raw = _read().get("fmea", {}).get(fmea_id)
    if raw is None:
        return None
    return compute.recompute_doc(dict(raw))


def list_fmea(
    tenant_id: str | None = None, *, include_deleted: bool = False, only_deleted: bool = False,
    architecture_id: str | None = None, workload_id: str | None = None,
) -> list[dict[str, Any]]:
    out = [dict(m) for m in _read().get("fmea", {}).values()]
    if tenant_id is not None:
        out = [m for m in out if (m.get("tenant_id") or "") in ("", tenant_id)]
    if architecture_id is not None:
        out = [m for m in out if m.get("architecture_id") == architecture_id]
    if workload_id is not None:
        out = [m for m in out if m.get("workload_id") == workload_id]
    if only_deleted:
        out = [m for m in out if m.get("deleted_at")]
    elif not include_deleted:
        out = [m for m in out if not m.get("deleted_at")]
    out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return out


def create_fmea(
    *,
    architecture_id: str,
    workload_id: str = "",
    workload_name: str = "",
    connection_id: str = "",
    title: str = "",
    scope_note: str = "",
    tenant_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Create a NEW (draft) FMEA document for an architecture and return it."""
    data = _read()
    store = data.setdefault("fmea", {})
    fid = str(uuid.uuid4())
    rec: dict[str, Any] = {
        "id": fid,
        "architecture_id": architecture_id,
        "workload_id": workload_id,
        "workload_name": workload_name,
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "title": title or (f"FMEA — {workload_name}" if workload_name else "Failure Mode & Effects Analysis"),
        "scope_note": scope_note,
        "tables": [],
        "status": "draft",
        "source": "edited",
        "ai": {},
        "deleted_at": "",
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": actor,
        "updated_by": actor,
    }
    store[fid] = rec
    _write(data)
    return dict(rec)


def update_fmea(
    fmea_id: str,
    *,
    workload_id: str = "",
    workload_name: str = "",
    connection_id: str = "",
    title: str | None = None,
    scope_note: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    status: str | None = None,
    source: str | None = None,
    ai: dict[str, Any] | None = None,
    tenant_id: str = "",
    actor: str = "",
    reason: str = "Edited",
    create_if_missing: bool = False,
    architecture_id: str = "",
) -> dict[str, Any] | None:
    """Update an existing FMEA by id (read-modify-write) + auto-snapshot a revision.
    Returns the saved record, or None if it doesn't exist (and ``create_if_missing`` is off)."""
    data = _read()
    store = data.setdefault("fmea", {})
    existing = store.get(fmea_id)
    if existing is None:
        if not create_if_missing:
            return None
        existing = {}
    merged: dict[str, Any] = dict(existing)
    merged["id"] = fmea_id
    if architecture_id or "architecture_id" not in merged:
        merged["architecture_id"] = architecture_id or merged.get("architecture_id", "")
    if workload_id or "workload_id" not in merged:
        merged["workload_id"] = workload_id or merged.get("workload_id", "")
    if workload_name or "workload_name" not in merged:
        merged["workload_name"] = workload_name or merged.get("workload_name", "")
    if connection_id or "connection_id" not in merged:
        merged["connection_id"] = connection_id or merged.get("connection_id", "")
    if tenant_id or "tenant_id" not in merged:
        merged["tenant_id"] = tenant_id or merged.get("tenant_id", "")
    if title is not None:
        merged["title"] = title
    elif "title" not in merged:
        merged["title"] = ""
    if scope_note is not None:
        merged["scope_note"] = scope_note
    elif "scope_note" not in merged:
        merged["scope_note"] = ""
    if tables is not None:
        merged["tables"] = _clean_tables(tables)
    elif "tables" not in merged:
        merged["tables"] = []
    if status is not None and status in _STATUSES:
        merged["status"] = status
    elif "status" not in merged:
        merged["status"] = "draft"
    if source is not None:
        merged["source"] = source
    elif "source" not in merged:
        merged["source"] = "edited"
    if ai is not None:
        merged["ai"] = ai
    elif "ai" not in merged:
        merged["ai"] = {}
    merged.setdefault("deleted_at", "")
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    if actor:
        merged["updated_by"] = actor
        if not existing:
            merged.setdefault("created_by", actor)
    compute.recompute_doc(merged)
    store[fmea_id] = merged
    _write(data)
    from app.fmea import revisions

    snap_reason = ("Created" if reason == "Edited" else reason) if not existing else reason
    revisions.snapshot(fmea_id, merged, reason=snap_reason, actor=actor)
    return dict(merged)


def soft_delete(fmea_id: str, actor: str = "") -> bool:
    data = _read()
    rec = data.get("fmea", {}).get(fmea_id)
    if not rec or rec.get("deleted_at"):
        return False
    rec["deleted_at"] = _now()
    rec["updated_at"] = _now()
    if actor:
        rec["deleted_by"] = actor
    _write(data)
    return True


def restore(fmea_id: str) -> dict[str, Any] | None:
    data = _read()
    rec = data.get("fmea", {}).get(fmea_id)
    if not rec or not rec.get("deleted_at"):
        return None
    rec["deleted_at"] = ""
    rec["updated_at"] = _now()
    _write(data)
    return compute.recompute_doc(dict(rec))


def purge(fmea_id: str) -> bool:
    """Permanently delete an FMEA + its revisions."""
    data = _read()
    store = data.get("fmea", {})
    if fmea_id not in store:
        return False
    del store[fmea_id]
    _write(data)
    from app.fmea import revisions

    revisions.delete_for(fmea_id)
    return True


def empty_trash(tenant_id: str | None = None) -> int:
    """Permanently delete all soft-deleted FMEA documents (optionally tenant-scoped)."""
    purged = 0
    for rec in list_fmea(tenant_id, only_deleted=True):
        if purge(rec["id"]):
            purged += 1
    return purged


def restore_revision(fmea_id: str, revision_id: str, actor: str = "") -> dict[str, Any] | None:
    from app.fmea import revisions

    if get_fmea(fmea_id) is None:
        return None
    rev = revisions.get_revision(fmea_id, revision_id)
    if rev is None:
        return None
    return update_fmea(
        fmea_id,
        title=rev.get("title", ""),
        scope_note=rev.get("scope_note", ""),
        tables=rev.get("tables", []),
        status=rev.get("status", "draft"),
        source=rev.get("source", "edited"),
        ai=rev.get("ai", {}),
        actor=actor,
        reason="Restored from history",
    )


def prune_orphans(valid_architecture_ids: set[str]) -> int:
    """Drop FMEA records whose architecture no longer exists (cascades revisions).

    Prune-guard: callers must pass the REAL architecture set. (The endpoint that calls this
    skips the prune entirely when that set is empty, so a test run can't wipe live data.)
    """
    data = _read()
    store = data.get("fmea", {})
    orphans = [fid for fid, rec in store.items() if (rec or {}).get("architecture_id") not in valid_architecture_ids]
    if not orphans:
        return 0
    for fid in orphans:
        del store[fid]
    _write(data)
    from app.fmea import revisions

    for fid in orphans:
        revisions.delete_for(fid)
    return len(orphans)
