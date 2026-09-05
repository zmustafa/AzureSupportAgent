"""Offline regressions: input validation and honest discovery provenance."""
from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """No configured database bootstrap; audit writes use an in-memory stub."""
    yield


def _unexpected_io(*_args, **_kwargs):
    pytest.fail("Unexpected live I/O or configured-state access")


async def _unexpected_async_io(*_args, **_kwargs):
    _unexpected_io()


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _unexpected_io)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _unexpected_async_io)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_io)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _unexpected_async_io)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _unexpected_async_io)

    from app.core import config, jsonstore

    key = Fernet.generate_key()
    settings = config.Settings(
        _env_file=None, environment="test", dev_auth=False, llm_provider="",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unused.sqlite').as_posix()}",
        secrets_encryption_key=key.decode("ascii"),
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(jsonstore, "_postgres_connect_kwargs", lambda: None)
    monkeypatch.setattr(jsonstore, "_CACHE", {})
    monkeypatch.setattr(jsonstore, "_LOCKS", {})
    read_json, mutate_json = jsonstore.read_json, jsonstore.mutate_json

    def checked_read(path, *args, **kwargs):
        assert path.resolve().is_relative_to(tmp_path.resolve())
        return read_json(path, *args, **kwargs)

    def checked_mutate(path, *args, **kwargs):
        assert path.resolve().is_relative_to(tmp_path.resolve())
        return mutate_json(path, *args, **kwargs)

    monkeypatch.setattr(jsonstore, "read_json", checked_read)
    monkeypatch.setattr(jsonstore, "mutate_json", checked_mutate)
    from app.core import azure_connections, crypto, db, security
    from app.api import connections

    monkeypatch.setattr(crypto, "_fernet", Fernet(key))
    monkeypatch.setattr(azure_connections, "_PATH", tmp_path / "connections.json")
    monkeypatch.setattr(connections, "get_arm_token", _unexpected_async_io)
    monkeypatch.setattr(connections, "list_subscriptions", _unexpected_async_io)
    monkeypatch.setattr(connections, "list_management_groups", _unexpected_async_io)
    selected = azure_connections.upsert_connection({
        "id": "selected", "display_name": "Synthetic connection", "tenant_id": "azure-selected",
        "auth_method": "az_cli_token", "access_token": "offline-existing-token",
    })
    azure_connections.upsert_connection({
        "id": "default", "display_name": "Not selected", "tenant_id": "azure-default",
        "auth_method": "az_cli_token", "is_default": True,
    })
    audit = SimpleNamespace(add=Mock(), commit=AsyncMock())

    async def identity():
        return security.Principal(
            subject="connection-manager", email="manager@example.invalid",
            tenant_id="app-workspace", role="operator",
            permissions=frozenset({"connections.manage"}),
        )

    async def database():
        yield audit

    app = FastAPI()
    app.include_router(connections.router, prefix="/api")
    app.dependency_overrides[security.get_principal] = identity
    app.dependency_overrides[db.get_db] = database
    return SimpleNamespace(app=app, api=connections, conns=azure_connections, selected=selected, audit=audit)


async def _request(env, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url="http://offline.invalid", trust_env=False,
    ) as client:
        return await client.request(method, "/api/admin/connections" + path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["access_token_json", "graph_access_token_json"])
@pytest.mark.parametrize("value", [
    [], None, "token", 123, True, {}, {"accessToken": ""}, {"accessToken": "   "},
    {"accessToken": []}, {"accessToken": {"secret": "not-a-string"}},
    {"accessToken": 12}, {"accessToken": True}, {"access_token": ["not-a-string"]},
    {"accessToken": "token with spaces"},
    {"accessToken": "offline-token", "expires_on": []},
    {"accessToken": "offline-token", "expires_on": {}},
    {"accessToken": "offline-token", "expires_on": True},
    {"accessToken": "offline-token", "expires_on": -1},
    {"accessToken": "offline-token", "expires_on": "not-a-date"},
    {"accessToken": "offline-token", "expiresOn": []},
    {"accessToken": "offline-token", "tenant": []},
    {"accessToken": "offline-token", "subscription": 123},
])
async def test_invalid_pasted_token_returns_400_without_persistence(offline, field, value):
    original = offline.conns._PATH.read_bytes()
    response = await _request(offline, "PUT", "", json={
        "id": "selected", "display_name": "Must not persist", "tenant_id": "azure-selected",
        "auth_method": "az_cli_token", field: json.dumps(value),
    })
    assert response.status_code == 400, response.text
    assert offline.conns._PATH.read_bytes() == original
    offline.audit.add.assert_not_called()
    offline.audit.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_graph_json_does_not_save_valid_arm_token_first(offline):
    original = offline.conns._PATH.read_bytes()
    response = await _request(offline, "PUT", "", json={
        "id": "selected", "display_name": "Must not persist", "tenant_id": "azure-selected",
        "auth_method": "az_cli_token", "access_token_json": '{"accessToken":"offline-new-token"}',
        "graph_access_token_json": "{",
    })
    assert response.status_code == 400
    assert offline.conns._PATH.read_bytes() == original
    offline.audit.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,target,expiry_target", [
    ("access_token_json", "access_token", "token_expires_on"),
    ("graph_access_token_json", "graph_access_token", "graph_token_expires_on"),
])
@pytest.mark.parametrize("token_key", ["accessToken", "access_token"])
@pytest.mark.parametrize("expiry,expected", [
    ({"expires_on": 2000000000, "expiresOn": "ignored-local-format"}, "2000000000"),
    ({"expires_on": "2000000000"}, "2000000000"),
    ({"expiresOn": "2033-05-18 12:30:00.000000"}, "2033-05-18 12:30:00.000000"),
    ({"expiresOn": "2033-05-18T12:30:00Z"}, "2033-05-18T12:30:00Z"),
    ({"expiresOn": None}, ""),
    ({}, ""),
])
async def test_valid_cli_token_formats_preserve_compatibility(
    offline, field, target, expiry_target, token_key, expiry, expected,
):
    response = await _request(offline, "PUT", "", json={
        "id": "selected", "display_name": "Synthetic connection", "tenant_id": "azure-selected",
        "default_subscription": "explicit-subscription", "auth_method": "az_cli_token",
        field: json.dumps({token_key: "offline-valid-token", "tenant": None, "subscription": None, **expiry}),
    })
    assert response.status_code == 200, response.text
    saved = offline.conns.get_connection("selected")
    assert saved[target] == "offline-valid-token"
    assert saved[expiry_target] == expected
    assert saved["tenant_id"] == "azure-selected"
    assert saved["default_subscription"] == "explicit-subscription"
    assert "offline-valid-token" not in response.text
    assert "offline-valid-token" not in offline.conns._PATH.read_text(encoding="utf-8")
    offline.audit.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_error,mg_error,partial", [
    ("403 subscription discovery forbidden", "", True),
    ("", "403 management-group discovery forbidden", True),
    ("403 subscriptions", "403 management groups", False),
])
async def test_discovery_exposes_per_source_errors_and_preserves_readable_rows(
    offline, monkeypatch, sub_error, mg_error, partial,
):
    token = AsyncMock(return_value=("offline-token", None))
    subs = [] if sub_error else [{"id": "synthetic-sub", "name": "Readable subscription"}]
    mgs = [] if mg_error else [{"id": "synthetic-mg", "name": "Readable group"}]
    monkeypatch.setattr(offline.api, "get_arm_token", token)
    monkeypatch.setattr(offline.api, "list_subscriptions", AsyncMock(return_value=(subs, sub_error)))
    monkeypatch.setattr(offline.api, "list_management_groups", AsyncMock(return_value=(mgs, mg_error)))
    original = offline.conns._PATH.read_bytes()
    response = await _request(offline, "GET", "/selected/discover")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["partial"] is partial
    assert body["subscriptions"] == subs and body["management_groups"] == mgs
    assert body["errors"] == {k: v for k, v in (
        ("subscriptions", sub_error), ("management_groups", mg_error),
    ) if v}
    assert all(error in body["detail"] for error in body["errors"].values())
    token.assert_awaited_once_with(offline.selected)
    assert offline.conns._PATH.read_bytes() == original


