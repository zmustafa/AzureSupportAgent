"""Schedule math: compute the next run time for a scheduled task.

Supports three kinds:
- ``daily``  — every day at ``time_of_day`` in the task timezone
- ``weekly`` — on ``weekday`` (0=Mon..6=Sun) at ``time_of_day``
- ``cron``   — a standard 5-field cron expression (via croniter)

All computations are timezone-aware and return UTC datetimes so the scheduler and the
DB stay in UTC.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def _tz(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc


def _parse_hhmm(value: str | None) -> time:
    try:
        h, m = (value or "08:00").split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return time(8, 0)


def compute_next_run(task: dict, after: datetime | None = None) -> datetime | None:
    """Return the next UTC run time strictly after ``after`` (default: now).

    Returns None when the task can never run again (past its end date)."""
    now_utc = (after or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = _tz(task.get("timezone", "UTC"))
    kind = task.get("schedule_kind", "daily")

    # Respect the start date: never schedule before it.
    start = task.get("start_date")
    if isinstance(start, datetime):
        start_utc = start.astimezone(timezone.utc)
        if start_utc > now_utc:
            now_utc = start_utc - timedelta(seconds=1)

    candidate: datetime | None = None
    if kind == "cron":
        expr = task.get("cron_expr") or "0 8 * * *"
        try:
            base_local = now_utc.astimezone(tz)
            it = croniter(expr, base_local)
            candidate = it.get_next(datetime).astimezone(timezone.utc)
        except (ValueError, KeyError):
            return None
    else:
        tod = _parse_hhmm(task.get("time_of_day"))
        local_now = now_utc.astimezone(tz)
        # Start from today at the target time, then roll forward.
        day = local_now.replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
        if kind == "weekly":
            target_wd = int(task.get("weekday") or 0)
            # Advance to the next matching weekday at/after now.
            for _ in range(8):
                if day.weekday() == target_wd and day > local_now:
                    break
                day = day + timedelta(days=1)
                day = day.replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)
        else:  # daily
            if day <= local_now:
                day = day + timedelta(days=1)
        candidate = day.astimezone(timezone.utc)

    # Respect the end date.
    end = task.get("end_date")
    if isinstance(end, datetime) and candidate is not None:
        if candidate > end.astimezone(timezone.utc):
            return None
    return candidate


def human_schedule(task: dict) -> str:
    """A human-readable schedule label (e.g. 'Daily at 08:00 UTC')."""
    tz = task.get("timezone", "UTC")
    kind = task.get("schedule_kind", "daily")
    if kind == "cron":
        return f"Cron: {task.get('cron_expr', '')} ({tz})"
    tod = task.get("time_of_day", "08:00")
    if kind == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        wd = days[int(task.get("weekday") or 0) % 7]
        return f"Weekly on {wd} at {tod} ({tz})"
    return f"Daily at {tod} ({tz})"
