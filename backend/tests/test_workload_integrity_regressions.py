"""Offline workload-integrity regressions; no app lifespan or live estate.

Only synthetic connections, tmp_path registry files, ASGI and scripted CLI pages are used.
The collector, reconciliation, registry persistence and permission guards remain real.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


_SUB = "00000000-0000-0000-0000-000000000001"
_RG = f"/subscriptions/{_SUB}/resourceGroups/rg-integrity"


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Override the suite bootstrap: these tests must not open a configured database."""
    yield


def _unexpected_io(*_args, **_kwargs):
    pytest.fail("Unexpected external I/O or configured-state access in offline regression")


async def _unexpected_async_io(*_args, **_kwargs):
    _unexpected_io()


def _row(name):
    return {
        "id": f"{_RG}/providers/Microsoft.Compute/virtualMachines/{name}",
        "name": name, "type": "Microsoft.Compute/virtualMachines",
        "subscriptionId": _SUB, "resourceGroup": "rg-integrity",
        "location": "eastus", "tags": {"application": "offline-integrity"},
    }


def _node(row):
    return {
        "kind": "resource", "id": row["id"], "name": row["name"],
        "resource_type": row["type"], "subscription_id": row["subscriptionId"],
        "resource_group": row["resourceGroup"], "location": row["location"], "excludes": [],
    }


def _rg_node():
    return {
        "kind": "resource_group", "id": _RG, "name": "rg-integrity",
        "subscription_id": _SUB, "resource_group": "rg-integrity", "excludes": [],
    }


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _unexpected_io)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _unexpected_async_io)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_io)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _unexpected_async_io)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _unexpected_async_io)

    from app.core import config, jsonstore

    settings = config.Settings(
        _env_file=None, environment="test", dev_auth=False, llm_provider="",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unused.sqlite').as_posix()}",
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
    from app.core import app_settings, azure_connections, security

    monkeypatch.setattr(app_settings, "load_settings", lambda: {
        "command_timeout_seconds": 1, "arg_rate_limit_enabled": False,
    })
    from app.api import workloads
    from app.azure import arg_throttle, credentials
    from app.exec import command_runner
    from app.workloads import discovery, registry
    from app.workloads.cache import DiscoveryCache

    connections = {
        "selected": {"id": "selected", "tenant_id": "azure-selected", "auth_method": "service_principal"},
        "default": {"id": "default", "tenant_id": "azure-default", "auth_method": "service_principal"},
        "disabled": {"id": "disabled", "tenant_id": "azure-disabled", "disabled": True},
    }
    monkeypatch.setattr(azure_connections, "_PATH", tmp_path / "connections.json")
    monkeypatch.setattr(azure_connections, "get_connection", connections.get)
    monkeypatch.setattr(azure_connections, "get_default_connection", lambda: connections["default"])
    monkeypatch.setattr(workloads, "get_connection", connections.get)
    monkeypatch.setattr(registry, "_PATH", tmp_path / "workloads.json")
    monkeypatch.setattr(workloads, "discovery_cache", DiscoveryCache())
    monkeypatch.setattr(workloads, "_schedule_resource_group_prefetch", _unexpected_io)
    monkeypatch.setattr(credentials, "get_arm_token", _unexpected_async_io)
    monkeypatch.setattr(discovery, "get_arm_token", _unexpected_async_io)
    monkeypatch.setattr(discovery, "run_kql_capture", _unexpected_async_io)
    monkeypatch.setattr(command_runner, "_graph_page_cli", _unexpected_async_io)
    monkeypatch.setattr(command_runner, "_sp_login", _unexpected_async_io)
    monkeypatch.setattr(command_runner, "load_settings", app_settings.load_settings)
    monkeypatch.setattr(arg_throttle, "acquire", AsyncMock())

    env = SimpleNamespace(
        api=workloads, registry=registry, discovery=discovery, runner=command_runner,
        conns=azure_connections, connections=connections, tmp_path=tmp_path,
        principal=security.Principal(
            subject="writer", email="writer@example.invalid", tenant_id="app-workspace",
            role="operator", permissions=frozenset({"workloads.read", "workloads.write"}),
        ),
    )

    async def identity():
        return env.principal

    env.app = FastAPI()
    env.app.include_router(workloads.router, prefix="/api")
    # Identity only: both permission dependencies are real.
    env.app.dependency_overrides[security.get_principal] = identity
    return env


async def _request(env, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://offline.invalid",
        trust_env=False,
    ) as client:
        return await client.request(method, "/api/workloads" + path, **kwargs)


def _seed(env, connection_id="selected", nodes=None):
    return env.registry.upsert_workload({
        "id": "integrity", "name": "Keep business context", "connection_id": connection_id,
        "tenant_id": "azure-selected", "description": "Must survive refresh",
        "environment": "test", "criticality": "high", "tags": ["offline"],
        "created_by": "owner", "group_id": "family",
        "origin": {"kind": "subscription", "id": _SUB},
        "last_refreshed": "2000-01-01T00:00:00Z",
        "nodes": nodes if nodes is not None else [_rg_node(), _node(_row("alpha")), _node(_row("beta"))],
    })


def _script_cli(env, monkeypatch, pages):
    iterator = iter(pages)
    calls = []

    async def page(_az_path, query, _env, _timeout, page_size, skip_token, *, max_bytes):
        calls.append((skip_token, page_size, query, max_bytes))
        try:
            return next(iterator)
        except StopIteration:
            pytest.fail("Collector requested an unscripted page")

    monkeypatch.setattr(env.runner, "shutil", SimpleNamespace(which=lambda _: "unused-az", rmtree=_unexpected_io))
    monkeypatch.setattr(env.runner, "_run_env", lambda *_: {})
    monkeypatch.setattr(env.runner, "_graph_page_cli", page)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ok,complete,total", [(False, False, 3), (True, False, 3), (True, True, 3)])
async def test_refresh_failed_or_incomplete_read_preserves_exact_registry(offline, monkeypatch, ok, complete, total):
    before = _seed(offline)
    original = offline.registry._PATH.read_bytes()
    collect = AsyncMock(return_value=offline.runner.KqlResult(
        ok=ok, complete=complete, total=total, rows=[_row("alpha")], error="partial read",
    ))
    monkeypatch.setattr(offline.runner, "run_kql_collect", collect)
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 502, response.text
    assert "unchanged" in response.json()["detail"]
    assert offline.registry._PATH.read_bytes() == original
    assert offline.registry.get_workload("integrity") == before
    assert collect.await_args.args[1] == offline.connections["selected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [{}, {"id": ""}, {"id": None}, {"id": 1}])
async def test_malformed_membership_row_cannot_establish_deletions(offline, monkeypatch, row):
    _seed(offline)
    original = offline.registry._PATH.read_bytes()
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=True, rows=[row], complete=True, total=1,
    )))
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 502
    assert offline.registry._PATH.read_bytes() == original


