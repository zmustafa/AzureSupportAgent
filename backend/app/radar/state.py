"""Per-event Radar status registry.

The Reliability-finding state machine (New → Migration planned → Done/Waived) covers the
assessment-scoring side, but the Radar's own table needs lightweight, tenant-scoped status
+ assignment + waiver-reason state keyed by tracking ID, persisted on the Azure Files
volume (``backend/.data/radar_state.json``)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "radar_state.json"

STATUSES = ("new", "acknowledged", "migration_planned", "done", "waived")


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
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_states(tenant_id: str) -> dict[str, dict[str, Any]]:
    return _read().get(tenant_id or "default", {})


def set_state(
    tenant_id: str,
    tracking_id: str,
    *,
    status: str | None = None,
    assignee: str | None = None,
    waive_reason: str | None = None,
    actor: str = "",
) -> dict[str, Any]:
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    entry = bucket.setdefault(tracking_id, {"status": "new", "history": []})
    if status and status in STATUSES:
        entry["status"] = status
    if assignee is not None:
        entry["assignee"] = assignee
    if waive_reason is not None:
        entry["waive_reason"] = waive_reason
    entry["updated_at"] = _now()
    entry["updated_by"] = actor
    entry.setdefault("history", []).append(
        {"at": _now(), "by": actor, "status": entry["status"], "assignee": entry.get("assignee", ""), "waive_reason": entry.get("waive_reason", "")}
    )
    entry["history"] = entry["history"][-25:]
    _write(data)
    return entry


def apply_states(tenant_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay stored status/assignee/waiver onto a freshly-computed event list."""
    states = get_states(tenant_id)
    for e in events:
        st = states.get(e.get("tracking_id") or e.get("id", ""))
        if st:
            e["status"] = st.get("status", "new")
            e["assignee"] = st.get("assignee", "")
            e["waive_reason"] = st.get("waive_reason", "")
        else:
            e.setdefault("status", "new")
            e.setdefault("assignee", "")
            e.setdefault("waive_reason", "")
    return events
