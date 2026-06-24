"""Workload Know-Me revision history (automatic snapshots).

Every save of a Know-Me (AI generation, manual edit, TODO fill, status change, restore)
appends a content snapshot here so a user can review history and restore an earlier
version. Content-deduplicated and capped per document, mirroring the architecture-memory
revisions registry. Persisted under ``backend/.data/know_me_revisions.json``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "know_me_revisions.json"

_MAX_PER_DOC = 50
_CONTENT_KEYS = ("title", "sections", "todos", "status", "source", "ai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"revisions": {}})
    return data if isinstance(data, dict) else {"revisions": {}}


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


def signature(km_doc: dict[str, Any]) -> str:
    """A stable content fingerprint (excludes timestamps that always change)."""
    return json.dumps(
        {
            "title": km_doc.get("title", ""),
            "sections": km_doc.get("sections", []),
            "todos": km_doc.get("todos", []),
            "status": km_doc.get("status", "draft"),
        },
        sort_keys=True,
    )


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    sections = rev.get("sections", []) or []
    todos = rev.get("todos", []) or []
    return {
        "id": rev["id"],
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "title": rev.get("title", ""),
        "status": rev.get("status", "draft"),
        "source": rev.get("source", "edited"),
        "section_count": len(sections),
        "filled_count": sum(1 for s in sections if str(s.get("content") or "").strip()),
        "open_todos": sum(1 for t in todos if t.get("status") != "done"),
    }


def snapshot(architecture_id: str, km_doc: dict[str, Any], *, reason: str, actor: str) -> dict[str, Any] | None:
    if not architecture_id:
        return None
    data = _read()
    revs = data.setdefault("revisions", {}).setdefault(architecture_id, [])
    sig = signature(km_doc)
    if revs and revs[-1].get("sig") == sig:
        return None
    rev = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "sig": sig,
        **{k: km_doc.get(k) for k in _CONTENT_KEYS},
    }
    revs.append(rev)
    if len(revs) > _MAX_PER_DOC:
        del revs[: len(revs) - _MAX_PER_DOC]
    _write(data)
    return _meta(rev)


def list_revisions(architecture_id: str) -> list[dict[str, Any]]:
    revs = _read().get("revisions", {}).get(architecture_id, [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(architecture_id: str, revision_id: str) -> dict[str, Any] | None:
    for r in _read().get("revisions", {}).get(architecture_id, []):
        if r.get("id") == revision_id:
            return r
    return None


def delete_for(architecture_id: str) -> None:
    data = _read()
    if architecture_id in data.get("revisions", {}):
        del data["revisions"][architecture_id]
        _write(data)


def remap_keys(remap: dict[str, str]) -> None:
    """Re-key the revision store from old keys to new (used by the registry migration)."""
    if not remap:
        return
    data = _read()
    revs = data.get("revisions", {})
    changed = False
    for old, new in remap.items():
        if old in revs and old != new:
            revs[new] = revs.pop(old)
            changed = True
    if changed:
        _write(data)
