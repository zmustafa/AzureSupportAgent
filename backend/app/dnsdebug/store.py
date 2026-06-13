"""Persistent store of DNS-debug runs (Re-run + diff), under
``backend/.data/dnsdebug_runs.json``. Mirrors app.netcheck.store."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "dnsdebug_runs.json"
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


def run_key(architecture_id: str, source: str, fqdn: str) -> str:
    return f"{architecture_id}|{source}|{fqdn}".lower()


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
    key = run.get("key", "")
    same = [r for r in runs.values() if r.get("tenant_id") == tenant_id and r.get("key") == key]
    same.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    for old in same[_MAX_PER_KEY:]:
        runs.pop(old["id"], None)
    _write(data)
    return run


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


def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    r = _read().get("runs", {}).get(run_id)
    if r and r.get("tenant_id") == tenant_id:
        return r
    return None


def list_runs(tenant_id: str, *, architecture_id: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in _read().get("runs", {}).values():
        if r.get("tenant_id") != tenant_id:
            continue
        if architecture_id and r.get("architecture_id") != architecture_id:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def diff_runs(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[dict[str, Any]]:
    if not prev:
        return []
    out: list[dict[str, Any]] = []
    # Per-source verdict / resolved-ip deltas.
    prev_src = {s.get("source"): s for s in prev.get("sources", [])}
    for s in cur.get("sources", []):
        p = prev_src.get(s.get("source"))
        if p and p.get("classification") != s.get("classification"):
            out.append({"source": s.get("source"), "from": p.get("classification"), "to": s.get("classification")})
    return out
