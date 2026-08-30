"""Autopilot grouping memory — learn from the user's corrections.

When a user reviews Autopilot's proposed workloads they implicitly teach the system: they
rename a candidate, exclude a resource, or accept/reject a grouping. We persist those
signals per (tenant, connection) so the NEXT discovery run respects prior decisions
("you previously kept *staging* separate from *prod*") instead of re-proposing the same
thing the user already corrected.

Stored at backend/.data/workload_grouping_memory.json (Azure Files volume), keyed by
``<tenant>::<connection>``. No secrets → no encryption.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "workload_grouping_memory.json"
_MAX_DECISIONS = 200  # cap per (tenant, connection) bucket


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


def _key(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or 'default'}::{connection_id or ''}"


def record_decisions(
    tenant_id: str, connection_id: str, decisions: list[dict[str, Any]]
) -> int:
    """Append grouping decisions for a (tenant, connection). Each decision is a small dict
    like ``{action: 'accept'|'reject'|'rename'|'exclude', name, rg?, from?, to?, excluded?}``.
    Returns the new bucket size. De-dupes consecutive identical decisions."""
    if not decisions:
        return 0
    key = _key(tenant_id, connection_id)
    size = 0

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal size
        bucket = data.setdefault(key, [])
        for decision in decisions:
            if not isinstance(decision, dict) or not decision.get("action"):
                continue
            entry = {**decision, "at": _now()}
            # Skip an exact-content duplicate of the most recent entry.
            if bucket:
                previous = {k: v for k, v in bucket[-1].items() if k != "at"}
                if previous == {k: v for k, v in entry.items() if k != "at"}:
                    continue
            bucket.append(entry)
        # Cap (keep most recent).
        if len(bucket) > _MAX_DECISIONS:
            data[key] = bucket[-_MAX_DECISIONS:]
        size = len(data[key])

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return size


def get_decisions(tenant_id: str, connection_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """Most-recent grouping decisions for a (tenant, connection)."""
    data = _read()
    bucket = data.get(_key(tenant_id, connection_id), [])
    return list(bucket[-limit:])


def prompt_hint(tenant_id: str, connection_id: str, limit: int = 25) -> str:
    """Render prior decisions as a compact instruction block for the discovery prompt.
    Empty string when there's no memory (first run)."""
    decisions = get_decisions(tenant_id, connection_id, limit)
    if not decisions:
        return ""
    lines: list[str] = []
    for d in decisions:
        act = d.get("action", "")
        if act == "rename":
            lines.append(f"- The user renamed '{d.get('from', '')}' to '{d.get('to', '')}'.")
        elif act == "reject":
            lines.append(f"- The user rejected the proposed workload '{d.get('name', '')}'.")
        elif act == "accept":
            lines.append(f"- The user accepted the workload '{d.get('name', '')}'.")
        elif act == "exclude":
            lines.append(f"- The user excluded '{d.get('excluded', '')}' from '{d.get('name', '')}'.")
        elif act == "split":
            lines.append(f"- The user split '{d.get('name', '')}' into separate workloads.")
        elif act == "merge":
            lines.append(f"- The user merged {d.get('from', '')} into one workload '{d.get('to', '')}'.")
    if not lines:
        return ""
    return (
        "PRIOR USER CORRECTIONS (respect these — the user has taught you their mental model "
        "of these workloads; honor renames, keep rejected groupings apart, and keep "
        "accepted groupings intact):\n" + "\n".join(lines[:limit])
    )
