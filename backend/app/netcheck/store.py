"""Persistent store of reachability runs, for Re-run + diff.

Runs are saved under ``backend/.data/netcheck_runs.json`` keyed by
``(architecture_id, source, target, port)`` so a "Re-run" can diff against the prior run
of the same path. Keeps the last N runs per key."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "netcheck_runs.json"
_MAX_PER_KEY = 20


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
    return {"runs": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_key(architecture_id: str, source: str, target: str, port: int) -> str:
    return f"{architecture_id}|{source}|{target}|{port}".lower()


def latest_for_key(tenant_id: str, key: str) -> dict[str, Any] | None:
    runs = [r for r in _read().get("runs", {}).values() if r.get("tenant_id") == tenant_id and r.get("key") == key]
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs[0] if runs else None


def save_run(tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    runs = data.setdefault("runs", {})
    rid = run.get("id") or str(uuid.uuid4())
    run["id"] = rid
    run["tenant_id"] = tenant_id
    run.setdefault("created_at", _now())
    runs[rid] = run
    # Prune old runs for this key.
    key = run.get("key", "")
    same = [r for r in runs.values() if r.get("tenant_id") == tenant_id and r.get("key") == key]
    same.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    for old in same[_MAX_PER_KEY:]:
        runs.pop(old["id"], None)
    _write(data)
    return run


def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    r = _read().get("runs", {}).get(run_id)
    if r and r.get("tenant_id") == tenant_id:
        return r
    return None


def delete_by_architecture(tenant_id: str, architecture_id: str) -> int:
    """Remove all runs for an architecture id (used to purge demo data). Returns count."""
    data = _read()
    runs = data.get("runs", {})
    rids = [rid for rid, r in runs.items()
            if r.get("tenant_id") == tenant_id and r.get("architecture_id") == architecture_id]
    for rid in rids:
        runs.pop(rid, None)
    if rids:
        _write(data)
    return len(rids)


def list_runs(tenant_id: str, *, architecture_id: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in _read().get("runs", {}).values():
        if r.get("tenant_id") != tenant_id:
            continue
        if architecture_id and r.get("architecture_id") != architecture_id:
            continue
        if key and r.get("key") != key:
            continue
        out.append({k: v for k, v in r.items() if k != "steps_raw"})
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def diff_runs(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-step status delta between two runs (for fast change verification)."""
    if not prev:
        return []
    prev_steps = {s["step"]: s for s in prev.get("steps", [])}
    out: list[dict[str, Any]] = []
    for s in cur.get("steps", []):
        p = prev_steps.get(s["step"])
        if p and p.get("status") != s.get("status"):
            out.append({"step": s["step"], "from": p.get("status"), "to": s.get("status")})
    # Overall verdict change.
    if prev.get("verdict") != cur.get("verdict"):
        out.append({"step": "verdict", "from": prev.get("verdict"), "to": cur.get("verdict")})
    return out
