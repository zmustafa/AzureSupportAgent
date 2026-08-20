"""Derived RPO — how much data a failure costs, computed from configuration.

This is the defensible half of the module. RPO is *computed*, not estimated: a policy that
runs daily has a 24-hour worst case whatever anyone hoped, and a replication link reports
its own lag. Contrast :mod:`app.resiliency.rto`, which infers.

Three rules are load-bearing:

**Worst case, never average.** A daily backup at 02:00 means that at 01:59 you are 23 hours
59 minutes from your last recovery point. The RPO is 24 hours, not 12. Averaging halves the
stated exposure, and the business signs off on the number we print.

**Configured and observed are different facts.** The policy says what the design permits;
``recovery_point_age_hours`` says what reality is currently delivering. A daily policy whose
job has failed for six days is configured 24h and observed 144h. When they disagree reality
wins, and the gap is itself a finding — reporting only the configured value is how a
dashboard stays green through a week of failed backups.

**Redundancy is not a recovery point.** GRS answers region loss and says nothing about
corruption. :func:`app.resiliency.model.redundancy_helps` gates that, here as everywhere.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.resiliency import model
from app.resiliency.model import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    Evidence,
)

_MINUTES_PER_DAY = 1440
_MINUTES_PER_WEEK = 10_080

_DAY_ORDER = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

# ISO-8601 recurrence as Data Protection writes it: "R/2024-01-01T02:00:00+00:00/P1D".
_ISO_DURATION = re.compile(
    r"P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def _as_json(value: Any) -> Any:
    """Resource Graph hands back ``properties`` as dynamic OR as a string depending on the
    query, so every reader has to cope with both."""
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _clock_minutes(stamp: str) -> int | None:
    """Minutes past midnight from an ISO timestamp, ignoring the date."""
    text = str(stamp or "")
    if len(text) < 16 or text[10] not in ("T", " "):
        return None
    try:
        return int(text[11:13]) * 60 + int(text[14:16])
    except ValueError:
        return None


def parse_iso_duration(text: str) -> int | None:
    """ISO-8601 duration to whole minutes. Months and years are refused, not guessed."""
    raw = str(text or "").strip().upper()
    if not raw.startswith("P") or "M" in raw.split("T")[0][1:] or "Y" in raw:
        # A month is not a fixed number of minutes; refusing beats inventing 30 days.
        if not raw.startswith("P"):
            return None
    match = _ISO_DURATION.match(raw)
    if not match:
        return None
    parts = match.groupdict()
    minutes = 0
    minutes += int(parts["weeks"] or 0) * _MINUTES_PER_WEEK
    minutes += int(parts["days"] or 0) * _MINUTES_PER_DAY
    minutes += int(parts["hours"] or 0) * 60
    minutes += int(parts["minutes"] or 0)
    minutes += int(float(parts["seconds"] or 0) // 60)
    return minutes or None


def _worst_gap(points: list[int], period: int) -> int:
    """Largest gap between successive run times on a cyclic timeline.

    Two runs a day is a 12-hour worst case only if they are 12 hours apart. 02:00 and 03:00
    is a 23-hour worst case, and a naive ``period // len(points)`` reports 12."""
    if not points:
        return period
    ordered = sorted(set(p % period for p in points))
    if len(ordered) == 1:
        return period
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    gaps.append(period - ordered[-1] + ordered[0])
    return max(gaps)


def parse_schedule_interval(schedule_raw: Any) -> tuple[int | None, str]:
    """Worst-case interval in minutes for a backup schedule, plus a human summary.

    Returns ``(None, "")`` for a shape we do not recognize. That is ``unknown`` — never a
    24-hour default, because a wrong default here is invisible and understates exposure.

    **The window trap.** An hourly policy carries ``scheduleWindowStartTime`` and
    ``scheduleWindowDuration``. "Every 4 hours, 08:00-18:00" is *not* a 4-hour RPO: the
    worst gap is the 14 hours overnight when nothing runs. An implementation that reads only
    ``interval`` understates it threefold and passes every test written with a 24-hour
    window. ``_rsv_schedule_summary`` in Backup Manager renders exactly that way, which is
    why this function exists rather than reusing it.
    """
    policy = _as_json(schedule_raw)
    if not isinstance(policy, dict):
        return None, ""

    # --- Data Protection: ISO-8601 recurrences ---------------------------------------
    windows = policy.get("repeatingTimeIntervals")
    if isinstance(windows, list) and windows:
        intervals = []
        for item in windows:
            tail = str(item).split("/")[-1]
            minutes = parse_iso_duration(tail)
            if minutes:
                intervals.append(minutes)
        if intervals:
            worst = max(intervals)
            return worst, f"Every {_humanise(worst)}"

    frequency = str(policy.get("scheduleRunFrequency") or "").strip().lower()
    hourly = policy.get("hourlySchedule") or {}

    # --- Hourly, with the window that decides the real answer ------------------------
    if hourly or frequency == "hourly":
        raw_interval = (hourly or {}).get("interval")
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            return None, ""
        if interval <= 0:
            return None, ""
        interval_minutes = interval * 60
        duration = parse_iso_duration(str((hourly or {}).get("scheduleWindowDuration") or ""))
        start = _clock_minutes(str((hourly or {}).get("scheduleWindowStartTime") or ""))
        if duration and duration < _MINUTES_PER_DAY:
            # Runs happen only inside the window; the overnight silence is the worst gap.
            # A 10h window at 4h intervals fits THREE runs (08:00, 12:00, 16:00), not two —
            # the first one is at offset zero.
            runs_in_window = duration // interval_minutes + 1
            covered = (runs_in_window - 1) * interval_minutes
            gap = _MINUTES_PER_DAY - covered
            window = ""
            if start is not None:
                window = f" from {start // 60:02d}:{start % 60:02d}"
            return gap, (f"Every {interval}h within a {duration // 60}h window{window} — "
                         f"worst gap {_humanise(gap)}")
        return interval_minutes, f"Every {interval}h"

    times = policy.get("scheduleRunTimes")
    clocks = [c for c in (_clock_minutes(str(t)) for t in (times or [])) if c is not None]

    if frequency == "daily":
        return _worst_gap(clocks, _MINUTES_PER_DAY), _daily_summary(clocks)

    if frequency == "weekly":
        days = [str(d).strip().lower() for d in (policy.get("scheduleRunDays") or [])]
        indexes = [_DAY_ORDER.index(d) for d in days if d in _DAY_ORDER]
        if not indexes:
            return _MINUTES_PER_WEEK, "Weekly"
        clock = clocks[0] if clocks else 0
        points = [i * _MINUTES_PER_DAY + clock for i in indexes]
        gap = _worst_gap(points, _MINUTES_PER_WEEK)
        return gap, f"Weekly on {', '.join(d[:3].title() for d in days)} — worst gap {_humanise(gap)}"

    if clocks:
        return _worst_gap(clocks, _MINUTES_PER_DAY), _daily_summary(clocks)

    return None, ""


