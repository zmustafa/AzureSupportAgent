"""Day-over-day snapshots — remember what each pack saw last time it ran on a scope.

After a run, we persist a compact fingerprint (per source: the set of item ids that were
present). On the next run we diff the freshly gathered items against that fingerprint so the
reason stage can focus on *what is new since last time* rather than re-reporting steady state.

This is deliberately lightweight: JSON on disk, keyed by (tenant, pack, scope), capped id lists.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("app.insights.snapshots")

_PATH = Path(__file__).resolve().parents[2] / ".data" / "insight_snapshots.json"
_MAX_IDS_PER_SOURCE = 4000
_MAX_SCOPES = 2000


def scope_key(scope: dict[str, Any]) -> str:
    """A stable string identifying the scope a pack ran against (mode + ids)."""
    mode = (scope or {}).get("mode", "workload")
    if mode == "subscription":
        return f"sub:{scope.get('subscription_id', '')}"
    wids = scope.get("workload_ids") or ([scope["workload_id"]] if scope.get("workload_id") else [])
    return f"{mode}:{','.join(sorted(str(w) for w in wids))}"


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
    _PATH.write_text(json.dumps(data), encoding="utf-8")


def _key(tenant_id: str, pack_id: str, scope: dict[str, Any]) -> str:
    return f"{tenant_id or 'default'}|{pack_id}|{scope_key(scope)}"


def load(tenant_id: str, pack_id: str, scope: dict[str, Any]) -> dict[str, list[str]]:
    """Return the previous fingerprint: {source_id: [item_id, ...]} (empty if first run)."""
    entry = _read().get(_key(tenant_id, pack_id, scope))
    if not isinstance(entry, dict):
        return {}
    ids = entry.get("ids")
    return ids if isinstance(ids, dict) else {}


def save(tenant_id: str, pack_id: str, scope: dict[str, Any], ids_by_source: dict[str, list[str]]) -> None:
    """Persist the current fingerprint, trimming per-source id lists and old scopes."""
    if not pack_id:
        return
    data = _read()
    trimmed = {src: list(ids)[:_MAX_IDS_PER_SOURCE] for src, ids in ids_by_source.items()}
    data[_key(tenant_id, pack_id, scope)] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ids": trimmed,
    }
    if len(data) > _MAX_SCOPES:
        # Drop the oldest scopes beyond the cap.
        items = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("updated_at", ""), reverse=True)
        data = dict(items[:_MAX_SCOPES])
    try:
        _write(data)
    except OSError:
        log.warning("Failed to persist insight snapshot", exc_info=True)
