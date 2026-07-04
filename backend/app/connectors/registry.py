"""Runtime registry of configured connectors (admin-managed, encrypted at rest).

Mirrors the Azure-connections registry: a JSON file under backend/.data, secrets
encrypted via app.core.crypto, public views masked. Each saved connector has a `type`
(teams/outlook/jira/grafana), a `mode`, and the mode's config fields.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.connectors import (
    email,
    grafana,
    jira,
    logicapp,
    outlook,
    pagerduty,
    s3,
    securityhub,
    servicebus,
    servicenow,
    slack,
    splunk,
    sqs,
    teams,
    webhook,
    xsoar,
)
from app.connectors import crowdstrike_ngsiem, sumologic
from app.connectors.base import ConnectorToolset, ConnectorType
from app.core.crypto import decrypt, encrypt

_PATH = Path(__file__).resolve().parents[2] / ".data" / "connectors.json"

# All connector types the app knows about, keyed by id.
CONNECTOR_TYPES: dict[str, ConnectorType] = {
    teams.CONNECTOR.id: teams.CONNECTOR,
    outlook.CONNECTOR.id: outlook.CONNECTOR,
    email.CONNECTOR.id: email.CONNECTOR,
    jira.CONNECTOR.id: jira.CONNECTOR,
    servicenow.CONNECTOR.id: servicenow.CONNECTOR,
    grafana.CONNECTOR.id: grafana.CONNECTOR,
    slack.CONNECTOR.id: slack.CONNECTOR,
    webhook.CONNECTOR.id: webhook.CONNECTOR,
    pagerduty.CONNECTOR.id: pagerduty.CONNECTOR,
    splunk.CONNECTOR.id: splunk.CONNECTOR,
    xsoar.CONNECTOR.id: xsoar.CONNECTOR,
    sqs.CONNECTOR.id: sqs.CONNECTOR,
    s3.CONNECTOR.id: s3.CONNECTOR,
    securityhub.CONNECTOR.id: securityhub.CONNECTOR,
    servicebus.CONNECTOR.id: servicebus.CONNECTOR,
    logicapp.CONNECTOR.id: logicapp.CONNECTOR,
    sumologic.CONNECTOR.id: sumologic.CONNECTOR,
    crowdstrike_ngsiem.CONNECTOR.id: crowdstrike_ngsiem.CONNECTOR,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _migrate(data)
        except (json.JSONDecodeError, OSError):
            pass
    return {"connectors": {}}


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """One-time migrations applied on read. The SMTP transport moved out of the Outlook
    connector into its own "Email" connector, so re-tag any legacy outlook/smtp records."""
    changed = False
    for conn in data.get("connectors", {}).values():
        if conn.get("type") == "outlook" and conn.get("mode") == "smtp":
            conn["type"] = "email"
            changed = True
    if changed:
        _write(data)
    return data


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _secret_keys(type_id: str, mode: str) -> set[str]:
    ct = CONNECTOR_TYPES.get(type_id)
    if not ct:
        return set()
    return {f.key for f in ct.modes.get(mode, []) if f.secret}


def list_connectors() -> list[dict[str, Any]]:
    """All connectors with secrets DECRYPTED (internal/server use only)."""
    data = _read()
    out: list[dict[str, Any]] = []
    for cid, conn in data.get("connectors", {}).items():
        merged = dict(conn)
        merged["id"] = cid
        for key in _secret_keys(merged.get("type", ""), merged.get("mode", "")):
            if key in merged:
                merged[key] = decrypt(merged.get(key, ""))
        out.append(merged)
    out.sort(key=lambda c: (c.get("type", ""), c.get("name", "").lower()))
    return out


def get_connector(connector_id: str) -> dict[str, Any] | None:
    if not connector_id:
        return None
    for c in list_connectors():
        if c["id"] == connector_id:
            return c
    return None


def enabled_connectors() -> list[dict[str, Any]]:
    return [c for c in list_connectors() if not c.get("disabled")]


def upsert_connector(conn: dict[str, Any]) -> dict[str, Any]:
    """Create/update a connector. Secrets encrypted; blank secret keeps prior value."""
    data = _read()
    connectors = data.setdefault("connectors", {})
    cid = conn.get("id") or str(uuid.uuid4())
    existing = connectors.get(cid, {})
    type_id = conn.get("type") or existing.get("type", "")
    mode = conn.get("mode") or existing.get("mode", "")

    merged: dict[str, Any] = dict(existing)
    # Apply known top-level fields.
    for key in ("name", "type", "mode", "disabled", "config"):
        if key in conn and conn[key] is not None:
            merged[key] = conn[key]
    # Flatten the per-mode config fields onto the record, encrypting secrets.
    secret_keys = _secret_keys(type_id, mode)
    incoming_cfg = conn.get("config") or {}
    ct = CONNECTOR_TYPES.get(type_id)
    field_keys = {f.key for f in (ct.modes.get(mode, []) if ct else [])}
    for key in field_keys:
        if key not in incoming_cfg:
            continue
        val = incoming_cfg[key]
        if key in secret_keys:
            merged[key] = encrypt(val) if val else existing.get(key, "")
        else:
            merged[key] = val
    merged.pop("config", None)

    merged["type"] = type_id
    merged["mode"] = mode
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    connectors[cid] = merged
    _write(data)
    result = get_connector(cid)
    assert result is not None
    return result


def delete_connector(connector_id: str) -> bool:
    data = _read()
    if connector_id in data.get("connectors", {}):
        del data["connectors"][connector_id]
        _write(data)
        return True
    return False


def public_connector(conn: dict[str, Any]) -> dict[str, Any]:
    """A single connector safe for the UI: secret fields replaced by has_/hint flags."""
    type_id = conn.get("type", "")
    mode = conn.get("mode", "")
    ct = CONNECTOR_TYPES.get(type_id)
    fields = ct.modes.get(mode, []) if ct else []
    cfg: dict[str, Any] = {}
    for f in fields:
        raw = conn.get(f.key, "")
        if f.secret:
            cfg[f.key] = ""
            cfg[f"{f.key}_set"] = bool(raw)
        else:
            cfg[f.key] = raw
    return {
        "id": conn["id"],
        "name": conn.get("name", ""),
        "type": type_id,
        "mode": mode,
        "disabled": bool(conn.get("disabled", False)),
        "status": conn.get("status", "unknown"),
        "status_detail": conn.get("status_detail", ""),
        "config": cfg,
        "created_at": conn.get("created_at", ""),
        "updated_at": conn.get("updated_at", ""),
    }


def public_connectors() -> list[dict[str, Any]]:
    return [public_connector(c) for c in list_connectors()]


def update_status(connector_id: str, status: str, detail: str = "") -> None:
    data = _read()
    if connector_id in data.get("connectors", {}):
        data["connectors"][connector_id]["status"] = status
        data["connectors"][connector_id]["status_detail"] = detail
        _write(data)


def connector_types_public() -> list[dict[str, Any]]:
    """Type metadata for the setup UI (labels, modes, field specs)."""
    out: list[dict[str, Any]] = []
    for ct in CONNECTOR_TYPES.values():
        out.append(
            {
                "id": ct.id,
                "label": ct.label,
                "description": ct.description,
                "modes": {
                    mode: [
                        {
                            "key": f.key,
                            "label": f.label,
                            "type": f.type,
                            "placeholder": f.placeholder,
                            "secret": f.secret,
                            "optional": f.optional,
                            "help": f.help,
                            "options": f.options,
                        }
                        for f in fields
                    ]
                    for mode, fields in ct.modes.items()
                },
            }
        )
    return out


def build_toolset(
    allowed_tool_names: list[str] | None = None, *, include_connectors: bool = True
) -> ConnectorToolset:
    """Build a ConnectorToolset from the enabled connectors plus first-party built-in
    tools (network diagnostics + web fetch).

    If ``allowed_tool_names`` is given, only those tools are included (used to scope a
    custom agent to a chosen subset of tools). Set ``include_connectors=False`` to get a
    builtins-only toolset (used for the default assistant, which doesn't get external
    connectors but should still have the read-only utility tools)."""
    toolset = ConnectorToolset()
    allow = set(allowed_tool_names) if allowed_tool_names is not None else None
    if include_connectors:
        for conn in enabled_connectors():
            ct = CONNECTOR_TYPES.get(conn.get("type", ""))
            if not ct:
                continue
            tools = ct.build_tools(conn)
            if allow is not None:
                tools = [t for t in tools if t.name in allow]
            if tools:
                toolset.add_connector(conn, tools)
    # Built-in tools are first-party (no connector config) — register under an empty conn.
    from app.agent.builtins import builtin_tools

    builtins = builtin_tools(allowed_tool_names)
    if builtins:
        toolset.add_connector({}, builtins)
    return toolset


def all_tool_names() -> list[dict[str, str]]:
    """Every tool offered by every enabled connector (for the agent tool picker)."""
    out: list[dict[str, str]] = []
    for conn in enabled_connectors():
        ct = CONNECTOR_TYPES.get(conn.get("type", ""))
        if not ct:
            continue
        for t in ct.build_tools(conn):
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "kind": t.kind,
                    "connector_id": conn["id"],
                    "connector_name": conn.get("name", ""),
                    "connector_type": ct.id,
                }
            )
    # First-party built-in tools (network diagnostics + web fetch), if enabled.
    from app.agent.builtins import builtin_tools

    for t in builtin_tools():
        out.append(
            {
                "name": t.name,
                "description": t.description,
                "kind": t.kind,
                "connector_id": "builtin",
                "connector_name": "Built-in tools",
                "connector_type": "builtin",
            }
        )
    return out
