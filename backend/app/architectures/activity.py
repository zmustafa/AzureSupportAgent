"""Per-architecture management activity log (audit trail).

Records discrete management events for each architecture — created, renamed, diagram
edited, status changed, category/solution changed, AI generated/enhanced, cloned, and
restored — with the actor, timestamp, a human-readable detail string, and structured
before/after metadata. This is an append-only audit log (never deduped), distinct from
``revisions.py`` which stores restorable content snapshots. Persisted under
backend/.data/architecture_activity.json, consistent with the other JSON registries.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    return {"activity": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    data = _read()
    events = data.setdefault("activity", {}).setdefault(architecture_id, [])
    entry = {
        "id": str(uuid.uuid4()),
        "at": _now(),
        "by": actor or "",
        "event": event,
        "detail": detail,
        "meta": meta or {},
    }
    events.append(entry)
    if len(events) > _MAX_PER_ARCH:
        del events[: len(events) - _MAX_PER_ARCH]
    _write(data)
    return entry


def list_activity(architecture_id: str) -> list[dict[str, Any]]:
    """All management events for an architecture, newest first."""
    data = _read()
    events = data.get("activity", {}).get(architecture_id, [])
    return list(reversed(events))


def delete_for(architecture_id: str) -> None:
    """Drop the activity log for an architecture (called when it is deleted)."""
    data = _read()
    if architecture_id in data.get("activity", {}):
        del data["activity"][architecture_id]
        _write(data)
