"""Persistent store of DNS-debug runs (Re-run + diff), under
``backend/.data/dnsdebug_runs.json``. Mirrors app.netcheck.store."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "dnsdebug_runs.json"
_MAX_PER_KEY = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"runs": {}})
    return data if isinstance(data, dict) else {"runs": {}}


def run_key(architecture_id: str, source: str, fqdn: str) -> str:
    return f"{architecture_id}|{source}|{fqdn}".lower()


def latest_for_key(tenant_id: str, key: str) -> dict[str, Any] | None:
    runs = [r for r in _read().get("runs", {}).values() if r.get("tenant_id") == tenant_id and r.get("key") == key]
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs[0] if runs else None


def save_run(tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
    rid = run.get("id") or str(uuid.uuid4())
    run["id"] = rid
    run["tenant_id"] = tenant_id
    run.setdefault("created_at", _now())
    def _mutate(data: dict[str, Any]) -> None:
        runs = data.setdefault("runs", {})
        runs[rid] = run
        key = run.get("key", "")
        same = [
            stored for stored in runs.values()
            if stored.get("tenant_id") == tenant_id and stored.get("key") == key
        ]
        same.sort(key=lambda stored: stored.get("created_at", ""), reverse=True)
        for old in same[_MAX_PER_KEY:]:
            runs.pop(old["id"], None)

    jsonstore.mutate_json(_PATH, {"runs": {}}, _mutate)
    return run


def delete_by_architecture(tenant_id: str, architecture_id: str) -> int:
    """Remove all runs for an architecture id (used to purge demo data). Returns count."""
    removed: list[str] = []

    def _mutate(data: dict[str, Any]) -> None:
        runs = data.get("runs", {})
        removed.extend(
            rid for rid, run in runs.items()
            if run.get("tenant_id") == tenant_id
            and run.get("architecture_id") == architecture_id
        )
        for rid in removed:
            runs.pop(rid, None)

    jsonstore.mutate_json(_PATH, {"runs": {}}, _mutate)
    return len(removed)


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
