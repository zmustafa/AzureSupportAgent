"""Architecture Memory revision history (automatic snapshots).

Every change to a memory (manual edit, AI generation, enable toggle, restore) appends a
content snapshot here so a user can review history and restore an earlier version.
Snapshots are content-deduplicated (a no-op save won't create a new revision) and capped
per memory. Persisted under backend/.data/architecture_memory_revisions.json, consistent
with the architecture revisions registry.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_memory_revisions.json"

# Keep at most this many revisions per memory (oldest pruned first).
_MAX_PER_MEMORY = 50

# Fields copied into a snapshot (everything that defines a meaningful version).
_CONTENT_KEYS = ("title", "sections", "enabled_for_investigations", "source", "ai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"revisions": {}})
    return data if isinstance(data, dict) else {"revisions": {}}


def signature(memory: dict[str, Any]) -> str:
    """A stable content fingerprint (excludes timestamps that always change)."""
    return json.dumps(
        {
            "title": memory.get("title", ""),
            "sections": memory.get("sections", []),
            "enabled_for_investigations": memory.get("enabled_for_investigations", True),
        },
        sort_keys=True,
    )


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    """Lightweight metadata for list responses (no full section payload)."""
    sections = rev.get("sections", []) or []
    filled = sum(1 for s in sections if str(s.get("content") or "").strip())
    return {
        "id": rev["id"],
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "title": rev.get("title", ""),
        "source": rev.get("source", "manual"),
        "section_count": len(sections),
        "filled_count": filled,
    }


def snapshot(architecture_id: str, memory: dict[str, Any], *, reason: str, actor: str) -> dict[str, Any] | None:
    """Append a revision of ``memory``. Skips if identical to the most recent revision."""
    if not architecture_id:
        return None
    sig = signature(memory)
    rev = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "sig": sig,
        **{k: memory.get(k) for k in _CONTENT_KEYS},
    }
    added = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal added
        revs = data.setdefault("revisions", {}).setdefault(architecture_id, [])
        if revs and revs[-1].get("sig") == sig:
            return
        revs.append(rev)
        if len(revs) > _MAX_PER_MEMORY:
            del revs[: len(revs) - _MAX_PER_MEMORY]
        added = True

    jsonstore.mutate_json(_PATH, {"revisions": {}}, _mutate)
    return _meta(rev) if added else None


def list_revisions(architecture_id: str) -> list[dict[str, Any]]:
    """Revision metadata, newest first."""
    data = _read()
    revs = data.get("revisions", {}).get(architecture_id, [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(architecture_id: str, revision_id: str) -> dict[str, Any] | None:
    """Full revision content (for preview/restore), or None."""
    data = _read()
    for r in data.get("revisions", {}).get(architecture_id, []):
        if r.get("id") == revision_id:
            return r
    return None


def delete_for(architecture_id: str) -> None:
    """Drop all revisions for a memory (called when the memory is deleted)."""
    def _mutate(data: dict[str, Any]) -> None:
        data.get("revisions", {}).pop(architecture_id, None)

    jsonstore.mutate_json(_PATH, {"revisions": {}}, _mutate)
