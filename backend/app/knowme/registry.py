"""Workload Know-Me registry (JSON, no secrets → no encryption).

A *Know-Me* is a support-facing reference transformed from an architecture's Memory. A
workload (via its architecture) can have MULTIPLE Know-Me documents — e.g. a published
reference plus one or more drafts — so records are keyed by their own ``id`` (not by
architecture). Each record still links back to its source ``architecture_id`` /
``workload_id``. Persisted under ``backend/.data/know_me.json`` via the cached ``jsonstore``
helper (mtime-validated reads + atomic write-through writes). Each save auto-snapshots a
revision; soft-delete (Trash) + restore + purge are supported.

Record shape::

    { id, architecture_id, workload_id, workload_name, tenant_id, connection_id,
      title, sections:[{key,label,content}], todos:[{...}], assets:[...],
      status:'draft'|'in_review'|'published', source:'ai'|'edited'|'hybrid',
      ai:{...}, created_by, created_at, updated_at, deleted_at }
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore
from app.knowme import sections as km

_PATH = Path(__file__).resolve().parents[2] / ".data" / "know_me.json"

_STATUSES = ("draft", "in_review", "published", "archived")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"know_me": {}})
    if not isinstance(data, dict):
        return {"know_me": {}}
    return _migrate(data)


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """One-time migration from the old architecture-keyed layout ({architecture_id: rec})
    to the id-keyed layout ({km_id: rec}). Records already carry a unique ``id``; if every
    top-level key already equals its record's ``id`` there is nothing to do. Re-keys the
    revision store + asset folders to match."""
    store = data.get("know_me")
    if not isinstance(store, dict) or not store:
        return data
    needs = any(k != (v or {}).get("id") for k, v in store.items() if isinstance(v, dict))
    if not needs:
        return data
    remap: dict[str, str] = {}  # old key (architecture_id) -> km_id
    new_store: dict[str, Any] = {}
    for old_key, rec in store.items():
        if not isinstance(rec, dict):
            continue
        kid = rec.get("id") or str(uuid.uuid4())
        rec["id"] = kid
        if not rec.get("architecture_id"):
            rec["architecture_id"] = old_key
        new_store[kid] = rec
        if old_key != kid:
            remap[old_key] = kid
    data["know_me"] = new_store
    jsonstore.write_json(_PATH, data)
    if remap:
        try:
            from app.knowme import assets as kassets
            from app.knowme import revisions

            revisions.remap_keys(remap)
            kassets.remap_dirs(remap)
        except Exception:  # noqa: BLE001 — best-effort; the records are already re-keyed
            pass
    return data


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


def _clean_sections(rows: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for s in rows or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("key") or "").strip()
        if not key:
            continue
        cleaned.append({
            "key": key,
            "label": str(s.get("label") or km.section_label(key)),
            "content": str(s.get("content") or ""),
        })
    return cleaned


def _clean_todos(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in rows or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        status = str(t.get("status") or "open")
        ftype = str(t.get("type") or "text")
        source = str(t.get("source") or "human")
        sugg = t.get("suggestions")
        suggestions = [str(s) for s in sugg if str(s).strip()] if isinstance(sugg, list) else []
        ch = t.get("choices")
        choices = [str(c) for c in ch if str(c).strip()][:12] if isinstance(ch, list) else []
        choice_source = str(t.get("choice_source") or "")
        conf = t.get("confidence")
        try:
            confidence = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            confidence = None
        out.append({
            "id": str(t["id"]),
            "field_key": str(t.get("field_key") or ""),
            "label": str(t.get("label") or ""),
            "section_key": str(t.get("section_key") or ""),
            "status": status if status in ("open", "done") else "open",
            "value": str(t.get("value") or ""),
            "type": ftype if ftype in km.FIELD_TYPES else "text",
            "required": bool(t.get("required", False)),
            "group": str(t.get("group") or "other"),
            "suggestions": suggestions,
            "source": source if source in ("human", "auto", "suggested") else "human",
            "confidence": confidence,
            "assignee": str(t.get("assignee") or ""),
            "note": str(t.get("note") or ""),
            "choices": choices,
            "allow_custom": bool(t.get("allow_custom", True)),
            "choice_source": choice_source if choice_source in ("platform", "rule", "ai") else "",
            "multi": bool(t.get("multi", False)),
        })
    return out


def get_know_me(km_id: str) -> dict[str, Any] | None:
    """Fetch one Know-Me by its id (or None)."""
    raw = _read().get("know_me", {}).get(km_id)
    return dict(raw) if raw is not None else None


def list_know_me(
    tenant_id: str | None = None, *, include_deleted: bool = False, only_deleted: bool = False,
    architecture_id: str | None = None, workload_id: str | None = None,
) -> list[dict[str, Any]]:
    out = [dict(m) for m in _read().get("know_me", {}).values()]
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


def create_know_me(
    *,
    architecture_id: str,
    workload_id: str = "",
    workload_name: str = "",
    connection_id: str = "",
    title: str = "",
    description: str = "",
    tenant_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Create a NEW empty (draft) Know-Me for an architecture and return it."""
    data = _read()
    store = data.setdefault("know_me", {})
    kid = str(uuid.uuid4())
    rec: dict[str, Any] = {
        "id": kid,
        "architecture_id": architecture_id,
        "workload_id": workload_id,
        "workload_name": workload_name,
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "title": title,
        "description": description,
        "sections": km.default_sections(),
        "todos": [],
        "assets": [],
        "status": "draft",
        "is_reference": False,
        "source": "edited",
        "ai": {},
        "deleted_at": "",
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": actor,
        "updated_by": actor,
    }
    store[kid] = rec
    _write(data)
    return dict(rec)


