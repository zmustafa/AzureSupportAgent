"""ChatGPT Codex provider — robustness of the streaming request.

Offline: httpx is monkeypatched, so nothing here talks to ChatGPT. Focus: the Codex backend
rejects ``max_output_tokens`` with a 400 {"detail":"Unsupported parameter: ..."}; the provider
must drop that cap and retry instead of failing the turn (this is what was breaking
architecture/memory generation).
"""
from __future__ import annotations

import asyncio

from app.agent import codex_provider as cp


class _Resp:
    def __init__(self, status, body="", lines=None):
        self.status_code = status
        self._body = body
        self._lines = lines or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return self._body.encode("utf-8")

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


def _fake_client(responses: list[_Resp], sent: list[dict]):
    state = {"i": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, _method, _url, json=None, headers=None):  # noqa: A002
            sent.append(dict(json or {}))
            r = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            return r

    return _Client


def _drain(prov, **kw):
    async def run():
        toks = []
        async for ev in prov.stream([{"role": "user", "content": "hi"}], **kw):
            if ev.type == "token":
                toks.append(ev.text)
        return toks

    return asyncio.run(run())


def test_codex_drops_max_output_tokens_and_retries_on_400(monkeypatch):
    sent: list[dict] = []
    responses = [
        _Resp(400, body='{"detail":"Unsupported parameter: max_output_tokens"}'),
        _Resp(200, lines=["event: response.output_text.delta", 'data: {"delta": "hello world"}']),
    ]
    monkeypatch.setattr(cp.httpx, "AsyncClient", _fake_client(responses, sent))

    prov = cp.CodexProvider(model="gpt-5.5", api_key="override-token")  # override token => no oauth
    toks = _drain(prov, max_tokens=16000)

    # First attempt carried the cap; after the 400 it was dropped and the retry succeeded.
    assert len(sent) == 2
    assert "max_output_tokens" in sent[0]
    assert "max_output_tokens" not in sent[1]
    assert "".join(toks) == "hello world"


def test_codex_keeps_cap_when_accepted(monkeypatch):
    sent: list[dict] = []
    responses = [_Resp(200, lines=["event: response.output_text.delta", 'data: {"delta": "ok"}'])]
    monkeypatch.setattr(cp.httpx, "AsyncClient", _fake_client(responses, sent))

    prov = cp.CodexProvider(model="gpt-5.5", api_key="override-token")
    toks = _drain(prov, max_tokens=16000)

    assert len(sent) == 1  # no retry
    assert sent[0].get("max_output_tokens") == 16000
    assert "".join(toks) == "ok"


def test_codex_other_400_still_raises(monkeypatch):
    sent: list[dict] = []
    responses = [_Resp(400, body='{"detail":"Some other problem"}')]
    monkeypatch.setattr(cp.httpx, "AsyncClient", _fake_client(responses, sent))

    prov = cp.CodexProvider(model="gpt-5.5", api_key="override-token")
    raised = False
    try:
        _drain(prov, max_tokens=16000)
    except RuntimeError as exc:
        raised = True
        assert "400" in str(exc)
    assert raised
    assert len(sent) == 1  # not retried
