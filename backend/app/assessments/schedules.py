"""Scheduled assessments registry (JSON).

A schedule says: run pillars P against workload W on cadence C, optionally alerting on new
findings. The in-process scheduler ticks, finds due schedules, runs them, and stores
``last_run_*``/``next_run_at``. Persisted under backend/.data/assessment_schedules.json."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "assessment_schedules.json"

DEFAULTS: dict[str, Any] = {
    "name": "",
    "workload_id": "",
    "workload_name": "",
    "connection_id": "",
    "tenant_id": "",
    "pillars": ["security", "reliability"],
    "use_ai": True,
    "enabled": True,
    # cadence (reuses app.automations.schedule shapes)
    "schedule_kind": "weekly",  # daily|weekly|cron
    "time_of_day": "08:00",
    "weekday": 0,
    "cron_expr": "",
    "timezone": "UTC",
    # alerting: emit a notification when the run completes / has new failures
    "alert_on_new_findings": True,
    "alert_min_severity": "warning",
    # runtime status
    "next_run_at": None,
    "last_run_at": None,
    "last_run_id": None,
    "last_score": None,
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"schedules": {}})
    return data if isinstance(data, dict) else {"schedules": {}}


def _merge(sid: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = sid
    return merged


def list_schedules() -> list[dict[str, Any]]:
    data = _read()
    out = [_merge(sid, s) for sid, s in data.get("schedules", {}).items()]
    out.sort(key=lambda s: s.get("name", "").lower())
    return out


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    data = _read()
    raw = data.get("schedules", {}).get(schedule_id)
    return _merge(schedule_id, raw) if raw is not None else None


def _cron_dict(s: dict[str, Any]) -> dict[str, Any]:
    """Shape a schedule for app.automations.schedule.compute_next_run."""
    return {
        "schedule_kind": s.get("schedule_kind", "weekly"),
        "time_of_day": s.get("time_of_day", "08:00"),
        "weekday": s.get("weekday", 0),
        "cron_expr": s.get("cron_expr", ""),
        "timezone": s.get("timezone", "UTC"),
    }


def upsert_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    from app.automations.schedule import compute_next_run

    sid = schedule.get("id") or str(uuid.uuid4())
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        schedules = data.setdefault("schedules", {})
        existing = schedules.get(sid, {})
        merged = dict(existing)
        for key in DEFAULTS:
            if key in schedule and schedule[key] is not None:
                merged[key] = schedule[key]
        merged["created_at"] = existing.get("created_at") or _now()
        merged["updated_at"] = _now()
        # Recompute next run from the cadence (when enabled).
        if merged.get("enabled"):
            nxt = compute_next_run(_cron_dict(merged))
            merged["next_run_at"] = nxt.isoformat() if nxt else None
        else:
            merged["next_run_at"] = None
        merged.pop("id", None)
        schedules[sid] = merged
        result.update(_merge(sid, merged))

    jsonstore.mutate_json(_PATH, {"schedules": {}}, _mutate)
    return result


def delete_schedule(schedule_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        if schedule_id in data.get("schedules", {}):
            del data["schedules"][schedule_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"schedules": {}}, _mutate)
    return deleted


def mark_ran(schedule_id: str, *, run_id: str, score: int | None) -> None:
    """Record a completed run and roll the next_run_at forward."""
    from app.automations.schedule import compute_next_run

    def _mutate(data: dict[str, Any]) -> None:
        s = data.get("schedules", {}).get(schedule_id)
        if s is None:
            return
        s["last_run_at"] = _now()
        s["last_run_id"] = run_id
        s["last_score"] = score
        if s.get("enabled"):
            nxt = compute_next_run(_cron_dict(_merge(schedule_id, s)))
            s["next_run_at"] = nxt.isoformat() if nxt else None

    jsonstore.mutate_json(_PATH, {"schedules": {}}, _mutate)


def due_schedules(now: datetime | None = None) -> list[dict[str, Any]]:
    """Enabled schedules whose next_run_at is in the past."""
    now = now or datetime.now(timezone.utc)
    out = []
    for s in list_schedules():
        if not s.get("enabled"):
            continue
        nxt = s.get("next_run_at")
        if not nxt:
            continue
        try:
            when = datetime.fromisoformat(nxt)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            out.append(s)
    return out
