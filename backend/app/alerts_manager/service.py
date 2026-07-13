"""Azure-backed Alerts Manager services.

The inventory stays live in Azure.  Only approval/change metadata and encrypted before/after
payloads are persisted locally.  This prevents a second configuration database becoming the
source of truth while still providing audit, rollback, and optimistic-concurrency controls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.alerts_manager import cache as inventory_cache
from app.core.crypto import decrypt, encrypt

_ACTION_GROUP_API = "2023-01-01"
_ALERTS_API = "2019-05-05-preview"
_ALERT_TYPES = (
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.alertsmanagement/prometheusrulegroups",
)
_RECEIVER_KEYS = (
    "emailReceivers",
    "smsReceivers",
    "webhookReceivers",
    "armRoleReceivers",
    "voiceReceivers",
    "azureAppPushReceivers",
    "azureFunctionReceivers",
    "logicAppReceivers",
    "eventHubReceivers",
    "automationRunbookReceivers",
    "itsmReceivers",
)
_ALL_RECEIVER_KEYS = _RECEIVER_KEYS
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+?\d[\d ()-]{7,}\d")
_AG_ID_RE = re.compile(
    r"/subscriptions/[^\s\"']+/resourcegroups/[^\s\"']+/providers/microsoft\.insights/actiongroups/[^\s\"'/?]+",
    re.I,
)
_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_RESOURCE_ID_RE = re.compile(r"/subscriptions/[^/]+/resourcegroups/[^/]+/providers/[^/]+/.+", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    text = json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encrypted_json(value: Any) -> str:
    return encrypt(json.dumps(value or {}, separators=(",", ":"), ensure_ascii=True))


def decrypted_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(decrypt(value) or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def safe_error(value: str | None) -> str:
    """Remove signed URL query strings from an Azure error."""
    text = str(value or "")[:1500]
    text = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']+", r"\1?<redacted>", text)
    return text


def _display_email(value: str) -> str:
    return value or "configured email"


def _display_phone(value: str) -> str:
    return value or "configured phone"


def _masked_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return parts.hostname or "configured endpoint"
    except ValueError:
        return "configured endpoint"


def _editable_url(value: str) -> str:
    """Never return signed query strings or embedded credentials to the browser."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if not host:
            return ""
        netloc = host
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return ""


def _safe_url_receiver(item: dict[str, Any], *, url_field: str, resource_fields: dict[str, str]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or ""),
        **{target: str(item.get(source) or "") for target, source in resource_fields.items()},
        "endpoint": _editable_url(str(item.get(url_field) or "")),
        "preserve_secret": bool(item.get(url_field)),
        "use_common_alert_schema": bool(item.get("useCommonAlertSchema", True)),
    }