def add_asset(km_id: str, asset: dict[str, Any]) -> dict[str, Any] | None:
    data = _read()
    rec = data.get("know_me", {}).get(km_id)
    if not rec:
        return None
    rec.setdefault("assets", []).append(asset)
    rec["updated_at"] = _now()
    _write(data)
    return asset


def remove_asset(km_id: str, asset_id: str) -> bool:
    data = _read()
    rec = data.get("know_me", {}).get(km_id)
    if not rec:
        return False
    before = rec.get("assets", []) or []
    after = [a for a in before if a.get("id") != asset_id]
    if len(after) == len(before):
        return False
    rec["assets"] = after
    rec["updated_at"] = _now()
    _write(data)
    return True


def update_know_me(
    km_id: str,
    *,
    workload_id: str = "",
    workload_name: str = "",
    connection_id: str = "",
    title: str | None = None,
    description: str | None = None,
    sections: list[dict[str, Any]] | None = None,
    todos: list[dict[str, Any]] | None = None,
    status: str | None = None,
    source: str | None = None,
    ai: dict[str, Any] | None = None,
    tenant_id: str = "",
    actor: str = "",
    reason: str = "Edited",
    create_if_missing: bool = False,
    architecture_id: str = "",
) -> dict[str, Any] | None:
    """Update an existing Know-Me by id (read-modify-write) + auto-snapshot a revision.
    Returns the saved record, or None if it doesn't exist (and ``create_if_missing`` is off)."""
    data = _read()
    store = data.setdefault("know_me", {})
    existing = store.get(km_id)
    if existing is None:
        if not create_if_missing:
            return None
        existing = {}
    merged: dict[str, Any] = dict(existing)
    merged["id"] = km_id
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
    if description is not None:
        merged["description"] = description
    elif "description" not in merged:
        merged["description"] = ""
    if sections is not None:
        merged["sections"] = _clean_sections(sections)
    elif "sections" not in merged:
        merged["sections"] = km.default_sections()
    if todos is not None:
        merged["todos"] = _clean_todos(todos)
    elif "todos" not in merged:
        merged["todos"] = []
    if "assets" not in merged:
        merged["assets"] = []
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
    merged.setdefault("is_reference", bool(existing.get("is_reference", False)))
    merged.setdefault("deleted_at", "")
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    if actor:
        merged["updated_by"] = actor
        if not existing:
            merged.setdefault("created_by", actor)
    store[km_id] = merged
    _write(data)
    from app.knowme import revisions

    snap_reason = ("Created" if reason == "Edited" else reason) if not existing else reason
    revisions.snapshot(km_id, merged, reason=snap_reason, actor=actor)
    return dict(merged)


