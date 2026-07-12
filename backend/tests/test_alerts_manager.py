"""Phase 1/2 Alerts Manager safety and Action Group management contracts."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.alerts_manager import service


BASE_PAYLOAD = {
    "name": "ops-alerts",
    "subscription_id": "sub-1",
    "resource_group": "rg-monitoring",
    "location": "Global",
    "short_name": "ops",
    "enabled": True,
    "email_receivers": [
        {"name": "oncall", "email_address": "oncall@example.com", "use_common_alert_schema": True}
    ],
    "sms_receivers": [],
    "webhook_receivers": [],
    "arm_role_receivers": [],
    "tags": {"managed-by": "alerts-manager"},
}


def test_action_group_payload_build_and_summary_do_not_expose_destinations() -> None:
    assert service.validate_action_group_payload(BASE_PAYLOAD, create=True) == []
    body = service.build_action_group_body(BASE_PAYLOAD)
    assert body["properties"]["emailReceivers"][0]["emailAddress"] == "oncall@example.com"
    summary = service.summarize_action_group_body(body)
    assert summary["email_receivers"] == 1
    assert "oncall@example.com" not in json.dumps(summary)


def test_sensitive_change_payload_is_encrypted_and_round_trips() -> None:
    encrypted = service.encrypted_json({"body": service.build_action_group_body(BASE_PAYLOAD)})
    assert encrypted.startswith("enc:v1:")
    assert "oncall@example.com" not in encrypted
    assert service.decrypted_json(encrypted)["body"]["properties"]["emailReceivers"][0]["emailAddress"] == "oncall@example.com"


def test_webhook_secret_is_preserved_without_returning_query_string() -> None:
    before = {
        "location": "Global",
        "properties": {
            "groupShortName": "ops",
            "enabled": True,
            "webhookReceivers": [
                {"name": "hook", "serviceUri": "https://hooks.example.test/notify?sig=secret", "useCommonAlertSchema": True}
            ],
        },
    }
    editable = service.editable_action_group({"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops", **before})
    webhook = editable["webhook_receivers"][0]
    assert webhook["service_uri"] == "https://hooks.example.test/notify"
    assert webhook["preserve_secret"] is True
    rebuilt = service.build_action_group_body(editable, before)
    assert rebuilt["properties"]["webhookReceivers"][0]["serviceUri"].endswith("?sig=secret")


def test_clone_preserves_source_webhook_secret() -> None:
    source = {
        "location": "Global",
        "properties": {
            "groupShortName": "ops",
            "enabled": True,
            "webhookReceivers": [
                {"name": "hook", "serviceUri": "https://hooks.example.test/notify?sig=secret", "useCommonAlertSchema": True}
            ],
        },
    }
    desired = service.editable_action_group({
        "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops",
        "name": "ops",
        **source,
    })
    desired.update({"id": "", "name": "ops-copy", "short_name": "ops-cp"})
    cloned = service.build_action_group_body(desired, source)
    assert cloned["properties"]["groupShortName"] == "ops-cp"
    assert cloned["properties"]["webhookReceivers"][0]["serviceUri"].endswith("?sig=secret")


def test_invalid_receivers_are_rejected() -> None:
    payload = {**BASE_PAYLOAD, "short_name": "this-is-more-than-twelve", "email_receivers": [{"name": "bad", "email_address": "not-email"}]}
    errors = service.validate_action_group_payload(payload, create=True)
    assert any("12 characters" in error for error in errors)
    assert any("Invalid email" in error for error in errors)


def test_safe_error_shows_contacts_and_masks_signed_urls() -> None:
    result = service.safe_error("Failed for user@example.com +1 (555) 123-4567 at https://hooks.test/a?sig=topsecret")
    assert "user@example.com" in result
    assert "123-4567" in result
    assert "topsecret" not in result


def test_read_only_connection_rejects_writes() -> None:
    with pytest.raises(PermissionError, match="read-only"):
        service.assert_writable({"read_only": True})


@pytest.mark.asyncio
async def test_live_action_group_inventory_includes_dependencies_and_full_destinations(monkeypatch) -> None:
    group_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops"
    rows = [
        {
            "id": group_id,
            "name": "ops",
            "type": "microsoft.insights/actiongroups",
            "subscriptionId": "s",
            "resourceGroup": "rg",
            "location": "Global",
            "properties": {
                "enabled": True,
                "groupShortName": "ops",
                "emailReceivers": [{"name": "oncall", "emailAddress": "oncall@example.com"}],
            },
        },
        {
            "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/cpu",
            "name": "cpu",
            "type": "microsoft.insights/metricalerts",
            "properties": {"scopes": ["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"], "actions": [{"actionGroupId": group_id}]},
        },
    ]

    async def fake_arg(*_args, **_kwargs):
        return rows

    monkeypatch.setattr(service, "_arg", fake_arg)
    result = await service.list_action_groups({"id": "c"}, subscription_id="s")
    assert result[0]["dependency_count"] == 1
    assert result[0]["receivers"][0]["destination"] == "oncall@example.com"
    assert result[0]["receivers"][0]["masked"] == "oncall@example.com"


def test_state_hash_is_stable_and_changes_with_configuration() -> None:
    body = service.build_action_group_body(BASE_PAYLOAD)
    assert service.canonical_hash(body) == service.canonical_hash(json.loads(json.dumps(body)))
    changed = json.loads(json.dumps(body))
    changed["properties"]["enabled"] = False
    assert service.canonical_hash(body) != service.canonical_hash(changed)


def test_all_advanced_action_group_receivers_round_trip_and_preserve_secrets() -> None:
    app_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Web/sites/functions"
    logic_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Logic/workflows/notify"
    automation_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Automation/automationAccounts/ops"
    payload = {
        **BASE_PAYLOAD,
        "voice_receivers": [{"name": "voice", "country_code": "1", "phone_number": "5551234567"}],
        "azure_app_push_receivers": [{"name": "push", "email_address": "operator@example.com"}],
        "azure_function_receivers": [{"name": "fn", "function_app_resource_id": app_id, "function_name": "HandleAlert", "endpoint": "https://functions.azurewebsites.net/api/HandleAlert?code=secret", "preserve_secret": False, "use_common_alert_schema": True}],
        "logic_app_receivers": [{"name": "logic", "resource_id": logic_id, "endpoint": "https://logic.azure.com/workflows/callback?sig=secret", "preserve_secret": False, "use_common_alert_schema": True}],
        "event_hub_receivers": [{"name": "hub", "subscription_id": "11111111-1111-1111-1111-111111111111", "tenant_id": "22222222-2222-2222-2222-222222222222", "namespace_name": "events", "event_hub_name": "alerts", "use_common_alert_schema": True}],
        "automation_runbook_receivers": [{"name": "runbook", "automation_account_id": automation_id, "runbook_name": "Triage", "webhook_resource_id": f"{automation_id}/webhooks/Triage", "endpoint": "https://automation.azure.com/webhooks?token=secret", "preserve_secret": False, "is_global_runbook": False, "use_common_alert_schema": True}],
        "itsm_receivers": [{"name": "itsm", "workspace_id": "33333333-3333-3333-3333-333333333333", "connection_id": "servicenow", "region": "eastus", "ticket_configuration": '{"urgency":"2"}', "preserve_configuration": False}],
    }
    assert service.validate_action_group_payload(payload, create=True) == []
    body = service.build_action_group_body(payload)
    resource = {"id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops", **body}
    editable = service.editable_action_group(resource)
    assert editable["azure_function_receivers"][0]["endpoint"] == "https://functions.azurewebsites.net/api/HandleAlert"
    assert editable["azure_function_receivers"][0]["preserve_secret"] is True
    assert editable["logic_app_receivers"][0]["endpoint"] == "https://logic.azure.com/workflows/callback"
    assert editable["itsm_receivers"][0]["ticket_configuration"] == ""
    assert editable["itsm_receivers"][0]["preserve_configuration"] is True
    rebuilt = service.build_action_group_body(editable, resource)
    assert rebuilt["properties"]["azureFunctionReceivers"][0]["httpTriggerUrl"].endswith("?code=secret")
    assert rebuilt["properties"]["logicAppReceivers"][0]["callbackUrl"].endswith("?sig=secret")
    assert rebuilt["properties"]["automationRunbookReceivers"][0]["serviceUri"].endswith("?token=secret")
    assert rebuilt["properties"]["itsmReceivers"][0]["ticketConfiguration"] == '{"urgency":"2"}'


def test_unknown_future_action_group_receiver_is_preserved() -> None:
    before = {"location": "Global", "properties": {"groupShortName": "ops", "enabled": True, "futureReceivers": [{"name": "future", "secret": "preserve"}]}}
    payload = {**BASE_PAYLOAD, "email_receivers": []}
    body = service.build_action_group_body(payload, before)
    assert body["properties"]["futureReceivers"] == [{"name": "future", "secret": "preserve"}]


def test_advanced_receiver_validation_rejects_incomplete_configuration() -> None:
    payload = {**BASE_PAYLOAD, "azure_function_receivers": [{"name": "fn", "function_app_resource_id": "bad", "function_name": "", "endpoint": "http://bad", "preserve_secret": False}], "event_hub_receivers": [{"name": "hub", "subscription_id": "bad", "namespace_name": "", "event_hub_name": ""}]}
    errors = service.validate_action_group_payload(payload, create=True)
    assert any("Function App resource ID" in error for error in errors)
    assert any("HTTPS endpoint" in error for error in errors)
    assert any("subscription GUID" in error for error in errors)


@pytest.mark.asyncio
async def test_create_change_submits_validated_arm_put(monkeypatch) -> None:
    body = service.build_action_group_body(BASE_PAYLOAD)
    change = SimpleNamespace(
        operation="create",
        target_id="/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.Insights/actionGroups/ops-alerts",
        desired_encrypted=service.encrypted_json({"body": body}),
        before_encrypted=service.encrypted_json({}),
        expected_state_hash="",
    )

    async def missing(*_args, **_kwargs):
        return None, 404, "ARM 404: not found"

    async def token(_connection):
        return "token", None

    captured = {}

    async def write(_token, method, path, *, body=None, api_version="", query=None):
        captured.update({"method": method, "path": path, "body": body, "api_version": api_version})
        return {"id": path, **body}, None, 201

    monkeypatch.setattr(service, "get_arm_resource", missing)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr("app.azure.arm.arm_write", write)
    resource, status, error = await service.apply_action_group_change({"read_only": False}, change)
    assert error == ""
    assert status == 201
    assert resource and resource["properties"]["groupShortName"] == "ops"
    assert captured["method"] == "PUT"
    assert captured["api_version"] == "2023-01-01"


@pytest.mark.asyncio
async def test_failed_legacy_clone_restores_empty_webhook_uri(monkeypatch) -> None:
    source_id = "/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.Insights/actionGroups/source"
    target_id = "/subscriptions/sub-1/resourceGroups/rg-monitoring/providers/Microsoft.Insights/actionGroups/source-copy"
    source = {
        "id": source_id,
        "location": "Global",
        "properties": {
            "groupShortName": "source",
            "enabled": True,
            "webhookReceivers": [{"name": "hook", "serviceUri": "https://hooks.test/notify?sig=secret", "useCommonAlertSchema": True}],
        },
    }
    payload = service.editable_action_group(source)
    payload.update({"id": "", "name": "source-copy", "short_name": "source-cp", "clone_source_id": source_id})
    broken = service.build_action_group_body(payload)
    assert broken["properties"]["webhookReceivers"][0]["serviceUri"] == ""
    change = SimpleNamespace(
        operation="create", target_id=target_id,
        desired_encrypted=service.encrypted_json({"payload": payload, "body": broken}),
        before_encrypted=service.encrypted_json({}), expected_state_hash="", summary_json={},
    )

    async def resources(_connection, resource_id):
        return (source, 200, "") if resource_id == source_id else (None, 404, "ARM 404: not found")

    async def token(_connection):
        return "token", None

    captured = {}

    async def write(_token, _method, _path, *, body=None, **_kwargs):
        captured["body"] = body
        return {"id": target_id, **body}, None, 201

    monkeypatch.setattr(service, "get_arm_resource", resources)
    monkeypatch.setattr("app.azure.credentials.get_arm_token", token)
    monkeypatch.setattr("app.azure.arm.arm_write", write)
    resource, status, error = await service.apply_action_group_change({"read_only": False}, change)
    assert error == ""
    assert status == 201
    assert resource
    assert captured["body"]["properties"]["webhookReceivers"][0]["serviceUri"].endswith("?sig=secret")


@pytest.mark.asyncio
async def test_update_change_blocks_stale_azure_state(monkeypatch) -> None:
    before = {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ops", "location": "Global", "properties": {"enabled": True, "groupShortName": "ops"}}
    change = SimpleNamespace(
        operation="update",
        target_id=before["id"],
        desired_encrypted=service.encrypted_json({"body": service._resource_body(before)}),
        before_encrypted=service.encrypted_json(before),
        expected_state_hash=service.canonical_hash(service._resource_body(before)),
    )
    changed_live = json.loads(json.dumps(before))
    changed_live["properties"]["enabled"] = False

    async def live(*_args, **_kwargs):
        return changed_live, 200, ""

    monkeypatch.setattr(service, "get_arm_resource", live)
    resource, status, error = await service.apply_action_group_change({"read_only": False}, change)
    assert resource is None
    assert status == 409
    assert "changed after" in error
