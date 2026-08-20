"""Recovery Readiness history — is this getting better or worse?

The snapshot store keeps exactly one analysis per scope and overwrites it, which is the
right contract for a screen whose numbers must not move under the reader. It also means a
report can say what the estate looks like and nothing at all about direction, and "twelve
resources have no recovery path" reads very differently depending on whether it was four
last month or forty.

So each analysis appends **one small point** here — counts only, never resource rows. A
history that stored snapshots would grow without bound and duplicate the thing it is a
history of.

Two rules the trend must not break:

* **`undetermined` travels with every point.** A drop in "no recovery path" that happened
  because a source stopped being readable is not an improvement, and a line that cannot
  show the difference will be read as one.
* **Points are never interpolated or back-filled.** A gap in the series means nobody ran an
  analysis; drawing through it invents measurements that were never taken.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.resiliency import model

log = logging.getLogger("app.resiliency.history")

SCHEMA_VERSION = 1

_PATH = Path(__file__).resolve().parents[2] / ".data" / "resiliency_history.json"

#: Points kept per scope. A year of weekly analyses, or a quarter of daily ones.
MAX_POINTS = 60
#: Scopes tracked. Matches the snapshot store so history cannot outlive its snapshots.
MAX_SCOPES = 24


def set_path_for_tests(path: Path) -> None:
    global _PATH
    _PATH = path


def _key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return "|".join((str(tenant_id or "default"), str(connection_id or "default"),
                     str(scope_kind or ""), str(scope_id or "").lower()))


def _read_all() -> dict[str, Any]:
    if not _PATH.exists():
        return {}
    try:
        value = json.loads(_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("resiliency: unreadable history store, starting empty: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _write_all(value: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(_PATH)
    except OSError as exc:
        log.warning("resiliency: could not persist history: %s", exc)


def point_from(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The counts worth trending, derived from a finished snapshot."""
    summary = snapshot.get("summary") or {}
    by_scenario = summary.get("by_scenario") or {}
    protection = summary.get("protection") or {}

    no_path = sum(int((by_scenario.get(s) or {}).get("no_recovery_path", 0))
                  for s in model.SCENARIOS)
    undetermined = sum(int((by_scenario.get(s) or {}).get("undetermined", 0))
                       for s in model.SCENARIOS)

    breaches = snapshot.get("breaches") or []
    return {
        "generated_at": str(snapshot.get("generated_at") or ""),
        "resources": int(summary.get("resources", 0)),
        # Per resource-scenario pair, not per resource: one resource can lose three ways.
        "no_recovery_path": no_path,
        "undetermined": undetermined,
        "breaches": len(breaches),
        "protected": int(protection.get("protected", 0)),
        "not_protected": int(protection.get("not_protected", 0)),
        "protection_unknown": int(protection.get("unknown", 0)),
        "demo": bool(snapshot.get("demo")),
    }


def record(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str,
           snapshot: dict[str, Any]) -> None:
    """Append one point for this scope. Demo analyses are not recorded.

    A demo trend would be a straight line through synthetic data presented beside real
    numbers, which is the kind of thing that gets quoted."""
    if snapshot.get("demo") or not snapshot.get("report_exists"):
        return
    point = point_from(snapshot)
    if not point["generated_at"]:
        return

    store = _read_all()
    key = _key(tenant_id, connection_id, scope_kind, scope_id)
    points = [p for p in (store.get(key) or {}).get("points", []) if isinstance(p, dict)]
    # Re-analyzing the same scope twice in a minute is one measurement, not a trend.
    points = [p for p in points if p.get("generated_at") != point["generated_at"]]
    points.append(point)
    points.sort(key=lambda p: str(p.get("generated_at") or ""))
    store[key] = {"schema_version": SCHEMA_VERSION, "points": points[-MAX_POINTS:]}

    if len(store) > MAX_SCOPES:
        ordered = sorted(
            store.items(),
            key=lambda kv: str(((kv[1].get("points") or [{}])[-1]).get("generated_at") or ""))
        for stale, _ in ordered[: len(store) - MAX_SCOPES]:
            store.pop(stale, None)
    _write_all(store)


def read(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str
         ) -> list[dict[str, Any]]:
    entry = _read_all().get(_key(tenant_id, connection_id, scope_kind, scope_id)) or {}
    if entry.get("schema_version") != SCHEMA_VERSION:
        return []
    return list(entry.get("points") or [])


def trend(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str
          ) -> dict[str, Any]:
    """Direction of travel, or an explicit refusal to claim one.

    ``available`` is False for a single point. One measurement is not a trend, and a
    sparkline drawn through it invites a reader to see a direction that was never
    measured."""
    points = read(tenant_id, connection_id, scope_kind, scope_id)
    if len(points) < 2:
        return {"available": False, "points": points,
                "reason": "At least two analyses are needed before a direction can be shown."}

    first, last = points[0], points[-1]
    deltas = {
        key: int(last.get(key, 0)) - int(first.get(key, 0))
        for key in ("no_recovery_path", "breaches", "undetermined", "resources",
                    "not_protected")
    }
    # A fall in `no_recovery_path` alongside a rise in `undetermined` is very often the same
    # resources becoming unreadable, so the caller is handed both rather than a verdict.
    reading_degraded = deltas["undetermined"] > 0 and deltas["no_recovery_path"] < 0
    return {
        "available": True,
        "points": points,
        "first": first,
        "last": last,
        "deltas": deltas,
        "reading_degraded": reading_degraded,
        "caveat": (
            "Fewer resources are reported without a recovery path, but more could not be "
            "read at all. That is not necessarily an improvement."
        ) if reading_degraded else "",
    }


def clear(tenant_id: str = "", connection_id: str = "", scope_kind: str = "",
          scope_id: str = "") -> int:
    store = _read_all()
    if not scope_id and not scope_kind:
        count = len(store)
        _write_all({})
        return count
    key = _key(tenant_id, connection_id, scope_kind, scope_id)
    removed = 1 if store.pop(key, None) else 0
    _write_all(store)
    return removed


__all__ = ["record", "read", "trend", "clear", "point_from", "set_path_for_tests",
           "MAX_POINTS", "MAX_SCOPES", "SCHEMA_VERSION"]