@pytest.mark.asyncio
async def test_real_cli_cap_cannot_delete_1001st_member(offline, monkeypatch):
    rows = [_row(f"vm-{i:04}") for i in range(1001)]
    _seed(offline, nodes=[_rg_node(), *[_node(r) for r in rows]])
    original = offline.registry._PATH.read_bytes()
    calls = _script_cli(offline, monkeypatch, [offline.runner.CaptureResult(
        ok=True, stdout=json.dumps({"data": rows[:1000], "skip_token": "more", "total_records": 1001}),
    )])
    # Reuse a caller-owned dummy session; no login, temp profile or process is created.
    collect = offline.runner.run_kql_collect

    async def in_session(*args, **kwargs):
        return await collect(*args, **kwargs, session_config_dir=str(offline.tmp_path / "unused-session"))

    monkeypatch.setattr(offline.runner, "run_kql_collect", in_session)
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 502, response.text
    assert len(calls) == 1
    assert offline.registry._PATH.read_bytes() == original


@pytest.mark.asyncio
async def test_reader_cannot_refresh_membership(offline):
    from app.core.security import Principal

    _seed(offline)
    original = offline.registry._PATH.read_bytes()
    offline.principal = Principal(
        subject="reader", email="reader@example.invalid", tenant_id="app-workspace",
        role="auditor", permissions=frozenset({"workloads.read"}),
    )
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 403
    assert offline.registry._PATH.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_id", ["missing", "disabled"])
