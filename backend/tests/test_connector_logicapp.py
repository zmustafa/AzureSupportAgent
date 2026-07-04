"""Azure Logic Apps connector (app.connectors.logicapp).

Pins: host restriction to *.logic.azure.com, HTTPS enforcement, the {title, message,
severity, facts} envelope vs an explicit payload override, and success on 2xx (incl. 202).
"""
import asyncio
import json

import httpx

from app.connectors import logicapp

_GOOD_URL = (
    "https://prod-12.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"
    "?api-version=2016-10-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=SECRETSIG"
)


def _run(coro):
    return asyncio.run(coro)


def test_valid_trigger_url_accepts_logic_azure_com():
    assert logicapp._valid_trigger_url(_GOOD_URL) is None


def test_valid_trigger_url_rejects_non_logic_host():
    assert logicapp._valid_trigger_url("https://evil.example.com/hook") is not None


def test_valid_trigger_url_rejects_http():
    assert logicapp._valid_trigger_url("http://prod-1.eastus.logic.azure.com/x") is not None


def test_payload_uses_explicit_override():
    out = logicapp._payload({}, {"payload": {"a": 1}, "title": "ignored"})
    assert out == {"a": 1}


def test_payload_builds_envelope():
    out = logicapp._payload({}, {"title": "T", "message": "M", "severity": "warning"})
    assert out == {"title": "T", "message": "M", "severity": "warning", "facts": {}}


def test_payload_merges_static_additions_with_caller_winning():
    out = logicapp._payload(
        {"static_payload": "source=agent\ntitle=default"},
        {"title": "T", "message": "M"},
    )
    assert out["source"] == "agent"
    assert out["title"] == "T"  # caller value wins over static default


def test_trigger_http_posts_and_succeeds(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 202
        text = ""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["body"] = json.loads(content)
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("app.core.ssrf.check_url", lambda url, require_https=True: None)

    res = _run(logicapp._trigger_http(
        {"trigger_url": _GOOD_URL, "headers": "X-Source: azsupagent"},
        {"title": "Hello", "message": "World"},
    ))
    assert res["isError"] is False
    assert "202" in res["content"][0]
    assert captured["body"]["title"] == "Hello"
    assert captured["headers"]["X-Source"] == "azsupagent"


def test_trigger_http_surfaces_response_body(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"ok": true, "runId": "abc"}'

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("app.core.ssrf.check_url", lambda url, require_https=True: None)

    res = _run(logicapp._trigger_http({"trigger_url": _GOOD_URL}, {"message": "hi"}))
    assert res["isError"] is False
    assert "runId" in res["content"][0]


def test_trigger_http_rejects_bad_host(monkeypatch):
    res = _run(logicapp._trigger_http({"trigger_url": "https://evil.example.com/x"}, {"message": "hi"}))
    assert res["isError"] is True


def test_trigger_http_requires_url():
    res = _run(logicapp._trigger_http({}, {"message": "hi"}))
    assert res["isError"] is True
