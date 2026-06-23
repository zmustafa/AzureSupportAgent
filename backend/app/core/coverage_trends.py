"""Shared per-(feature, tenant, scope) trend store for coverage / posture metrics.

The coverage & profiling dashboards each compute a single headline 0-100 score for a
workload (AMBA monitoring coverage %, telemetry coverage %, backup/DR % protected,
performance score). Their full snapshots are big (per-resource matrices) and only the
*latest* is cached — which can't answer "how is this moving over time?".

This module records a COMPACT point per scan — ``{at, pct, extra}`` — so a workload's
posture can be charted as a small trend line and compared scan-over-scan, without storing
heavy history. One JSON file holds every feature's series, keyed by
``feature:scope_kind:scope_id`` within a tenant. Persisted on the Azure Files volume
(``backend/.data/coverage_trends.json``), bounded per series.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "coverage_trends.json"

# Features that record trend points (used for validation + demo purge enumeration).
FEATURES = ("amba", "telemetry", "backupdr", "performance", "ownership")

# Max points kept per series (oldest evicted). ~3 months of daily scans.
_MAX_POINTS = 90

# Two scans of the SAME value within this window collapse into one point (so rapid
# double-clicks on Refresh don't spam the series); day-over-day scans always append.
_DEDUP_WINDOW_S = 300


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


def _key(feature: str, scope_kind: str, scope_id: str) -> str:
    return f"{feature}:{scope_kind}:{scope_id}"


def _coerce_pct(pct: Any) -> int | None:
    if pct is None:
        return None
    try:
        return max(0, min(100, round(float(pct))))
    except (TypeError, ValueError):
        return None


def _age_s(iso: str) -> float | None:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def record(
    feature: str,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    *,
    pct: Any,
    extra: dict[str, Any] | None = None,
    demo: bool = False,
    at: str | None = None,
) -> dict[str, Any]:
    """Append a compact trend point for a scan. Collapses a duplicate value recorded within
    ``_DEDUP_WINDOW_S`` of the previous point (rapid re-refresh) into that point. Returns the
    stored point. ``pct`` is clamped to 0-100; ``None`` is allowed (e.g. nothing in scope)."""
    point = {"at": at or _now(), "pct": _coerce_pct(pct), "extra": extra or {}, "demo": bool(demo)}
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    series = bucket.setdefault(_key(feature, scope_kind, scope_id), [])
    last = series[-1] if series else None
    last_age = _age_s(last.get("at", "")) if last is not None else None
    if (
        last is not None
        and last.get("pct") == point["pct"]
        and last_age is not None
        and last_age < _DEDUP_WINDOW_S
    ):
        # Same value, just re-scanned moments ago → refresh the existing point in place.
        last["at"] = point["at"]
        last["extra"] = point["extra"]
        last["demo"] = point["demo"]
    else:
        series.append(point)
        if len(series) > _MAX_POINTS:
            del series[: len(series) - _MAX_POINTS]
    _write(data)
    return point


def series(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """The chronological (oldest-first) trend points for a scope."""
    bucket = _read().get(tenant_id or "default", {})
    pts = bucket.get(_key(feature, scope_kind, scope_id), [])
    return pts if isinstance(pts, list) else []


def trend(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> dict[str, Any]:
    """A chart-ready summary: the point series plus current/previous/delta for the header."""
    pts = series(feature, tenant_id, scope_kind, scope_id)
    current = pts[-1]["pct"] if pts else None
    previous = pts[-2]["pct"] if len(pts) >= 2 else None
    delta = (current - previous) if (isinstance(current, int) and isinstance(previous, int)) else None
    return {
        "feature": feature,
        "points": pts,
        "current": current,
        "previous": previous,
        "delta": delta,
        "count": len(pts),
        "unit": "%",
    }


def delete_scope(feature: str, tenant_id: str, scope_kind: str, scope_id: str) -> bool:
    """Drop a scope's whole series (used to purge demo data). True if one existed."""
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    k = _key(feature, scope_kind, scope_id)
    if k in bucket:
        del bucket[k]
        _write(data)
        return True
    return False


def seed_demo_series(
    feature: str,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    *,
    current_pct: Any,
    extra: dict[str, Any] | None = None,
    points: int = 6,
    span_days: int = 12,
    climb: int = 16,
) -> list[dict[str, Any]]:
    """Backfill a believable *rising* history ending at ``current_pct`` so the trend chart
    shows movement immediately on a demo scope (i.e. "gaps got fixed over the last 2 weeks").
    No-op (returns the existing series) if a series already exists. Marks points ``demo``."""
    existing = series(feature, tenant_id, scope_kind, scope_id)
    if existing:
        return existing
    end = _coerce_pct(current_pct)
    if end is None:
        return []
    start = max(0, end - max(0, climb))
    now = datetime.now(timezone.utc)
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    out: list[dict[str, Any]] = []
    n = max(2, points)
    for i in range(n):
        frac = i / (n - 1)
        # Ease-out so most of the improvement happens earlier, then plateaus near current.
        val = round(start + (end - start) * (frac ** 0.7))
        # Oldest point is ~span_days ago; the newest historical scan is ~1 day ago, so a
        # fresh "Refresh now" appends today's point and the timeline visibly grows.
        days_ago = 1 + (span_days - 1) * (1 - frac)
        at = (now - timedelta(days=round(days_ago))).isoformat()
        out.append({"at": at, "pct": val, "extra": extra or {} if i == n - 1 else {}, "demo": True})
    bucket[_key(feature, scope_kind, scope_id)] = out
    _write(data)
    return out