@pytest.mark.asyncio
async def test_partially_read_source_keeps_its_rows(offline, monkeypatch):
    monkeypatch.setattr(offline.api, "get_arm_token", AsyncMock(return_value=("offline-token", None)))
    monkeypatch.setattr(offline.api, "list_subscriptions", AsyncMock(return_value=([{"id": "known-sub"}], "page two denied")))
    monkeypatch.setattr(offline.api, "list_management_groups", AsyncMock(return_value=([], "groups denied")))
    body = (await _request(offline, "GET", "/selected/discover")).json()
    assert body["ok"] is False and body["partial"] is True
    assert body["subscriptions"] == [{"id": "known-sub"}]
    assert set(body["errors"]) == {"subscriptions", "management_groups"}


@pytest.mark.asyncio
async def test_disabled_connection_diagnostics_keep_exact_identity_and_success_shape(offline, monkeypatch):
    disabled = offline.conns.upsert_connection({"id": "selected", "disabled": True})
    token = AsyncMock(return_value=("offline-token", None))
    monkeypatch.setattr(offline.api, "get_arm_token", token)
    monkeypatch.setattr(offline.api, "list_subscriptions", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(offline.api, "list_management_groups", AsyncMock(return_value=([], None)))
    response = await _request(offline, "GET", "/selected/discover")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "subscriptions": [], "management_groups": []}
    token.assert_awaited_once_with(disabled)


@pytest.mark.asyncio
async def test_missing_connection_diagnostics_do_not_use_default(offline, monkeypatch):
    token = AsyncMock(side_effect=_unexpected_async_io)
    monkeypatch.setattr(offline.api, "get_arm_token", token)
    response = await _request(offline, "GET", "/missing/discover")
    assert response.status_code == 404
    token.assert_not_awaited()