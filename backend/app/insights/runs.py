"""Insight Pack run store — persists each run's digest (verdict, headline, bullets, table,
counts, scope) so the UI can render history and notifications can deep-link back. JSON-backed
per tenant, bounded to the most recent runs.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parents[2] / ".data" / "insight_runs"
_MAX_PER_TENANT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _path(tenant_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tenant_id or "default"))
    return _DIR / f"{safe}.json"


def _read(tenant_id: str) -> list[dict[str, Any]]:
    p = _path(tenant_id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _write(tenant_id: str, runs: list[dict[str, Any]]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _path(tenant_id).write_text(json.dumps(runs[:_MAX_PER_TENANT], indent=2), encoding="utf-8")


def save_run(tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
    run.setdefault("id", new_id())
    run.setdefault("created_at", _now())
    runs = _read(tenant_id)
    runs.insert(0, run)  # newest first
    _write(tenant_id, runs)
    return run


def list_runs(tenant_id: str, *, pack_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    runs = _read(tenant_id)
    if pack_id:
        runs = [r for r in runs if r.get("pack_id") == pack_id]
    return runs[:limit]


def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    for r in _read(tenant_id):
        if r.get("id") == run_id:
            return r
    return None


def latest_run(tenant_id: str, *, pack_id: str | None = None) -> dict[str, Any] | None:
    runs = list_runs(tenant_id, pack_id=pack_id, limit=1)
    return runs[0] if runs else None