def _receiver_summary(props: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mapping = {
        "emailReceivers": ("email", "emailAddress", _display_email),
        "smsReceivers": ("sms", "phoneNumber", _display_phone),
        "webhookReceivers": ("webhook", "serviceUri", _masked_url),
        "armRoleReceivers": ("armrole", "roleId", lambda value: f"…{value[-12:]}"),
        "voiceReceivers": ("voice", "phoneNumber", _display_phone),
        "azureAppPushReceivers": ("azureapppush", "emailAddress", _display_email),
        "azureFunctionReceivers": ("azurefunction", "functionAppResourceId", lambda value: f"…{value.rstrip('/').split('/')[-1] if value else 'configured'}"),
        "logicAppReceivers": ("logicapp", "resourceId", lambda value: f"…{value.rstrip('/').split('/')[-1] if value else 'configured'}"),
        "eventHubReceivers": ("eventhub", "eventHubName", lambda value: value or "configured event hub"),
        "automationRunbookReceivers": ("automationrunbook", "runbookName", lambda value: value or "configured runbook"),
        "itsmReceivers": ("itsm", "workspaceId", lambda _value: "configured ITSM"),
    }
    for key, (kind, field, masker) in mapping.items():
        for receiver in props.get(key) or []:
            if not isinstance(receiver, dict):
                continue
            display_destination = str(receiver.get(field) or "")
            destination = display_destination
            if key == "azureFunctionReceivers":
                destination = f"{receiver.get('functionAppResourceId') or ''}:{receiver.get('functionName') or ''}"
            elif key == "logicAppReceivers":
                destination = str(receiver.get("resourceId") or "")
            elif key == "eventHubReceivers":
                destination = f"{receiver.get('subscriptionId') or ''}:{receiver.get('eventHubNameSpace') or ''}:{receiver.get('eventHubName') or ''}"
            elif key == "automationRunbookReceivers":
                destination = f"{receiver.get('automationAccountId') or ''}:{receiver.get('runbookName') or ''}:{receiver.get('webhookResourceId') or ''}"
            elif key == "itsmReceivers":
                destination = f"{receiver.get('workspaceId') or ''}:{receiver.get('connectionId') or ''}"
            fingerprint = hashlib.sha256(f"{kind}:{destination.lower()}".encode()).hexdigest()[:12]
            displayed = masker(display_destination)
            rows.append(
                {
                    "type": kind,
                    "name": str(receiver.get("name") or ""),
                    "destination": displayed,
                    "masked": displayed,
                    "fingerprint": fingerprint,
                    "enabled": str(receiver.get("status") or "Enabled").lower() != "disabled",
                    "use_common_alert_schema": bool(receiver.get("useCommonAlertSchema", False)),
                }
            )
    return rows


def _resource_body(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "location": resource.get("location") or "Global",
        "tags": resource.get("tags") or {},
        "properties": resource.get("properties") or {},
    }


def _subscription_from_id(resource_id: str) -> str:
    parts = resource_id.strip("/").split("/")
    try:
        return parts[parts.index("subscriptions") + 1]
    except (ValueError, IndexError):
        return ""


def _resource_group_from_id(resource_id: str) -> str:
    parts = resource_id.strip("/").split("/")
    lower = [part.lower() for part in parts]
    try:
        return parts[lower.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return ""


def _name_from_id(resource_id: str) -> str:
    return resource_id.rstrip("/").rsplit("/", 1)[-1]


def _action_group_ids(value: Any) -> set[str]:
    text = json.dumps(value or {}, separators=(",", ":"))
    return {match.group(0).lower() for match in _AG_ID_RE.finditer(text)}


def _workload_context(workload_id: str | None) -> tuple[dict[str, Any] | None, set[str], set[str]]:
    if not workload_id:
        return None, set(), set()
    from app.workloads.registry import get_workload

    workload = get_workload(workload_id)
    ids: set[str] = set()
    subscriptions: set[str] = set()
    for node in (workload or {}).get("nodes", []):
        rid = str(node.get("id") or "")
        if rid:
            ids.add(rid.lower())
        sub = str(node.get("subscription_id") or _subscription_from_id(rid))
        if sub:
            subscriptions.add(sub)
    return workload, ids, subscriptions


def resolve_selected_connection(connection_id: str | None, workload_id: str | None = None) -> dict[str, Any]:
    from app.core.azure_connections import connection_for_scope, resolve_connection

    workload, _ids, _subs = _workload_context(workload_id)
    connection = (
        connection_for_scope("workload", connection_id=connection_id, workload=workload)
        if workload_id
        else resolve_connection(connection_id)
    )
    if not connection:
        raise ValueError("No Azure connection is configured for this scope.")
    if connection.get("disabled"):
        raise ValueError("The selected Azure connection is disabled.")
    return connection


def assert_writable(connection: dict[str, Any]) -> None:
    if connection.get("read_only", True):
        raise PermissionError("The selected Azure connection is read-only.")


async def _token(connection: dict[str, Any]) -> str:
    from app.azure.credentials import get_arm_token

    token, error = await get_arm_token(connection)
    if not token:
        raise ValueError(safe_error(error or "Could not acquire an ARM token."))
    return token


async def _arg(
    connection: dict[str, Any], query: str, subscriptions: set[str] | None = None, *, max_rows: int = 5000,
    with_metadata: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.azure.arm import query_resource_graph_paged

    token = await _token(connection)
    rows, error, complete, total = await query_resource_graph_paged(
        token, query, sorted(subscriptions or set()) or None, max_rows=max_rows
    )
    if error:
        raise ValueError(safe_error(error))
    metadata = {
        "partial": not complete,
        "truncated": not complete,
        "source_total": total,
        "source_count": len(rows),
        "source_limit": max_rows,
    }
    return (rows, metadata) if with_metadata else rows


def _arg_rows_and_metadata(
    result: list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]], *, max_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Accept legacy/monkeypatched row-only ARG results while exposing completeness."""
    if isinstance(result, tuple):
        return result
    return result, {
        "partial": False, "truncated": False, "source_total": len(result),
        "source_count": len(result), "source_limit": max_rows,
    }


async def get_arm_resource(connection: dict[str, Any], resource_id: str, api_version: str = _ACTION_GROUP_API) -> tuple[dict[str, Any] | None, int, str]:
    from app.azure.arm import arm_write

    token = await _token(connection)
    data, error, status = await arm_write(token, "GET", resource_id, api_version=api_version)
    return (data if isinstance(data, dict) else None), status, safe_error(error)


async def list_fired_alerts(
    connection: dict[str, Any], *, workload_id: str | None = None, subscription_id: str | None = None,
    management_group_id: str | None = None, days: int = 30, states: set[str] | None = None,
    tenant_id: str = "", with_metadata: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_days = max(1, min(days, 90))
    normalized_states = tuple(sorted(state.strip().lower() for state in (states or set()) if state.strip()))
    key = inventory_cache.inventory_key(
        "fired_alerts", connection, tenant_id=tenant_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
        dimensions=(normalized_days, normalized_states),
    )

    async def load() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return await _list_fired_alerts_uncached(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, days=normalized_days, states=set(normalized_states),
        )

    rows, metadata = await inventory_cache.get_or_create(key, load)
    return (rows, metadata) if with_metadata else rows


async def _list_fired_alerts_uncached(
    connection: dict[str, Any], *, workload_id: str | None, subscription_id: str | None,
    management_group_id: str | None, days: int, states: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _workload, workload_ids, workload_subs = _workload_context(workload_id)
    subscriptions = {subscription_id} if subscription_id else workload_subs
    if management_group_id:
        from app.workloads.discovery import subscriptions_under_mg

        subscriptions = set(await subscriptions_under_mg(connection, management_group_id))
    query = """
alertsmanagementresources
| where type =~ 'microsoft.alertsmanagement/alerts'
| project id, name, subscriptionId, properties
"""
    rows, metadata = _arg_rows_and_metadata(
        await _arg(connection, query, subscriptions, max_rows=5000, with_metadata=True), max_rows=5000,
    )
    cutoff = now().timestamp() - max(1, min(days, 90)) * 86400
    wanted_states = {state.lower() for state in states}
    out: list[dict[str, Any]] = []
    for row in rows:
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        essentials = props.get("essentials") if isinstance(props.get("essentials"), dict) else {}
        target_ids = [str(value) for value in (essentials.get("alertTargetIDs") or [])]
        if workload_ids and not any(
            any(target.lower() == wid or target.lower().startswith(wid.rstrip("/") + "/") for wid in workload_ids)
            for target in target_ids
        ):
            continue
        fired_raw = str(essentials.get("startDateTime") or essentials.get("firedDateTime") or "")
        try:
            fired_at = datetime.fromisoformat(fired_raw.replace("Z", "+00:00"))
            if fired_at.timestamp() < cutoff:
                continue
        except ValueError:
            fired_at = None
        state = str(essentials.get("alertState") or "New")
        if wanted_states and state.lower() not in wanted_states:
            continue
        out.append(
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "rule_id": str(essentials.get("alertRule") or essentials.get("alertRuleId") or ""),
                "rule_name": str(essentials.get("alertRule") or ""),
                "severity": str(essentials.get("severity") or ""),
                "state": state,
                "monitor_condition": str(essentials.get("monitorCondition") or ""),
                "monitor_service": str(essentials.get("monitorService") or ""),
                "signal_type": str(essentials.get("signalType") or ""),
                "fired_at": fired_at.isoformat() if fired_at else fired_raw,
                "last_modified_at": str(essentials.get("lastModifiedDateTime") or ""),
                "description": str(essentials.get("description") or ""),
                "target_ids": target_ids,
                "subscription_id": str(row.get("subscriptionId") or ""),
            }
        )
    out.sort(key=lambda item: item.get("fired_at") or "", reverse=True)
    metadata["normalized_count"] = len(out)
    return out, metadata


async def fired_alert_history(connection: dict[str, Any], alert_id: str) -> dict[str, Any]:
    from app.azure.arm import arm_write

    token = await _token(connection)
    data, error, status = await arm_write(token, "GET", f"{alert_id.rstrip('/')}/history", api_version=_ALERTS_API)
    if error:
        raise ValueError(safe_error(error))
    return {"status": status, "history": data or {}}


async def change_fired_alert_state(connection: dict[str, Any], alert_id: str, new_state: str) -> dict[str, Any]:
    from app.azure.arm import arm_write

    assert_writable(connection)
    normalized = new_state.strip().lower()
    values = {"new": "New", "acknowledged": "Acknowledged", "closed": "Closed"}
    if normalized not in values:
        raise ValueError("State must be New, Acknowledged, or Closed.")
    token = await _token(connection)
    path = f"{alert_id.rstrip('/')}/changeState"
    data, error, status = await arm_write(
        token, "POST", path, api_version=_ALERTS_API, query={"newState": values[normalized]}
    )
    if error:
        raise ValueError(safe_error(error))
    return {"status": status, "alert": data or {}, "new_state": values[normalized]}


async def list_action_groups(
    connection: dict[str, Any], *, workload_id: str | None = None, subscription_id: str | None = None,
    management_group_id: str | None = None, tenant_id: str = "", with_metadata: bool = False,
    all_visible: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    key = inventory_cache.inventory_key(
        "action_groups", connection, tenant_id=tenant_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
        dimensions=("all_visible",) if all_visible else (),
    )

    async def load() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return await _list_action_groups_uncached(
            connection, workload_id=workload_id, subscription_id=subscription_id,
            management_group_id=management_group_id, all_visible=all_visible,
        )

    rows, metadata = await inventory_cache.get_or_create(key, load)
    return (rows, metadata) if with_metadata else rows


async def _list_action_groups_uncached(
    connection: dict[str, Any], *, workload_id: str | None, subscription_id: str | None,
    management_group_id: str | None, all_visible: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _workload, workload_ids, workload_subs = _workload_context(workload_id)
    subscriptions = {subscription_id} if subscription_id else workload_subs
    if all_visible:
        subscriptions = set()
    if management_group_id:
        from app.workloads.discovery import subscriptions_under_mg

        subscriptions = set(await subscriptions_under_mg(connection, management_group_id))
    quoted = ",".join(f"'{item}'" for item in _ALERT_TYPES)
    query = f"""
resources
| where type =~ 'microsoft.insights/actiongroups' or type in~ ({quoted})
| project id, name, type, subscriptionId, resourceGroup, location, tags, properties
"""
    rows, metadata = _arg_rows_and_metadata(
        await _arg(connection, query, subscriptions, max_rows=10000, with_metadata=True), max_rows=10000,
    )
    groups = [row for row in rows if str(row.get("type") or "").lower() == "microsoft.insights/actiongroups"]
    rules = [row for row in rows if str(row.get("type") or "").lower() != "microsoft.insights/actiongroups"]
    dependencies: dict[str, list[dict[str, str]]] = {}
    for rule in rules:
        props = rule.get("properties") or {}
        scopes = [str(value).lower() for value in (props.get("scopes") or props.get("scope") or [])]
        if isinstance(props.get("scope"), str):
            scopes = [str(props.get("scope")).lower()]
        if workload_ids and scopes and not any(
            any(scope == wid or scope.startswith(wid.rstrip("/") + "/") or wid.startswith(scope.rstrip("/") + "/") for wid in workload_ids)
            for scope in scopes
        ):
            continue
        for group_id in _action_group_ids(props):
            dependencies.setdefault(group_id, []).append(
                {"id": str(rule.get("id") or ""), "name": str(rule.get("name") or ""), "type": str(rule.get("type") or "")}
            )
    out: list[dict[str, Any]] = []
    for group in groups:
        props = group.get("properties") if isinstance(group.get("properties"), dict) else {}
        receivers = _receiver_summary(props)
        group_id = str(group.get("id") or "")
        deps = dependencies.get(group_id.lower(), [])
        out.append(
            {
                "id": group_id,
                "name": str(group.get("name") or ""),
                "subscription_id": str(group.get("subscriptionId") or _subscription_from_id(group_id)),
                "resource_group": str(group.get("resourceGroup") or _resource_group_from_id(group_id)),
                "location": str(group.get("location") or "Global"),
                "short_name": str(props.get("groupShortName") or ""),
                "enabled": bool(props.get("enabled", True)),
                "receivers": receivers,
                "receiver_count": len(receivers),
                "active_receiver_count": sum(1 for receiver in receivers if receiver["enabled"]),
                "dependencies": deps,
                "dependency_count": len(deps),
                "state_hash": canonical_hash(_resource_body(group)),
                "tags": group.get("tags") or {},
            }
        )
    out.sort(key=lambda item: item["name"].lower())
    metadata["normalized_count"] = len(out)
    return out, metadata


def editable_action_group(resource: dict[str, Any]) -> dict[str, Any]:
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    webhooks = []
    for receiver in props.get("webhookReceivers") or []:
        if not isinstance(receiver, dict):
            continue
        webhooks.append(
            {
                "name": str(receiver.get("name") or ""),
                "service_uri": _editable_url(str(receiver.get("serviceUri") or "")),
                "preserve_secret": True,
                "use_common_alert_schema": bool(receiver.get("useCommonAlertSchema", True)),
                "use_aad_auth": bool(receiver.get("useAadAuth", False)),
                "object_id": str(receiver.get("objectId") or ""),
                "identifier_uri": str(receiver.get("identifierUri") or ""),
                "tenant_id": str(receiver.get("tenantId") or ""),
            }
        )
    return {
        "id": str(resource.get("id") or ""),
        "name": str(resource.get("name") or _name_from_id(str(resource.get("id") or ""))),
        "subscription_id": _subscription_from_id(str(resource.get("id") or "")),
        "resource_group": _resource_group_from_id(str(resource.get("id") or "")),
        "location": str(resource.get("location") or "Global"),
        "short_name": str(props.get("groupShortName") or ""),
        "enabled": bool(props.get("enabled", True)),
        "email_receivers": [
            {
                "name": str(item.get("name") or ""),
                "email_address": str(item.get("emailAddress") or ""),
                "use_common_alert_schema": bool(item.get("useCommonAlertSchema", True)),
            }
            for item in props.get("emailReceivers") or [] if isinstance(item, dict)
        ],
        "sms_receivers": [
            {
                "name": str(item.get("name") or ""),
                "country_code": str(item.get("countryCode") or ""),
                "phone_number": str(item.get("phoneNumber") or ""),
            }
            for item in props.get("smsReceivers") or [] if isinstance(item, dict)
        ],
        "webhook_receivers": webhooks,
        "arm_role_receivers": [
            {
                "name": str(item.get("name") or ""),
                "role_id": str(item.get("roleId") or ""),
                "use_common_alert_schema": bool(item.get("useCommonAlertSchema", True)),
            }
            for item in props.get("armRoleReceivers") or [] if isinstance(item, dict)
        ],
        "voice_receivers": [
            {"name": str(item.get("name") or ""), "country_code": str(item.get("countryCode") or ""), "phone_number": str(item.get("phoneNumber") or "")}
            for item in props.get("voiceReceivers") or [] if isinstance(item, dict)
        ],
        "azure_app_push_receivers": [
            {"name": str(item.get("name") or ""), "email_address": str(item.get("emailAddress") or "")}
            for item in props.get("azureAppPushReceivers") or [] if isinstance(item, dict)
        ],
        "azure_function_receivers": [
            _safe_url_receiver(
                item, url_field="httpTriggerUrl",
                resource_fields={"function_app_resource_id": "functionAppResourceId", "function_name": "functionName"},
            )
            for item in props.get("azureFunctionReceivers") or [] if isinstance(item, dict)
        ],
        "logic_app_receivers": [
            _safe_url_receiver(item, url_field="callbackUrl", resource_fields={"resource_id": "resourceId"})
            for item in props.get("logicAppReceivers") or [] if isinstance(item, dict)
        ],
        "event_hub_receivers": [
            {
                "name": str(item.get("name") or ""), "subscription_id": str(item.get("subscriptionId") or ""),
                "resource_group": "", "tenant_id": str(item.get("tenantId") or ""), "namespace_name": str(item.get("eventHubNameSpace") or ""),
                "event_hub_name": str(item.get("eventHubName") or ""),
                "use_common_alert_schema": bool(item.get("useCommonAlertSchema", True)),
            }
            for item in props.get("eventHubReceivers") or [] if isinstance(item, dict)
        ],
        "automation_runbook_receivers": [
            {
                **_safe_url_receiver(
                    item, url_field="serviceUri",
                    resource_fields={"automation_account_id": "automationAccountId", "runbook_name": "runbookName", "webhook_resource_id": "webhookResourceId"},
                ),
                "is_global_runbook": bool(item.get("isGlobalRunbook", False)),
            }
            for item in props.get("automationRunbookReceivers") or [] if isinstance(item, dict)
        ],
        "itsm_receivers": [
            {
                "name": str(item.get("name") or ""), "workspace_id": str(item.get("workspaceId") or ""),
                "connection_id": str(item.get("connectionId") or ""), "region": str(item.get("region") or ""),
                "ticket_configuration": "", "preserve_configuration": bool(item.get("ticketConfiguration")),
            }
            for item in props.get("itsmReceivers") or [] if isinstance(item, dict)
        ],
        "tags": resource.get("tags") or {},
        "state_hash": canonical_hash(_resource_body(resource)),
        "advanced_receiver_count": sum(len(props.get(key) or []) for key in _RECEIVER_KEYS[4:]),
    }


def _validate_https_endpoint(item: dict[str, Any], label: str) -> list[str]:
    uri = str(item.get("endpoint") or "")
    try:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
    except ValueError:
        return [f"{label} {item.get('name') or 'unnamed'} requires an HTTPS endpoint without embedded credentials."]
    return []


def validate_action_group_payload(payload: dict[str, Any], *, create: bool) -> list[str]:
    errors: list[str] = []
    name = str(payload.get("name") or "").strip()
    if create and not name:
        errors.append("Action group name is required.")
    if create and not str(payload.get("subscription_id") or "").strip():
        errors.append("Subscription is required.")
    if create and not str(payload.get("resource_group") or "").strip():
        errors.append("Resource group is required.")
    short_name = str(payload.get("short_name") or "").strip()
    if not short_name or len(short_name) > 12:
        errors.append("Short name is required and must be at most 12 characters.")
    names: set[str] = set()
    receiver_fields = (
        "email_receivers", "sms_receivers", "webhook_receivers", "arm_role_receivers", "voice_receivers",
        "azure_app_push_receivers", "azure_function_receivers", "logic_app_receivers", "event_hub_receivers",
        "automation_runbook_receivers", "itsm_receivers",
    )
    for key in receiver_fields:
        for item in payload.get(key) or []:
            receiver_name = str((item or {}).get("name") or "").strip()
            if not receiver_name:
                errors.append(f"Every {key.replace('_', ' ')} entry needs a name.")
            elif receiver_name.lower() in names:
                errors.append("Receiver names must be unique across the action group.")
            names.add(receiver_name.lower())
    for item in payload.get("email_receivers") or []:
        if not _EMAIL_RE.fullmatch(str((item or {}).get("email_address") or "")):
            errors.append(f"Invalid email receiver: {(item or {}).get('name') or 'unnamed'}.")
    for item in [*(payload.get("sms_receivers") or []), *(payload.get("voice_receivers") or [])]:
        if not str((item or {}).get("country_code") or "").isdigit() or len(re.sub(r"\D", "", str((item or {}).get("phone_number") or ""))) < 7:
            errors.append(f"Invalid phone receiver: {(item or {}).get('name') or 'unnamed'}.")
    for item in payload.get("azure_app_push_receivers") or []:
        if not _EMAIL_RE.fullmatch(str((item or {}).get("email_address") or "")):
            errors.append(f"Invalid Azure app push receiver: {(item or {}).get('name') or 'unnamed'}.")
    for item in payload.get("webhook_receivers") or []:
        if not item.get("preserve_secret"):
            uri = str((item or {}).get("service_uri") or "")
            try:
                parts = urlsplit(uri)
                if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                    raise ValueError
            except ValueError:
                errors.append(f"Webhook {(item or {}).get('name') or 'unnamed'} must use an HTTPS URL without embedded credentials.")
        if item.get("use_aad_auth") and (
            not _GUID_RE.fullmatch(str(item.get("object_id") or ""))
            or not _GUID_RE.fullmatch(str(item.get("tenant_id") or ""))
            or not str(item.get("identifier_uri") or "").strip()
        ):
            errors.append(f"Secure webhook {(item or {}).get('name') or 'unnamed'} requires object, tenant, and identifier values.")
    for item in payload.get("arm_role_receivers") or []:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str((item or {}).get("role_id") or "")):
            errors.append(f"Invalid ARM role receiver: {(item or {}).get('name') or 'unnamed'}.")
    for item in payload.get("azure_function_receivers") or []:
        resource_id = str(item.get("function_app_resource_id") or "")
        if not _RESOURCE_ID_RE.fullmatch(resource_id) or "/providers/microsoft.web/sites/" not in resource_id.lower():
            errors.append(f"Azure Function {(item or {}).get('name') or 'unnamed'} requires a Function App resource ID.")
        if not str(item.get("function_name") or "").strip():
            errors.append(f"Azure Function {(item or {}).get('name') or 'unnamed'} requires a function name.")
        if not item.get("preserve_secret"):
            errors.extend(_validate_https_endpoint(item, "Azure Function"))
    for item in payload.get("logic_app_receivers") or []:
        resource_id = str(item.get("resource_id") or "")
        if not _RESOURCE_ID_RE.fullmatch(resource_id) or "/providers/microsoft.logic/workflows/" not in resource_id.lower():
            errors.append(f"Logic App {(item or {}).get('name') or 'unnamed'} requires a workflow resource ID.")
        if not item.get("preserve_secret"):
            errors.extend(_validate_https_endpoint(item, "Logic App"))
    for item in payload.get("event_hub_receivers") or []:
        if not _GUID_RE.fullmatch(str(item.get("subscription_id") or "")):
            errors.append(f"Event Hub {(item or {}).get('name') or 'unnamed'} requires a subscription GUID.")
        if item.get("tenant_id") and not _GUID_RE.fullmatch(str(item.get("tenant_id"))):
            errors.append(f"Event Hub {(item or {}).get('name') or 'unnamed'} has an invalid tenant GUID.")
        if not str(item.get("namespace_name") or "").strip() or not str(item.get("event_hub_name") or "").strip():
            errors.append(f"Event Hub {(item or {}).get('name') or 'unnamed'} requires namespace and hub names.")
    for item in payload.get("automation_runbook_receivers") or []:
        account_id = str(item.get("automation_account_id") or "")
        webhook_id = str(item.get("webhook_resource_id") or "")
        if not _RESOURCE_ID_RE.fullmatch(account_id) or "/providers/microsoft.automation/automationaccounts/" not in account_id.lower():
            errors.append(f"Automation receiver {(item or {}).get('name') or 'unnamed'} requires an Automation Account resource ID.")
        if not webhook_id.startswith(account_id.rstrip("/") + "/") or "/webhooks/" not in webhook_id.lower():
            errors.append(f"Automation receiver {(item or {}).get('name') or 'unnamed'} requires a webhook resource ID under the Automation Account.")
        if not str(item.get("runbook_name") or "").strip():
            errors.append(f"Automation receiver {(item or {}).get('name') or 'unnamed'} requires a runbook name.")
        if not item.get("preserve_secret"):
            errors.extend(_validate_https_endpoint(item, "Automation receiver"))
    itsm_regions = {"centralindia", "japaneast", "southeastasia", "australiasoutheast", "uksouth", "westcentralus", "canadacentral", "eastus", "westeurope"}
    for item in payload.get("itsm_receivers") or []:
        if not _GUID_RE.fullmatch(str(item.get("workspace_id") or "")) or not str(item.get("connection_id") or "").strip():
            errors.append(f"ITSM receiver {(item or {}).get('name') or 'unnamed'} requires workspace and connection IDs.")
        if str(item.get("region") or "").lower() not in itsm_regions:
            errors.append(f"ITSM receiver {(item or {}).get('name') or 'unnamed'} uses an unsupported region.")
        if not item.get("preserve_configuration"):
            try:
                parsed = json.loads(str(item.get("ticket_configuration") or ""))
                if not isinstance(parsed, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                errors.append(f"ITSM receiver {(item or {}).get('name') or 'unnamed'} requires JSON ticket configuration.")
    return list(dict.fromkeys(errors))


def build_action_group_body(payload: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    before_props = (before or {}).get("properties") if isinstance((before or {}).get("properties"), dict) else {}
    props: dict[str, Any] = {
        key: json.loads(json.dumps(value))
        for key, value in before_props.items()
        if key not in _RECEIVER_KEYS and key not in {"enabled", "groupShortName", "provisioningState"}
    }
    props["groupShortName"] = str(payload.get("short_name") or "").strip()
    props["enabled"] = bool(payload.get("enabled", True))
    props["emailReceivers"] = [
        {
            "name": str(item.get("name") or "").strip(),
            "emailAddress": str(item.get("email_address") or "").strip(),
            "useCommonAlertSchema": bool(item.get("use_common_alert_schema", True)),
        }
        for item in payload.get("email_receivers") or []
    ]
    props["smsReceivers"] = [
        {
            "name": str(item.get("name") or "").strip(),
            "countryCode": str(item.get("country_code") or "").strip(),
            "phoneNumber": re.sub(r"\D", "", str(item.get("phone_number") or "")),
        }
        for item in payload.get("sms_receivers") or []
    ]
    previous_webhooks = {
        str(item.get("name") or "").lower(): item for item in before_props.get("webhookReceivers") or [] if isinstance(item, dict)
    }
    webhooks: list[dict[str, Any]] = []
    for item in payload.get("webhook_receivers") or []:
        name = str(item.get("name") or "").strip()
        previous = previous_webhooks.get(name.lower(), {})
        uri = str(previous.get("serviceUri") or "") if item.get("preserve_secret") else str(item.get("service_uri") or "").strip()
        receiver: dict[str, Any] = {
            "name": name,
            "serviceUri": uri,
            "useCommonAlertSchema": bool(item.get("use_common_alert_schema", True)),
        }
        if item.get("use_aad_auth"):
            receiver.update(
                {
                    "useAadAuth": True,
                    "objectId": str(item.get("object_id") or ""),
                    "identifierUri": str(item.get("identifier_uri") or ""),
                    "tenantId": str(item.get("tenant_id") or ""),
                }
            )
        webhooks.append(receiver)
    props["webhookReceivers"] = webhooks
    props["armRoleReceivers"] = [
        {
            "name": str(item.get("name") or "").strip(),
            "roleId": str(item.get("role_id") or "").strip(),
            "useCommonAlertSchema": bool(item.get("use_common_alert_schema", True)),
        }
        for item in payload.get("arm_role_receivers") or []
    ]
    props["voiceReceivers"] = [
        {"name": str(item.get("name") or "").strip(), "countryCode": str(item.get("country_code") or "").strip(), "phoneNumber": re.sub(r"\D", "", str(item.get("phone_number") or ""))}
        for item in payload.get("voice_receivers") or []
    ]
    props["azureAppPushReceivers"] = [
        {"name": str(item.get("name") or "").strip(), "emailAddress": str(item.get("email_address") or "").strip()}
        for item in payload.get("azure_app_push_receivers") or []
    ]

    def endpoint_receivers(payload_key: str, azure_key: str, url_field: str, field_map: dict[str, str]) -> list[dict[str, Any]]:
        previous_by_name = {
            str(item.get("name") or "").lower(): item
            for item in before_props.get(azure_key) or [] if isinstance(item, dict)
        }
        result: list[dict[str, Any]] = []
        for item in payload.get(payload_key) or []:
            name = str(item.get("name") or "").strip()
            previous = previous_by_name.get(name.lower(), {})
            endpoint = str(previous.get(url_field) or "") if item.get("preserve_secret") else str(item.get("endpoint") or "").strip()
            receiver = {"name": name, url_field: endpoint, "useCommonAlertSchema": bool(item.get("use_common_alert_schema", True))}
            receiver.update({azure_field: item.get(payload_field) for payload_field, azure_field in field_map.items()})
            result.append(receiver)
        return result

    props["azureFunctionReceivers"] = endpoint_receivers(
        "azure_function_receivers", "azureFunctionReceivers", "httpTriggerUrl",
        {"function_app_resource_id": "functionAppResourceId", "function_name": "functionName"},
    )
    props["logicAppReceivers"] = endpoint_receivers(
        "logic_app_receivers", "logicAppReceivers", "callbackUrl", {"resource_id": "resourceId"},
    )
    props["eventHubReceivers"] = [
        {
            "name": str(item.get("name") or "").strip(), "subscriptionId": str(item.get("subscription_id") or "").strip(),
            "eventHubNameSpace": str(item.get("namespace_name") or "").strip(), "eventHubName": str(item.get("event_hub_name") or "").strip(),
            "useCommonAlertSchema": bool(item.get("use_common_alert_schema", True)),
            **({"tenantId": str(item.get("tenant_id"))} if item.get("tenant_id") else {}),
        }
        for item in payload.get("event_hub_receivers") or []
    ]
    runbooks = endpoint_receivers(
        "automation_runbook_receivers", "automationRunbookReceivers", "serviceUri",
        {"automation_account_id": "automationAccountId", "runbook_name": "runbookName", "webhook_resource_id": "webhookResourceId"},
    )
    for receiver, item in zip(runbooks, payload.get("automation_runbook_receivers") or []):
        receiver["isGlobalRunbook"] = bool(item.get("is_global_runbook", False))
    props["automationRunbookReceivers"] = runbooks
    previous_itsm = {
        str(item.get("name") or "").lower(): item
        for item in before_props.get("itsmReceivers") or [] if isinstance(item, dict)
    }
    props["itsmReceivers"] = [
        {
            "name": str(item.get("name") or "").strip(), "workspaceId": str(item.get("workspace_id") or "").strip(),
            "connectionId": str(item.get("connection_id") or "").strip(), "region": str(item.get("region") or "").strip().lower(),
            "ticketConfiguration": str((previous_itsm.get(str(item.get("name") or "").lower()) or {}).get("ticketConfiguration") or "")
            if item.get("preserve_configuration") else str(item.get("ticket_configuration") or "").strip(),
        }
        for item in payload.get("itsm_receivers") or []
    ]
    return {
        "location": str(payload.get("location") or (before or {}).get("location") or "Global"),
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else (before or {}).get("tags") or {},
        "properties": props,
    }


def summarize_action_group_body(body: dict[str, Any]) -> dict[str, Any]:
    props = body.get("properties") if isinstance(body.get("properties"), dict) else {}
    return {
        "enabled": bool(props.get("enabled", True)),
        "location": str(body.get("location") or "Global"),
        "short_name": str(props.get("groupShortName") or ""),
        "email_receivers": len(props.get("emailReceivers") or []),
        "sms_receivers": len(props.get("smsReceivers") or []),
        "webhook_receivers": len(props.get("webhookReceivers") or []),
        "arm_role_receivers": len(props.get("armRoleReceivers") or []),
        "voice_receivers": len(props.get("voiceReceivers") or []),
        "azure_app_push_receivers": len(props.get("azureAppPushReceivers") or []),
        "azure_function_receivers": len(props.get("azureFunctionReceivers") or []),
        "logic_app_receivers": len(props.get("logicAppReceivers") or []),
        "event_hub_receivers": len(props.get("eventHubReceivers") or []),
        "automation_runbook_receivers": len(props.get("automationRunbookReceivers") or []),
        "itsm_receivers": len(props.get("itsmReceivers") or []),
        "advanced_receivers": sum(len(props.get(key) or []) for key in _RECEIVER_KEYS[4:]),
    }


async def apply_action_group_change(connection: dict[str, Any], change: Any) -> tuple[dict[str, Any] | None, int, str]:
    from app.azure.arm import arm_write

    assert_writable(connection)
    token = await _token(connection)
    desired = decrypted_json(change.desired_encrypted)
    payload = desired.get("payload") if isinstance(desired.get("payload"), dict) else {}
    before = decrypted_json(change.before_encrypted)
    if change.operation != "create":
        live, status, error = await get_arm_resource(connection, change.target_id)
        if error or not live:
            return None, status, error or "The action group no longer exists."
        if canonical_hash(_resource_body(live)) != change.expected_state_hash:
            return None, 409, "Azure state changed after this request was reviewed. Refresh and create a new change."
        before = live
    elif change.operation == "create":
        live, status, _error = await get_arm_resource(connection, change.target_id)
        if live:
            return None, 409, "An action group with this name already exists."
        if status not in (0, 404):
            return None, status, "Could not verify that the action group name is available."
    if change.operation == "delete":
        _data, error, status = await arm_write(token, "DELETE", change.target_id, api_version=_ACTION_GROUP_API)
        return ({} if not error else None), status, safe_error(error)
    body = desired.get("body") if isinstance(desired.get("body"), dict) else {}
    if change.operation == "create" and payload:
        props = body.get("properties") if isinstance(body.get("properties"), dict) else {}
        endpoint_fields = {
            "webhookReceivers": "serviceUri",
            "azureFunctionReceivers": "httpTriggerUrl",
            "logicAppReceivers": "callbackUrl",
            "automationRunbookReceivers": "serviceUri",
        }
        has_empty_preserved_endpoint = any(
            any(not str(receiver.get(field) or "").strip() for receiver in props.get(key) or [] if isinstance(receiver, dict))
            for key, field in endpoint_fields.items()
        )
        source_id = str(payload.get("clone_source_id") or (change.summary_json or {}).get("clone_source_id") or "").strip()
        if has_empty_preserved_endpoint and source_id:
            source, source_status, source_error = await get_arm_resource(connection, source_id)
            if source_error or not source:
                return None, source_status, source_error or "Source Action Group not found; the clone could not restore its receiver endpoints."
            body = build_action_group_body(payload, source)
    data, error, status = await arm_write(token, "PUT", change.target_id, body=body, api_version=_ACTION_GROUP_API)
    return (data if isinstance(data, dict) else {} if not error else None), status, safe_error(error)


async def apply_resource_group_change(connection: dict[str, Any], change: Any) -> tuple[dict[str, Any] | None, int, str]:
    """Create an approved resource-group prerequisite without overwriting an existing group."""
    from app.azure.arm import arm_write

    assert_writable(connection)
    if change.operation != "create":
        return None, 422, "Resource group managed changes only support explicit creation."
    live, status, error = await get_arm_resource(connection, change.target_id, "2021-04-01")
    if live:
        return None, 409, "The resource group already exists; refresh and preview the Activity Log plan again."
    if status not in (0, 404):
        return None, status, error or "Could not verify that the resource group name is available."
    desired = decrypted_json(change.desired_encrypted)
    body = desired.get("body") if isinstance(desired.get("body"), dict) else {}
    location = str(body.get("location") or "").strip()
    if not location:
        return None, 422, "A location is required to create a resource group."
    body = {"location": location, "tags": body.get("tags") if isinstance(body.get("tags"), dict) else {}}
    token = await _token(connection)
    data, error, status = await arm_write(
        token, "PUT", change.target_id, body=body, api_version="2021-04-01",
    )
    return (data if isinstance(data, dict) else {} if not error else None), status, safe_error(error)


async def test_action_group(connection: dict[str, Any], action_group_id: str, alert_type: str) -> dict[str, Any]:
    assert_writable(connection)
    resource, _status, error = await get_arm_resource(connection, action_group_id)
    if error or not resource:
        raise ValueError(error or "Action group not found.")
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    request_body = {"alertType": alert_type}
    for key in _ALL_RECEIVER_KEYS:
        if props.get(key):
            request_body[key] = props[key]
    token = await _token(connection)
    url = f"https://management.azure.com{action_group_id.rstrip('/')}/createNotifications"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, params={"api-version": "2021-09-01"}, headers=headers, json=request_body)
        if response.status_code == 200:
            return response.json()
        if response.status_code != 202:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except (ValueError, AttributeError):
                detail = response.text
            raise ValueError(safe_error(f"ARM {response.status_code}: {detail}"))
        status_url = response.headers.get("location") or ""
        if not status_url:
            return {"state": "Accepted", "actionDetails": []}
        for _attempt in range(15):
            await asyncio.sleep(1)
            status_response = await client.get(status_url, headers={"Authorization": f"Bearer {token}"})
            if status_response.status_code == 200:
                result = status_response.json()
                if str(result.get("state") or "").lower() not in {"new", "running", "pending"}:
                    return result
        return {"state": "Running", "actionDetails": [], "detail": "Test accepted; status is still pending."}
