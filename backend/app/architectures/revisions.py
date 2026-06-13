"""Architecture revision history (automatic snapshots).

Every change to an architecture (manual edit, AI enhance, state/category change, clone,
restore) appends a content snapshot here so a user can review history and restore an
earlier version. Snapshots are content-deduplicated (a no-op save won't create a new
revision) and capped per architecture. Persisted under
backend/.data/architecture_revisions.json, consistent with the other JSON registries.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_revisions.json"

# Keep at most this many revisions per architecture (oldest pruned first).
_MAX_PER_ARCH = 50

# Fields that define a meaningful version (used for the dedup signature + restore).
_CONTENT_KEYS = ("name", "description", "source", "state", "category_id", "nodes", "edges", "groups", "ai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"revisions": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def signature(arch: dict[str, Any]) -> str:
    """A stable content fingerprint (excludes timestamps/ai noise that always changes)."""
    return json.dumps(
        {
            "name": arch.get("name", ""),
            "description": arch.get("description", ""),
            "state": arch.get("state", ""),
            "category_id": arch.get("category_id", ""),
            "nodes": arch.get("nodes", []),
            "edges": arch.get("edges", []),
            "groups": arch.get("groups", []),
        },
        sort_keys=True,
    )


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    """Lightweight metadata for list responses (no heavy nodes/edges payload)."""
    return {
        "id": rev["id"],
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "state": rev.get("state", "draft"),
        "category_id": rev.get("category_id", ""),
        "node_count": len(rev.get("nodes", []) or []),
        "edge_count": len(rev.get("edges", []) or []),
    }


def snapshot(architecture_id: str, arch: dict[str, Any], *, reason: str, actor: str) -> dict[str, Any] | None:
    """Append a revision of ``arch``. Skips if identical to the most recent revision."""
    if not architecture_id:
        return None
    data = _read()
    revs = data.setdefault("revisions", {}).setdefault(architecture_id, [])
    sig = signature(arch)
    if revs and revs[-1].get("sig") == sig:
        return None  # dedup: nothing meaningful changed
    rev = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "sig": sig,
        **{k: arch.get(k) for k in _CONTENT_KEYS},
    }
    revs.append(rev)
    if len(revs) > _MAX_PER_ARCH:
        del revs[: len(revs) - _MAX_PER_ARCH]
    _write(data)
    return _meta(rev)


def list_revisions(architecture_id: str) -> list[dict[str, Any]]:
    """Revision metadata, newest first."""
    data = _read()
    revs = data.get("revisions", {}).get(architecture_id, [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(architecture_id: str, revision_id: str) -> dict[str, Any] | None:
    """Full revision content (for restore), or None."""
    data = _read()
    for r in data.get("revisions", {}).get(architecture_id, []):
        if r.get("id") == revision_id:
            return r
    return None


def delete_for(architecture_id: str) -> None:
    """Drop all revisions for an architecture (called when it is deleted)."""
    data = _read()
    if architecture_id in data.get("revisions", {}):
        del data["revisions"][architecture_id]
        _write(data)
