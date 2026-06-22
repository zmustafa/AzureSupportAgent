"""Connection-matrix guardrail tests for the non-service-principal ARM-REST data bridge.

The bug class: a read-only `az` CLI data path returns ZERO (or fails cryptically) for a
non-service-principal connection (pasted ARM token / managed identity) because the CLI has no
ambient login for that tenant. The fix routes such connections to ARM REST with the
connection's own token (for ARM-audience reads) or fails CLOSED with a clear message (for
audiences a pasted ARM token can't serve: Log Analytics / App Insights / Key Vault data-plane).

These tests pin that contract so the class can't silently regress: a read collector must
return DATA for every connection type, or a non-empty ERROR — never a silent empty result.
"""
import json

import pytest

import app.azure.arm as arm
import app.azure.credentials as creds
import app.exec.command_runner as cr

SP = {"id": "sp", "auth_method": "service_principal", "tenant_id": "t", "client_id": "c", "client_secret": "s"}
PASTED = {"id": "mat", "auth_method": "az_cli_token", "tenant_id": "t", "access_token": "TOK"}


def _arm_token_ok(*_a, **_k):
    async def _f(conn):
        return "TOK", None
    return _f


def _arm_token_expired(conn):
    return (None, "Pasted token has expired — paste a fresh one.")


# --------------------------------------------------------------------------- mode decision
def test_arm_rest_mode_sp_uses_cli():
    import asyncio
    mode, token, err = asyncio.run(cr._arm_rest_mode(SP))
    assert mode == "cli" and token is None


