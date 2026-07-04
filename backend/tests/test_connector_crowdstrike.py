"""CrowdStrike Next-Gen SIEM connector (app.connectors.crowdstrike_ngsiem).

Pins: host restriction to CrowdStrike/LogScale domains, HTTPS enforcement, bearer-token
auth, and the HEC {"event": ...} body shape (envelope vs explicit event).
"""
import asyncio
import json

import httpx

from app.connectors import crowdstrike_ngsiem as cs

_GOOD_URL = "https://acme.crowdstrike.com/api/v1/ingest/hec"


def _run(coro):
    return asyncio.run(coro)


def test_valid_ingest_url_accepts_crowdstrike():
    assert cs._valid_ingest_url(_GOOD_URL) is None


def test_valid_ingest_url_accepts_humio():
    assert cs._valid_ingest_url("https://acme.humio.com/api/v1/ingest/hec") is None


def test_valid_ingest_url_rejects_non_allowed_host():
    assert cs._valid_ingest_url("https://evil.example.com/x") is not None


def test_valid_ingest_url_rejects_http():
    assert cs._valid_ingest_url("http://acme.crowdstrike.com/x") is not None


def test_payload_envelope_wrapped_in_event():
    body = cs._payload({"title": "T", "message": "M"})
    assert body == {"event": {"title": "T", "message": "M", "severity": "info", "facts": {}}}


def test_payload_explicit_event_and_fields():
    body = cs._payload({"event": {"a": 1}, "fields": {"host": "vm1"}})
    assert body == {"event": {"a": 1}, "fields": {"host": "vm1"}}


def test_send_event_posts_with_bearer(monkeypatch):
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

    res = _run(cs._send_event(
        {"ingest_url": _GOOD_URL, "ingest_token": "TESTTOKEN"},
        {"title": "Hello", "message": "World"},
    ))
    assert res["isError"] is False
    assert captured["headers"]["Authorization"] == "Bearer TESTTOKEN"
    assert captured["body"]["event"]["title"] == "Hello"


def test_send_event_rejects_bad_host(monkeypatch):
    res = _run(cs._send_event(
        {"ingest_url": "https://evil.example.com/x", "ingest_token": "t"}, {"message": "hi"}
    ))
    assert res["isError"] is True


def test_send_event_requires_url_and_token():
    assert _run(cs._send_event({"ingest_url": _GOOD_URL}, {"message": "hi"}))["isError"] is True
    assert _run(cs._send_event({"ingest_token": "t"}, {"message": "hi"}))["isError"] is True