async def test_invalid_canonical_link_never_scans_default(offline, monkeypatch, connection_id):
    _seed(offline, connection_id)
    original = offline.registry._PATH.read_bytes()
    collect = AsyncMock(side_effect=_unexpected_async_io)
    monkeypatch.setattr(offline.runner, "run_kql_collect", collect)
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == (404 if connection_id == "missing" else 400)
    assert "connection" in response.json()["detail"].lower()
    collect.assert_not_awaited()
    assert offline.registry._PATH.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_id", ["missing", "disabled"])
@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/tree", {"kind": "resource_group", "node_id": _RG}),
    ("POST", "/search", {"query": "alpha"}),
    ("POST", "/facets", {}),
    ("POST", "/cache/prefetch", {}),
    ("POST", "/cache/invalidate", {}),
    ("POST", "/autopilot/survey", {}),
    ("POST", "/autopilot/estimate", {}),
    ("POST", "/autopilot/trace", {"seed_resource_id": _row("alpha")["id"]}),
    ("POST", "/autopilot/discover", {}),
    ("POST", "/autopilot/save", {}),
    ("POST", "/autopilot/profiles", {"name": "invalid"}),
    ("GET", "/autopilot/profiles", {}),
    ("DELETE", "/autopilot/profiles/not-a-profile", {}),
    ("GET", "/estate-coverage", {}),
    ("GET", "/overlaps", {"deep": "true"}),
    ("PUT", "", {"name": "invalid"}),
])
async def test_explicit_discovery_selection_is_exact(offline, connection_id, method, path, payload):
    body = {**payload, "connection_id": connection_id}
    kwargs = {"params" if method in ("GET", "DELETE") else "json": body}
    response = await _request(offline, method, path, **kwargs)
    assert response.status_code == (404 if connection_id == "missing" else 400), response.text
    assert "connection" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_default_discovery_cache_is_bound_to_resolved_connection(offline, monkeypatch):
    facets = AsyncMock(side_effect=lambda conn, _sub: {"types": [conn["id"]], "locations": []})
    monkeypatch.setattr(offline.discovery, "facets", facets)
    first = await _request(offline, "POST", "/facets", json={})
    monkeypatch.setattr(offline.conns, "get_default_connection", lambda: offline.connections["selected"])
    second = await _request(offline, "POST", "/facets", json={})
    assert first.json()["types"] == ["default"]
    assert second.json()["types"] == ["selected"]
    assert facets.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_complete_refresh_reconciles_but_preserves_business_context(offline, monkeypatch, empty):
    before = _seed(offline)
    same = _row("alpha")
    same["id"] = same["id"].upper()
    rows = [] if empty else [same, _row("gamma")]
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=True, rows=rows, total=len(rows), complete=True,
    )))
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 200, response.text
    after = offline.registry.get_workload("integrity")
    assert after["nodes"] == ([before["nodes"][0]] if empty else [*before["nodes"][:2], _node(_row("gamma"))])
    assert response.json()["diff"]["removed_count"] == (2 if empty else 1)
    for key in ("description", "tenant_id", "connection_id", "created_at", "created_by", "group_id", "origin", "tags", "criticality"):
        assert after[key] == before[key]
    assert after["last_refreshed"] != before["last_refreshed"]
    assert after["tenant_id"] != offline.principal.tenant_id