def set_reference(km_id: str, *, is_reference: bool = True, actor: str = "") -> dict[str, Any] | None:
    """Mark this Know-Me as the canonical *reference* for its workload. Only one document per
    workload can be the reference, so setting it clears the flag on its siblings. Pass
    ``is_reference=False`` to simply unset it. Returns the updated record (or None)."""
    data = _read()
    store = data.get("know_me", {})
    rec = store.get(km_id)
    if not rec or rec.get("deleted_at"):
        return None
    if is_reference:
        wid = rec.get("workload_id") or ""
        aid = rec.get("architecture_id") or ""
        for other in store.values():
            if other is rec or not isinstance(other, dict):
                continue
            same = (wid and other.get("workload_id") == wid) or (not wid and other.get("architecture_id") == aid)
            if same and other.get("is_reference"):
                other["is_reference"] = False
    rec["is_reference"] = bool(is_reference)
    rec["updated_at"] = _now()
    if actor:
        rec["updated_by"] = actor
    _write(data)
    return dict(rec)


def merge_ai_sections(
    existing_sections: list[dict[str, Any]] | None, ai_sections: dict[str, str]
) -> list[dict[str, str]]:
    """Overwrite each section the model returned non-empty content for; keep the rest.
    Always returns the full catalog order (every Know-Me section is present)."""
    by_key = {s.get("key"): dict(s) for s in (existing_sections or km.default_sections())}
    for key, content in (ai_sections or {}).items():
        if key and str(content or "").strip():
            by_key[key] = {"key": key, "label": km.section_label(key), "content": str(content)}
    ordered: list[dict[str, str]] = []
    for k in km.SECTION_KEYS:
        ordered.append(by_key.pop(k, {"key": k, "label": km.section_label(k), "content": ""}))
    ordered.extend(by_key.values())
    return ordered


def soft_delete(km_id: str, actor: str = "") -> bool:
    data = _read()
    rec = data.get("know_me", {}).get(km_id)
    if not rec or rec.get("deleted_at"):
        return False
    rec["deleted_at"] = _now()
    rec["updated_at"] = _now()
    if actor:
        rec["deleted_by"] = actor
    _write(data)
    return True


def restore(km_id: str) -> dict[str, Any] | None:
    data = _read()
    rec = data.get("know_me", {}).get(km_id)
    if not rec or not rec.get("deleted_at"):
        return None
    rec["deleted_at"] = ""
    rec["updated_at"] = _now()
    _write(data)
    return dict(rec)


def purge(km_id: str) -> bool:
    """Permanently delete a Know-Me + its revisions + assets."""
    data = _read()
    store = data.get("know_me", {})
    if km_id not in store:
        return False
    del store[km_id]
    _write(data)
    from app.knowme import assets as kassets
    from app.knowme import revisions

    revisions.delete_for(km_id)
    kassets.delete_all(km_id)
    return True


def empty_trash(tenant_id: str | None = None) -> int:
    """Permanently delete all soft-deleted Know-Me's (optionally tenant-scoped)."""
    purged = 0
    for rec in list_know_me(tenant_id, only_deleted=True):
        if purge(rec["id"]):
            purged += 1
    return purged


def restore_revision(km_id: str, revision_id: str, actor: str = "") -> dict[str, Any] | None:
    from app.knowme import revisions

    if get_know_me(km_id) is None:
        return None
    rev = revisions.get_revision(km_id, revision_id)
    if rev is None:
        return None
    return update_know_me(
        km_id,
        title=rev.get("title", ""),
        sections=rev.get("sections", []),
        todos=rev.get("todos", []),
        status=rev.get("status", "draft"),
        source=rev.get("source", "edited"),
        ai=rev.get("ai", {}),
        actor=actor,
        reason="Restored from history",
    )


def prune_orphans(valid_architecture_ids: set[str]) -> int:
    """Drop Know-Me records whose architecture no longer exists (cascades revisions+assets)."""
    data = _read()
    store = data.get("know_me", {})
    orphans = [kid for kid, rec in store.items() if (rec or {}).get("architecture_id") not in valid_architecture_ids]
    if not orphans:
        return 0
    for kid in orphans:
        del store[kid]
    _write(data)
    from app.knowme import assets as kassets
    from app.knowme import revisions

    for kid in orphans:
        revisions.delete_for(kid)
        kassets.delete_all(kid)
    return len(orphans)
