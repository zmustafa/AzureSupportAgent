"""Approval-gated, non-executing Alerts Manager remediation-plan registry."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alert_analysis_plans.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if not _PATH.exists():
        return {"plans": {}}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"plans": {}}
    except (OSError, json.JSONDecodeError):
        return {"plans": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def create_plan(
    *,
    tenant_id: str,
    connection_id: str,
    scope_kind: str,
    scope_id: str,
    scope_name: str,
    requested_by: str,
    artifact: str,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    plan_id = str(uuid.uuid4())
    plan = {
        "id": plan_id,
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "status": "pending",
        "requested_by": requested_by,
        "requested_at": _now(),
        "decided_by": "",
        "decided_at": "",
        "reason": "",
        "artifact_format": "bicep",
        "artifact": artifact,
        "actions": actions[:500],
        "safety": "Preview only. This application has no endpoint that executes this plan.",
    }
    data = _read()
    data.setdefault("plans", {})[plan_id] = plan
    _write(data)
    return plan


def list_plans(tenant_id: str) -> list[dict[str, Any]]:
    out = [plan for plan in _read().get("plans", {}).values() if plan.get("tenant_id") == tenant_id]
    out.sort(key=lambda item: item.get("requested_at", ""), reverse=True)
    return out


def get_plan(tenant_id: str, plan_id: str) -> dict[str, Any] | None:
    plan = _read().get("plans", {}).get(plan_id)
    return plan if plan and plan.get("tenant_id") == tenant_id else None


def decide_plan(tenant_id: str, plan_id: str, decision: str, actor: str, reason: str = "") -> dict[str, Any] | None:
    if decision not in {"approved", "rejected"}:
        return None
    data = _read()
    plan = data.get("plans", {}).get(plan_id)
    if not plan or plan.get("tenant_id") != tenant_id or plan.get("status") != "pending":
        return None
    plan["status"] = decision
    plan["decided_by"] = actor
    plan["decided_at"] = _now()
    plan["reason"] = reason[:1000]
    _write(data)
    return plan


def delete_plan(tenant_id: str, plan_id: str) -> bool:
    data = _read()
    plan = data.get("plans", {}).get(plan_id)
    if not plan or plan.get("tenant_id") != tenant_id:
        return False
    del data["plans"][plan_id]
    _write(data)
    return True