@pytest.mark.asyncio
async def test_legacy_default_and_shared_azure_configuration_remain_supported(offline, monkeypatch):
    before = _seed(offline, connection_id="")
    collect = AsyncMock(return_value=offline.runner.KqlResult(ok=True, rows=[], total=0))
    monkeypatch.setattr(offline.runner, "run_kql_collect", collect)
    listed = await _request(offline, "GET", "")
    assert listed.json()["workloads"] == [before]
    assert (await _request(offline, "POST", "/integrity/refresh")).status_code == 200
    assert collect.await_args.args[1] == offline.connections["default"]
    # The shared legacy resolver is deliberately NOT changed by the workload API fix.
    assert offline.conns.resolve_connection("missing") == offline.connections["default"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,excluded_scope", [
    ("resource_group", "resource"), ("resource_group", "child"),
    ("subscription", "resource"), ("subscription", "resource_group"),
    ("mg", "resource_group"), ("mg", "subscription"),
])
async def test_refresh_honors_resource_and_scope_exclusions(offline, monkeypatch, kind, excluded_scope):
    excluded = _row("excluded")
    exclusion = excluded["id"]
    if excluded_scope == "child":
        excluded["id"] += "/extensions/agent"
    elif excluded_scope == "resource_group":
        exclusion = _RG
    elif excluded_scope == "subscription":
        exclusion = f"/subscriptions/{_SUB}"
    scope = _rg_node() if kind == "resource_group" else {
        "kind": kind, "id": _SUB if kind == "subscription" else "test-mg",
    }
    scope["excludes"] = [exclusion.upper() + "/"]
    _seed(offline, nodes=[scope, _node(_row("alpha"))])
    monkeypatch.setattr(offline.discovery, "resolve_management_group_scope", AsyncMock(return_value={"subscriptions": [_SUB]}))
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=True, rows=[_row("alpha"), excluded], complete=True, total=2,
    )))
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 200, response.text
    members = [n["id"] for n in response.json()["workload"]["nodes"] if n["kind"] == "resource"]
    assert members == [_row("alpha")["id"]]
    assert response.json()["diff"]["added_count"] == 0


@pytest.mark.asyncio
async def test_exclusions_are_per_scope_not_a_global_deny_list(offline, monkeypatch):
    scope = _rg_node()
    scope["excludes"] = [_row("gamma")["id"]]
    included_scope = {"kind": "subscription", "id": _SUB, "excludes": []}
    _seed(offline, nodes=[scope, included_scope, _node(_row("alpha"))])
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=True, rows=[_row("alpha"), _row("gamma")], total=2,
    )))
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 200
    assert response.json()["diff"]["added"] == [_node(_row("gamma"))]


@pytest.mark.asyncio
async def test_unreadable_exclusion_scope_cannot_persist_refresh(offline, monkeypatch):
    _seed(offline, nodes=[{"kind": "mg", "id": "mg", "excludes": [_RG]}, _node(_row("alpha"))])
    original = offline.registry._PATH.read_bytes()
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(ok=True, rows=[])))
    monkeypatch.setattr(offline.discovery, "resolve_management_group_scope", AsyncMock(side_effect=PermissionError("unreadable")))
    response = await _request(offline, "POST", "/integrity/refresh")
    assert response.status_code == 502
    assert offline.registry._PATH.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_kind", ["empty", "overlap", "raises"])
async def test_paged_fallback_preserves_known_rows_and_incompleteness(offline, monkeypatch, fallback_kind):
    rows = [_row("alpha"), _row("beta")]
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=False, rows=deepcopy(rows), complete=False, total=3, error="later page denied",
    )))
    fallback_rows = [offline.discovery._norm_resource(r) for r in [_row("alpha"), _row("gamma")]]
    fallback_rows[0]["id"] = fallback_rows[0]["id"].upper()
    fallback = AsyncMock(return_value=fallback_rows if fallback_kind == "overlap" else [])
    if fallback_kind == "raises":
        fallback.side_effect = RuntimeError("fallback unavailable")
    monkeypatch.setattr(offline.discovery, "resources_in_subscriptions", fallback)
    resources, truncated = await offline.discovery.enumerate_resources_paged(offline.connections["selected"], [_SUB], cap=3)
    assert resources[:2] == [offline.discovery._norm_resource(r) for r in rows]
    assert len(resources) == (3 if fallback_kind == "overlap" else 2)
    assert truncated is True


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_failed_enumeration_with_empty_fallback_never_claims_complete(offline, monkeypatch, known):
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=False, rows=[], complete=False, total=1 if known else None, error="denied",
    )))
    monkeypatch.setattr(offline.discovery, "resources_in_subscriptions", AsyncMock(return_value=[]))
    assert await offline.discovery.enumerate_resources_paged(offline.connections["selected"], [_SUB]) == ([], True)