def test_arm_rest_mode_pasted_token_uses_rest(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    mode, token, err = asyncio.run(cr._arm_rest_mode(PASTED))
    assert mode == "rest" and token == "TOK"


def test_arm_rest_mode_pasted_token_no_token_fails_closed(monkeypatch):
    import asyncio
    async def _no(conn):
        return None, "Pasted token has expired — paste a fresh one."
    monkeypatch.setattr(creds, "get_arm_token", _no)
    mode, token, err = asyncio.run(cr._arm_rest_mode(PASTED))
    assert mode == "error" and token is None and "expired" in err.lower()


def test_arm_rest_mode_none_connection_falls_through_to_cli():
    import asyncio
    mode, token, err = asyncio.run(cr._arm_rest_mode(None))
    assert mode == "cli"


# --------------------------------------------------------------------------- audience classifier
@pytest.mark.parametrize("argv,expected", [
    (["monitor", "log-analytics", "query", "--workspace", "w"], "log_analytics"),
    (["monitor", "app-insights", "query", "--app", "a"], "app_insights"),
    (["keyvault", "secret", "list", "--vault-name", "v"], "key_vault"),
    (["monitor", "diagnostic-settings", "list", "--resource", "r"], "arm"),
    (["monitor", "metrics", "list-definitions", "--resource", "r"], "arm"),
    (["rest", "--method", "get", "--url", "https://management.azure.com/x"], "arm"),
    (["rest", "--method", "get", "--url", "https://graph.microsoft.com/x"], "other"),
])
def test_az_argv_audience(argv, expected):
    assert cr._az_argv_audience(argv) == expected


# --------------------------------------------------------------------------- metrics → REST
def test_run_metrics_capture_pasted_token_uses_rest(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())

    seen = {}
    async def _fake_get_metrics(token, resource_id, *, metricnames, aggregations, interval, start_time, end_time, dimension_filter):
        seen.update(token=token, rid=resource_id, metricnames=metricnames, aggregations=aggregations)
        return json.dumps({"value": [{"timeseries": [{"data": [{"average": 1.0}]}]}]}), None
    monkeypatch.setattr(arm, "get_metrics", _fake_get_metrics)

    res = asyncio.run(cr.run_metrics_capture("/subscriptions/s/rg/r", ["Cpu"], PASTED, aggregation="Average"))
    assert res.ok and seen["token"] == "TOK" and seen["metricnames"] == ["Cpu"]
    data = json.loads(res.stdout)
    assert data["value"][0]["timeseries"][0]["data"][0]["average"] == 1.0


def test_run_metrics_capture_sp_uses_cli(monkeypatch):
    """A service principal must NOT take the REST branch — it runs the CLI (unchanged)."""
    import asyncio
    called = {"cli": False}

    async def _fake_stream(argv_tail, connection, *, label, session_config_dir=None, **k):
        called["cli"] = True
        yield {"type": "stdout", "text": "{}"}
        yield {"type": "exit", "code": 0}
    monkeypatch.setattr(cr, "_run_az_argv_stream", _fake_stream)

    asyncio.run(cr.run_metrics_capture("/subscriptions/s/rg/r", ["Cpu"], SP, aggregation="Average"))
    assert called["cli"] is True


def test_run_metrics_capture_pasted_token_no_token_fails_closed(monkeypatch):
    import asyncio
    async def _no(conn):
        return None, "token gone"
    monkeypatch.setattr(creds, "get_arm_token", _no)
    res = asyncio.run(cr.run_metrics_capture("/subscriptions/s/rg/r", ["Cpu"], PASTED))
    assert res.ok is False and res.stdout == "" and "token" in res.error.lower()


# --------------------------------------------------------------- run_az_json_capture → REST
def test_diag_settings_pasted_token_uses_rest(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    seen = {}
    async def _fake_diag(token, rid):
        seen.update(token=token, rid=rid)
        return json.dumps({"value": [{"name": "diag1"}]}), None
    monkeypatch.setattr(arm, "get_diagnostic_settings", _fake_diag)

    res = asyncio.run(cr.run_az_json_capture(
        ["monitor", "diagnostic-settings", "list", "--resource", "/r", "-o", "json"], PASTED))
    assert res.ok and seen["rid"] == "/r"
    assert json.loads(res.stdout)["value"][0]["name"] == "diag1"


def test_metric_definitions_pasted_token_uses_rest(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    async def _fake_defs(token, rid):
        return json.dumps([{"name": {"value": "Cpu"}}]), None  # bare list (CLI shape)
    monkeypatch.setattr(arm, "get_metric_definitions", _fake_defs)

    res = asyncio.run(cr.run_az_json_capture(
        ["monitor", "metrics", "list-definitions", "--resource", "/r"], PASTED))
    assert res.ok and isinstance(json.loads(res.stdout), list)


def test_az_rest_arm_url_pasted_token_uses_rest(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    seen = {}
    async def _fake_rest(token, method, url, body=None):
        seen.update(method=method, url=url)
        return json.dumps({"value": [1, 2]}), None
    monkeypatch.setattr(arm, "arm_rest", _fake_rest)

    res = asyncio.run(cr.run_az_json_capture(
        ["rest", "--method", "get", "--url", "https://management.azure.com/x/ProactiveDetectionConfigs"], PASTED))
    assert res.ok and seen["method"] == "get" and "management.azure.com" in seen["url"]


def test_keyvault_pasted_token_fails_closed(monkeypatch):
    """Key Vault data-plane can't be served by a pasted ARM token → clear fail-closed message."""
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    res = asyncio.run(cr.run_az_json_capture(
        ["keyvault", "secret", "list", "--vault-name", "v"], PASTED))
    assert res.ok is False and "key vault" in res.error.lower()


def test_az_rest_nonarm_url_pasted_token_fails_closed(monkeypatch):
    """An `az rest` to a non-ARM url (e.g. Graph) can't be served by an ARM token → fail closed."""
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    res = asyncio.run(cr.run_az_json_capture(
        ["rest", "--method", "get", "--url", "https://graph.microsoft.com/v1.0/applications"], PASTED))
    assert res.ok is False


def test_run_az_json_capture_sp_uses_cli(monkeypatch):
    import asyncio
    called = {"cli": False}
    async def _fake_stream(argv_tail, connection, *, label, session_config_dir=None, **k):
        called["cli"] = True
        yield {"type": "stdout", "text": "{}"}
        yield {"type": "exit", "code": 0}
    monkeypatch.setattr(cr, "_run_az_argv_stream", _fake_stream)
    asyncio.run(cr.run_az_json_capture(["monitor", "diagnostic-settings", "list", "--resource", "/r"], SP))
    assert called["cli"] is True


# --------------------------------------------------------- LA / App Insights fail-closed
def test_run_la_capture_pasted_token_fails_closed(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    res = asyncio.run(cr.run_la_capture("Heartbeat | take 1", "ws", PASTED))
    assert res.ok is False and "log analytics" in res.error.lower()


def test_run_app_insights_capture_pasted_token_fails_closed(monkeypatch):
    import asyncio
    monkeypatch.setattr(creds, "get_arm_token", _arm_token_ok())
    res = asyncio.run(cr.run_app_insights_capture("requests | take 1", "app", PASTED))
    assert res.ok is False and "application insights" in res.error.lower()


def test_la_sp_uses_cli(monkeypatch):
    import asyncio
    called = {"cli": False}
    async def _fake_stream(argv_tail, connection, *, label, session_config_dir=None, **k):
        called["cli"] = True
        yield {"type": "stdout", "text": "[]"}
        yield {"type": "exit", "code": 0}
    monkeypatch.setattr(cr, "_run_az_argv_stream", _fake_stream)
    asyncio.run(cr.run_la_capture("Heartbeat | take 1", "ws", SP))
    assert called["cli"] is True


# --------------------------------------------------------------------------- arm.py helpers
def test_get_metrics_builds_rest_params(monkeypatch):
    import asyncio
    seen = {}
    async def _fake_get(token, path, params):
        seen.update(path=path, params=params)
        return {"value": [{"timeseries": []}]}, None
    monkeypatch.setattr(arm, "_get", _fake_get)
    text, err = asyncio.run(arm.get_metrics(
        "TOK", "/subscriptions/s/rg/r", metricnames=["A", "B"], aggregations=["Total", "Average"],
        interval="PT5M", start_time="2026-06-21T00:00:00Z", end_time="2026-06-21T01:00:00Z",
        dimension_filter="StatusCode eq '403'"))
    assert err is None
    assert seen["path"].endswith("/providers/microsoft.insights/metrics")
    assert seen["params"]["metricnames"] == "A,B"
    assert seen["params"]["aggregation"] == "Total,Average"
    assert seen["params"]["timespan"] == "2026-06-21T00:00:00Z/2026-06-21T01:00:00Z"
    assert seen["params"]["$filter"] == "StatusCode eq '403'"


def test_get_metric_definitions_unwraps_value_to_bare_list(monkeypatch):
    """The metric-definitions parser requires a LIST; REST returns {value:[…]} → must unwrap."""
    import asyncio
    async def _fake_get(token, path, params):
        return {"value": [{"name": {"value": "Cpu"}}]}, None
    monkeypatch.setattr(arm, "_get", _fake_get)
    text, err = asyncio.run(arm.get_metric_definitions("TOK", "/r"))
    assert err is None and isinstance(json.loads(text), list)


def test_arm_rest_passthrough_get_and_error(monkeypatch):
    import asyncio

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload
            self.text = json.dumps(payload)
        def json(self):
            return self._p

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def request(self, method, url, headers=None, json=None):
            return _Resp(200, {"ok": True})
    monkeypatch.setattr(arm.httpx, "AsyncClient", _Client)
    text, err = asyncio.run(arm.arm_rest("TOK", "POST", "https://management.azure.com/x", {"q": 1}))
    assert err is None and json.loads(text)["ok"] is True


def test_is_arm_url():
    assert arm.is_arm_url("https://management.azure.com/subscriptions/x") is True
    assert arm.is_arm_url("https://api.loganalytics.io/x") is False
    assert arm.is_arm_url("") is False
