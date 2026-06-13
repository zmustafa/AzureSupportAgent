"""AMBA change-request registry — the "Send to Approval Inbox" path (Option A).

A standalone, audited registry of proposed monitoring-alert changes (the generated IaC
for a set of gaps) awaiting human review. Deliberately separate from the agent's
``Approval``/``ToolCall`` model (which is coupled to chat tool calls). The app NEVER
auto-applies an approved request — approval records the human sign-off and the IaC is
exported to the customer's own pipeline. Persisted at
``backend/.data/amba_change_requests.json`` on the Azure Files volume."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_change_requests.json"

STATUSES = ("pending", "approved", "rejected", "applied")


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
    return {"requests": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    data = _read()
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
    data.setdefault("requests", {})[rid] = req
    _write(data)
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
    data = _read()
    r = data.get("requests", {}).get(request_id)
    if not r or r.get("tenant_id") != tenant_id:
        return None
    r["status"] = decision
    r["decided_by"] = actor
    r["decided_at"] = _now()
    if reason:
        r["reason"] = reason[:1000]
    _write(data)
    return r


def delete_request(tenant_id: str, request_id: str) -> bool:
    data = _read()
    r = data.get("requests", {}).get(request_id)
    if not r or r.get("tenant_id") != tenant_id:
        return False
    del data["requests"][request_id]
    _write(data)
    return True