@pytest.mark.asyncio
async def test_paged_enumeration_cannot_hide_a_larger_known_total(offline, monkeypatch):
    monkeypatch.setattr(offline.runner, "run_kql_collect", AsyncMock(return_value=offline.runner.KqlResult(
        ok=True, rows=[_row("alpha")], complete=True, total=2,
    )))
    resources, truncated = await offline.discovery.enumerate_resources_paged(offline.connections["selected"], [_SUB])
    assert resources == [offline.discovery._norm_resource(_row("alpha"))]
    assert truncated is True


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper,max_rows,complete", [
    ({"data": [_row("alpha"), _row("beta")], "total_records": 2}, 1, False),
    ({"data": [_row("alpha"), _row("beta")]}, 1, False),
    ({"data": [_row("alpha")], "total_records": 1}, 1, True),
    ({"data": [_row("alpha")], "totalRecords": 2}, 5, False),
    ({"data": [_row("alpha")], "totalRecords": 0}, 5, False),
    ({"data": [_row("alpha")], "resultTruncated": "true"}, 5, False),
    ({"data": [], "total_records": 0}, 5, True),
    ([_row("alpha")], 5, True),
])
async def test_cli_terminal_page_completeness(offline, monkeypatch, wrapper, max_rows, complete):
    _script_cli(offline, monkeypatch, [offline.runner.CaptureResult(ok=True, stdout=json.dumps(wrapper))])
    result = await offline.runner.run_kql_collect(
        "Resources | project id", offline.connections["selected"], max_rows=max_rows,
        session_config_dir=str(offline.tmp_path / "unused-session"),
    )
    assert result.ok is True
    assert result.complete is complete
    assert len(result.rows) <= max_rows
    assert result.pages == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "", "{", "null", "true", "1", '"text"', "{}", '{"data":{}}',
    '{"data":[1]}', '{"data":null}', '{"data":[],"skip_token":1}',
    '{"data":[],"skip_token":false}', '{"data":[],"skip_token":[]}',
    '{"data":[],"total_records":true}', '{"data":[],"total_records":-1}',
    '{"data":[],"total_records":1.5}', '{"data":[],"resultTruncated":{}}',
])
async def test_cli_malformed_later_page_preserves_prior_rows_and_total(offline, monkeypatch, bad):
    calls = _script_cli(offline, monkeypatch, [
        offline.runner.CaptureResult(ok=True, stdout=json.dumps({"data": [_row("alpha")], "skip_token": "two", "total_records": 2})),
        offline.runner.CaptureResult(ok=True, stdout=bad),
    ])
    result = await offline.runner.run_kql_collect(
        "Resources | project id", offline.connections["selected"],
        session_config_dir=str(offline.tmp_path / "unused-session"),
    )
    assert result.ok is False and result.complete is False
    assert result.rows == [_row("alpha")]
    assert result.total == 2 and result.pages == 1
    assert "parse" in result.error.lower()
    assert [call[0] for call in calls] == ["", "two"]


@pytest.mark.asyncio
async def test_rest_adapter_checks_known_total_without_changing_return_shape(offline, monkeypatch):
    from app.azure import arm, credentials

    monkeypatch.setattr(credentials, "get_arm_token", AsyncMock(return_value=("offline-token", None)))
    monkeypatch.setattr(arm, "query_resource_graph_paged", AsyncMock(return_value=([_row("alpha")], None, True, 2)))
    result = await offline.runner.run_kql_collect("Resources | project id", {"id": "token", "auth_method": "az_cli_token"})
    assert result.ok is True and result.complete is False
    assert result.rows == [_row("alpha")] and result.total == 2


@pytest.mark.asyncio
async def test_invalid_query_is_not_a_complete_collection(offline):
    result = await offline.runner.run_kql_collect("", offline.connections["selected"])
    assert result.ok is False and result.complete is False
    assert result.rows == [] and result.pages == 0


@pytest.mark.asyncio
async def test_cli_complete_paging_retains_totals_and_requests_continuation(offline, monkeypatch):
    calls = _script_cli(offline, monkeypatch, [
        offline.runner.CaptureResult(ok=True, stdout=json.dumps({"data": [_row("alpha")], "skip_token": "two", "total_records": 2})),
        offline.runner.CaptureResult(ok=True, stdout=json.dumps({"data": [_row("beta")], "total_records": 2})),
    ])
    result = await offline.runner.run_kql_collect(
        "Resources | project id", offline.connections["selected"], page_size=1,
        session_config_dir=str(offline.tmp_path / "unused-session"),
    )
    assert result.ok and result.complete
    assert result.rows == [_row("alpha"), _row("beta")]
    assert result.total == 2 and result.pages == 2
    assert [call[0] for call in calls] == ["", "two"]