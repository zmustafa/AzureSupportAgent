"""Approval-gated, non-executing Alerts Manager remediation-plan registry."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alert_analysis_plans.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"plans": {}})
    return data if isinstance(data, dict) else {"plans": {}}


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


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
    jsonstore.mutate_json(
        _PATH,
        {"plans": {}},
        lambda data: data.setdefault("plans", {}).__setitem__(plan_id, plan),
    )
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
    decided: dict[str, Any] | None = None

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal decided
        plan = data.get("plans", {}).get(plan_id)
        if not plan or plan.get("tenant_id") != tenant_id or plan.get("status") != "pending":
            return
        plan["status"] = decision
        plan["decided_by"] = actor
        plan["decided_at"] = _now()
        plan["reason"] = reason[:1000]
        decided = dict(plan)

    jsonstore.mutate_json(_PATH, {"plans": {}}, _mutate)
    return decided


def delete_plan(tenant_id: str, plan_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        plan = data.get("plans", {}).get(plan_id)
        if plan and plan.get("tenant_id") == tenant_id:
            del data["plans"][plan_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"plans": {}}, _mutate)
    return deleted
