"""Per-architecture management activity log (audit trail).

Records discrete management events for each architecture — created, renamed, diagram
edited, status changed, category/solution changed, AI generated/enhanced, cloned, and
restored — with the actor, timestamp, a human-readable detail string, and structured
before/after metadata. This is an append-only audit log (never deduped), distinct from
``revisions.py`` which stores restorable content snapshots. Persisted under
backend/.data/architecture_activity.json, consistent with the other JSON registries.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_activity.json"

# Keep at most this many events per architecture (oldest pruned first).
_MAX_PER_ARCH = 200

# Known event kinds (the UI maps these to icons; unknown kinds still render).
CREATED = "created"
RENAMED = "renamed"
EDITED = "edited"
STATE_CHANGED = "state_changed"
CATEGORY_CHANGED = "category_changed"
WORKLOAD_CHANGED = "workload_changed"
AI_GENERATED = "ai_generated"
AI_ENHANCED = "ai_enhanced"
CLONED = "cloned"
CLONED_TO = "cloned_to"
RESTORED = "restored"
TRASHED = "trashed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"activity": {}})
    return data if isinstance(data, dict) else {"activity": {}}


def log(
    architecture_id: str,
    event: str,
    detail: str,
    actor: str,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one management event to an architecture's activity log."""
    if not architecture_id:
        return None
    entry = {
        "id": str(uuid.uuid4()),
        "at": _now(),
        "by": actor or "",
        "event": event,
        "detail": detail,
        "meta": meta or {},
    }

    def _mutate(data: dict[str, Any]) -> None:
        events = data.setdefault("activity", {}).setdefault(architecture_id, [])
        events.append(entry)
        if len(events) > _MAX_PER_ARCH:
            del events[: len(events) - _MAX_PER_ARCH]

    jsonstore.mutate_json(_PATH, {"activity": {}}, _mutate)
    return entry


def list_activity(architecture_id: str) -> list[dict[str, Any]]:
    """All management events for an architecture, newest first."""
    data = _read()
    events = data.get("activity", {}).get(architecture_id, [])
    return list(reversed(events))


def delete_for(architecture_id: str) -> None:
    """Drop the activity log for an architecture (called when it is deleted)."""
    def _mutate(data: dict[str, Any]) -> None:
        data.get("activity", {}).pop(architecture_id, None)

    jsonstore.mutate_json(_PATH, {"activity": {}}, _mutate)