def _daily_summary(clocks: list[int]) -> str:
    if not clocks:
        return "Daily"
    stamps = ", ".join(f"{c // 60:02d}:{c % 60:02d}" for c in sorted(set(clocks)))
    if len(set(clocks)) == 1:
        return f"Daily at {stamps}"
    return f"Daily at {stamps}"


def _humanise(minutes: int) -> str:
    if minutes % _MINUTES_PER_DAY == 0:
        days = minutes // _MINUTES_PER_DAY
        return "24h" if days == 1 else f"{days}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# --------------------------------------------------------------- native PaaS backup RPO
# Published platform behavior, per mechanism. Confidence reflects how the figure is known:
# read from config (high), documented behavior (medium), published-but-not-guaranteed (low).
_NATIVE: dict[str, tuple[int, str, str]] = {
    "sql_pitr": (10, CONFIDENCE_MEDIUM, "SQL point-in-time restore (continuous log backup)"),
    "sql_geo_replica": (1, CONFIDENCE_MEDIUM, "SQL active geo-replication (asynchronous)"),
    "sql_geo_restore": (60, CONFIDENCE_MEDIUM, "SQL geo-restore from geo-replicated backups"),
    "cosmos_continuous": (1, CONFIDENCE_HIGH, "Cosmos DB continuous backup"),
    "cosmos_multi_write": (0, CONFIDENCE_HIGH, "Cosmos DB multi-region writes"),
    "storage_grs": (15, CONFIDENCE_LOW, "Storage geo-replication (asynchronous, not covered by SLA)"),
    "storage_zrs": (0, CONFIDENCE_HIGH, "Zone-redundant storage"),
    "pg_geo_backup": (60, CONFIDENCE_MEDIUM, "PostgreSQL geo-redundant backup"),
    "zone_redundant": (0, CONFIDENCE_HIGH, "Zone-redundant configuration"),
}


def native_rpo(mechanism: str) -> tuple[int | None, str, str]:
    """``(minutes, confidence, detail)`` for a named platform mechanism."""
    entry = _NATIVE.get(mechanism)
    if not entry:
        return None, CONFIDENCE_LOW, ""
    minutes, confidence, detail = entry
    return minutes, confidence, detail


def observed_vs_configured(
    configured_minutes: int | None, recovery_point_age_hours: float | None,
) -> tuple[int | None, str, Evidence | None]:
    """Reconcile the design with reality; reality wins when they disagree.

    Returns ``(minutes, confidence, drift_evidence)``. ``drift_evidence`` is present only
    when the observed age materially exceeds the configured interval, because that gap is a
    finding in its own right — the policy is fine and the backups are not.
    """
    if recovery_point_age_hours is None:
        if configured_minutes is None:
            return None, CONFIDENCE_LOW, None
        return configured_minutes, CONFIDENCE_HIGH, None

    observed = int(round(float(recovery_point_age_hours) * 60))
    if configured_minutes is None:
        return observed, CONFIDENCE_MEDIUM, None

    # A little slack: a job that starts on time still takes time to finish.
    if observed <= configured_minutes * 1.5:
        return configured_minutes, CONFIDENCE_HIGH, None

    return observed, CONFIDENCE_HIGH, Evidence(
        kind=model.EV_OBSERVED_RECOVERY_POINT,
        detail=(f"Newest recovery point is {_humanise(observed)} old against a configured "
                f"{_humanise(configured_minutes)} — the schedule is not being met."),
        source="Backup Manager",
    )


__all__ = [
    "parse_schedule_interval", "parse_iso_duration", "native_rpo",
    "observed_vs_configured",
]
