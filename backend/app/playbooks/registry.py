"""Playbooks: chain workbooks into a sequential flow with conditional steps.

A *playbook* runs an ordered list of steps. Each step invokes a workbook with parameters
that may be static or mapped from a previous step's structured output. A step can be
gated on the running severity (e.g. only run a remediation step if a prior step reported
``>= error``). The highest step severity becomes the playbook's overall severity, and a
playbook can emit a notification event on completion.

Definitions persist as backend/.data/playbooks.json (no secrets). Run history is light
(kept in-memory return value + per-workbook WorkbookRun rows)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "playbooks.json"

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    # Tenant scope. Blank = global/seeded (visible to all); non-blank isolates to a tenant.
    "tenant_id": "",
    "connection_id": "",
    # Steps: [{id, workbook_id, name, params (static dict),
    #          param_map ({param_key: "prevStepId.structuredKey"}),
    #          run_if ("always" | "info" | "warning" | "error" | "critical")}]
    "steps": [],
    "alert": {"enabled": False, "min_severity": "warning"},
    "enabled": True,
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"playbooks": {}})
    return data if isinstance(data, dict) else {"playbooks": {}}


def _merge(stored: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    for k, v in stored.items():
        if k == "alert" and isinstance(v, dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def list_playbooks() -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for pid, pb in data.get("playbooks", {}).items():
        merged = _merge(pb)
        merged["id"] = pid
        out.append(merged)
    out.sort(key=lambda p: p.get("name", "").lower())
    return out


def get_playbook(playbook_id: str) -> dict[str, Any] | None:
    if not playbook_id:
        return None
    for p in list_playbooks():
        if p["id"] == playbook_id:
            return p
    return None


def upsert_playbook(pb: dict[str, Any]) -> dict[str, Any]:
    pid = pb.get("id") or str(uuid.uuid4())
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        playbooks = data.setdefault("playbooks", {})
        existing = playbooks.get(pid, {})
        merged = dict(existing)
        for key in DEFAULTS:
            if key in pb and pb[key] is not None:
                merged[key] = pb[key]
        merged["created_at"] = existing.get("created_at") or _now()
        merged["updated_at"] = _now()
        merged.pop("id", None)
        playbooks[pid] = merged
        result.update(_merge(merged))
        result["id"] = pid

    jsonstore.mutate_json(_PATH, {"playbooks": {}}, _mutate)
    return result


def delete_playbook(playbook_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        if playbook_id in data.get("playbooks", {}):
            del data["playbooks"][playbook_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"playbooks": {}}, _mutate)
    return deleted
