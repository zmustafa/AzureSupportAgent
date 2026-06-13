"""Workbook definition registry (JSON, no secrets → no encryption).

Persisted as backend/.data/workbooks.json, consistent with the custom-agents registry.
A workbook *definition* is separate from its run history (DB: WorkbookRun)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "workbooks.json"

# Allowed runtimes for a workbook body.
RUNTIMES = ("az", "kql", "powershell")
# AI'fication transforms a workbook can apply to raw output.
AIFY_MODES = ("summary", "extract", "severity", "diff")
SEVERITIES = ("info", "warning", "error", "critical")

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    # Tenant scope. Blank = a global/seeded workbook visible to every tenant; a non-blank
    # value isolates a user-authored workbook to its tenant.
    "tenant_id": "",
    "runtime": "az",  # az | kql | powershell
    # The snippet. Supports {{param}} interpolation from `params`.
    "body": "",
    # Typed parameters: [{key,label,type,default,required,help}]. type: text|number|select
    "params": [],
    "kind": "read",  # read | write (write requires confirm / non-read-only connection)
    "tags": [],
    "connection_id": "",  # default Azure connection (empty = global default)
    # AI'fication policy.
    "aify": {
        "enabled": True,
        "modes": ["summary", "severity"],  # subset of AIFY_MODES
        "schema": "",  # natural-language target schema for `extract`
    },
    # Emit a notification event when severity >= threshold.
    "alert": {
        "enabled": False,
        "min_severity": "warning",
    },
    # Surface the latest run as a Monitor dashboard tile.
    "tile": {
        "enabled": False,
        "label": "",
        "format": "severity",  # severity | number | text
        "metric_key": "",  # key into structured_json for `number` tiles
    },
    "enabled": True,
    "starter": False,  # true for seeded curated workbooks
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


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
    return {"workbooks": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(stored: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))  # deep copy
    for k, v in stored.items():
        if k in ("aify", "alert", "tile") and isinstance(v, dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def list_workbooks() -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for wid, wb in data.get("workbooks", {}).items():
        merged = _merge(wb)
        merged["id"] = wid
        out.append(merged)
    out.sort(key=lambda w: w.get("name", "").lower())
    return out


def get_workbook(workbook_id: str) -> dict[str, Any] | None:
    if not workbook_id:
        return None
    for w in list_workbooks():
        if w["id"] == workbook_id:
            return w
    return None


def upsert_workbook(wb: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    workbooks = data.setdefault("workbooks", {})
    wid = wb.get("id") or str(uuid.uuid4())
    existing = workbooks.get(wid, {})
    merged = dict(existing)
    for key in DEFAULTS:
        if key in wb and wb[key] is not None:
            merged[key] = wb[key]
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    workbooks[wid] = merged
    _write(data)
    result = get_workbook(wid)
    assert result is not None
    return result


def delete_workbook(workbook_id: str) -> bool:
    data = _read()
    if workbook_id in data.get("workbooks", {}):
        del data["workbooks"][workbook_id]
        _write(data)
        return True
    return False


def seed_if_empty() -> int:
    """Seed curated starter workbooks on first run. Returns number seeded."""
    data = _read()
    if data.get("workbooks"):
        return 0
    from app.workbooks.starters import STARTER_WORKBOOKS

    workbooks = data.setdefault("workbooks", {})
    for wb in STARTER_WORKBOOKS:
        wid = str(uuid.uuid4())
        merged = dict(wb)
        merged["created_at"] = _now()
        merged["updated_at"] = _now()
        merged["created_by"] = "system"
        merged["starter"] = True
        workbooks[wid] = merged
    _write(data)
    return len(STARTER_WORKBOOKS)
