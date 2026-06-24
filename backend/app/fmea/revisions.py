"""FMEA document revision history (automatic snapshots).

Every save of an FMEA document (AI generation, manual edit, status change, restore) appends
a content snapshot here so a user can review history and restore an earlier version.
Content-deduplicated and capped per document, mirroring the Know-Me revisions registry.
Persisted under ``backend/.data/fmea_revisions.json``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "fmea_revisions.json"

_MAX_PER_DOC = 50
_CONTENT_KEYS = ("title", "scope_note", "tables", "status", "source", "ai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"revisions": {}})
    return data if isinstance(data, dict) else {"revisions": {}}


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


def signature(doc: dict[str, Any]) -> str:
    """A stable content fingerprint (excludes timestamps that always change)."""
    return json.dumps(
        {
            "title": doc.get("title", ""),
            "scope_note": doc.get("scope_note", ""),
            "tables": doc.get("tables", []),
            "status": doc.get("status", "draft"),
        },
        sort_keys=True,
    )


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    tables = rev.get("tables", []) or []
    row_count = sum(len(t.get("rows", []) or []) for t in tables)
    return {
        "id": rev["id"],
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "title": rev.get("title", ""),
        "status": rev.get("status", "draft"),
        "source": rev.get("source", "edited"),
        "table_count": len(tables),
        "row_count": row_count,
    }


def snapshot(fmea_id: str, doc: dict[str, Any], *, reason: str, actor: str) -> dict[str, Any] | None:
    if not fmea_id:
        return None
    data = _read()
    revs = data.setdefault("revisions", {}).setdefault(fmea_id, [])
    sig = signature(doc)
    if revs and revs[-1].get("sig") == sig:
        return None
    rev = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "sig": sig,
        **{k: doc.get(k) for k in _CONTENT_KEYS},
    }
    revs.append(rev)
    if len(revs) > _MAX_PER_DOC:
        del revs[: len(revs) - _MAX_PER_DOC]
    _write(data)
    return _meta(rev)


def list_revisions(fmea_id: str) -> list[dict[str, Any]]:
    revs = _read().get("revisions", {}).get(fmea_id, [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(fmea_id: str, revision_id: str) -> dict[str, Any] | None:
    for r in _read().get("revisions", {}).get(fmea_id, []):
        if r.get("id") == revision_id:
            return r
    return None


def delete_for(fmea_id: str) -> None:
    data = _read()
    if fmea_id in data.get("revisions", {}):
        del data["revisions"][fmea_id]
        _write(data)
