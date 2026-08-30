"""AMBA change-request registry — the "Send to Approval Inbox" path (Option A).

A standalone, audited registry of proposed monitoring-alert changes (the generated IaC
for a set of gaps) awaiting human review. Deliberately separate from the agent's
``Approval``/``ToolCall`` model (which is coupled to chat tool calls). The app NEVER
auto-applies an approved request — approval records the human sign-off and the IaC is
exported to the customer's own pipeline. Persisted at
``backend/.data/amba_change_requests.json`` on the Azure Files volume."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_change_requests.json"

STATUSES = ("pending", "approved", "rejected", "applied")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"requests": {}})
    return data if isinstance(data, dict) else {"requests": {}}


def create_request(
    *,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    scope_name: str,
    gaps: list[dict[str, Any]],
    iac_format: str,
    iac_text: str,
    requested_by: str,
) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    req = {
        "id": rid,
        "tenant_id": tenant_id,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "gap_count": len(gaps),
        "gaps": gaps[:200],
        "iac_format": iac_format,
        "iac_text": iac_text,
        "status": "pending",
        "requested_by": requested_by,
        "requested_at": _now(),
        "decided_by": "",
        "decided_at": "",
        "reason": "",
    }
    jsonstore.mutate_json(
        _PATH,
        {"requests": {}},
        lambda data: data.setdefault("requests", {}).__setitem__(rid, req),
    )
    return req


def list_requests(tenant_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
    out = [
        r for r in _read().get("requests", {}).values()
        if r.get("tenant_id") == tenant_id and (status is None or r.get("status") == status)
    ]
    out.sort(key=lambda r: r.get("requested_at", ""), reverse=True)
    return out


def get_request(tenant_id: str, request_id: str) -> dict[str, Any] | None:
    r = _read().get("requests", {}).get(request_id)
    if r and r.get("tenant_id") == tenant_id:
        return r
    return None


def decide_request(
    tenant_id: str, request_id: str, *, decision: str, actor: str, reason: str = ""
) -> dict[str, Any] | None:
    """Set a request to approved/rejected/applied. Never applies anything itself."""
    if decision not in ("approved", "rejected", "applied"):
        return None
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        request = data.get("requests", {}).get(request_id)
        if not request or request.get("tenant_id") != tenant_id:
            return
        request["status"] = decision
        request["decided_by"] = actor
        request["decided_at"] = _now()
        if reason:
            request["reason"] = reason[:1000]
        result.update(request)

    jsonstore.mutate_json(_PATH, {"requests": {}}, _mutate)
    return result or None


def delete_request(tenant_id: str, request_id: str) -> bool:
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        request = data.get("requests", {}).get(request_id)
        if request and request.get("tenant_id") == tenant_id:
            del data["requests"][request_id]
            deleted = True

    jsonstore.mutate_json(_PATH, {"requests": {}}, _mutate)
    return deleted
