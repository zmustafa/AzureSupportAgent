"""Sanitized Action Group test-delivery history used by the path simulator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.alerts_manager import service
from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alerts_manager_delivery_history.json"
_MAX_ROWS = 1000


def record(tenant_id: str, action_group_id: str, result: dict[str, Any], *, actor: str) -> dict[str, Any]:
    details = []
    for item in result.get("actionDetails") or []:
        if not isinstance(item, dict):
            continue
        details.append({
            "mechanism": str(item.get("MechanismType") or item.get("mechanism") or ""),
            "name": str(item.get("Name") or item.get("name") or ""),
            "status": str(item.get("Status") or item.get("status") or ""),
            "sub_state": str(item.get("SubState") or item.get("sub_state") or ""),
            "detail": service.safe_error(item.get("Detail") or item.get("detail")),
        })
    row = {
        "tenant_id": tenant_id or "default", "action_group_id": action_group_id.lower(),
        "tested_at": datetime.now(timezone.utc).isoformat(), "tested_by": actor,
        "state": str(result.get("state") or "Unknown"), "details": details,
    }
    def _mutate(data: dict[str, Any]) -> None:
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        rows.append(row)
        data["rows"] = rows[-_MAX_ROWS:]

    jsonstore.mutate_json(_PATH, {"rows": []}, _mutate)
    return row


def for_groups(tenant_id: str, action_group_ids: list[str], *, limit: int = 50) -> list[dict[str, Any]]:
    wanted = {value.lower() for value in action_group_ids}
    data = jsonstore.read_json(_PATH, {"rows": []})
    rows = [
        row for row in data.get("rows") or []
        if isinstance(row, dict) and row.get("tenant_id") == (tenant_id or "default") and row.get("action_group_id") in wanted
    ]
    rows.sort(key=lambda row: row.get("tested_at", ""), reverse=True)
    return rows[: max(1, min(limit, 200))]
