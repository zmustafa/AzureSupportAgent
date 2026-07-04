"""Sumo Logic connector (app.connectors.sumologic).

Pins: host restriction to *.sumologic.com, HTTPS enforcement, the {title, message,
severity, facts} envelope vs an explicit event, and newline-delimited batching.
"""
import asyncio
import json

import httpx

from app.connectors import sumologic

_GOOD_URL = "https://endpoint4.collection.sumologic.com/receiver/v1/http/ZaVnC4dhaV1abc"


def _run(coro):
    return asyncio.run(coro)


def test_valid_source_url_accepts_sumologic():
    assert sumologic._valid_source_url(_GOOD_URL) is None


def test_valid_source_url_rejects_non_sumo_host():
    assert sumologic._valid_source_url("https://evil.example.com/x") is not None


def test_valid_source_url_rejects_http():
    assert sumologic._valid_source_url("http://endpoint4.collection.sumologic.com/x") is not None


def test_body_envelope():
    body = sumologic._body({"title": "T", "message": "M", "severity": "warning"})
    assert json.loads(body) == {"title": "T", "message": "M", "severity": "warning", "facts": {}}


def test_body_explicit_event_object():
    body = sumologic._body({"event": {"a": 1}})
    assert json.loads(body) == {"a": 1}


def test_body_list_is_newline_delimited():
    body = sumologic._body({"event": [{"a": 1}, {"b": 2}]}).decode()
    assert body == '{"a": 1}\n{"b": 2}'


def test_send_event_posts_with_category(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 200
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

    res = _run(sumologic._send_event(
        {"source_url": _GOOD_URL, "source_category": "azure/agent"},
        {"title": "Hello", "message": "World"},
    ))
    assert res["isError"] is False
    assert captured["headers"]["X-Sumo-Category"] == "azure/agent"
    assert captured["body"]["title"] == "Hello"


def test_send_event_rejects_bad_host(monkeypatch):
    res = _run(sumologic._send_event({"source_url": "https://evil.example.com/x"}, {"message": "hi"}))
    assert res["isError"] is True


def test_send_event_requires_url():
    res = _run(sumologic._send_event({}, {"message": "hi"}))
    assert res["isError"] is True
