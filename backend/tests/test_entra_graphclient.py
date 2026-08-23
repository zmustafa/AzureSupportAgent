"""Unit tests for the shared Entra Graph client.

Everything here runs against a fake transport — the point is to prove paging, `$batch`
splitting, id chunking, throttling and fail-open behavior without touching Microsoft Graph.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.entra import graphclient as gc
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError, GraphRequest


class FakeTransport(httpx.AsyncBaseTransport):
    """Scripted responses keyed by (method, path-ish substring)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        body = json.loads(request.content) if request.content else None
        return self.handler(request, body)


def _client(handler, **kw) -> GraphClient:
    client = GraphClient({"auth_method": "service_principal"}, **kw)
    client._token = "header.eyJyb2xlcyI6W119.sig"  # noqa: SLF001 - bypass token acquisition
    client._client = httpx.AsyncClient(transport=FakeTransport(handler), timeout=5)  # noqa: SLF001
    return client


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_get_all_follows_next_link():
    pages = {
        0: {"value": [{"id": "a"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?page=2"},
        1: {"value": [{"id": "b"}, {"id": "c"}]},
    }
    state = {"n": 0}

    def handler(request, body):  # noqa: ARG001
        page = pages[state["n"]]
        state["n"] += 1
        return _json(page)

    async def run():
        client = _client(handler)
        try:
            return await client.get_all("/users", select=["id"])
        finally:
            await client.aclose()

    items, truncated = asyncio.run(run())
    assert [i["id"] for i in items] == ["a", "b", "c"]
    assert truncated is False


def test_get_all_reports_truncation_rather_than_silently_capping():
    """A silently short list is worse than an explicit partial — the ARG truncation lesson."""
    def handler(request, body):  # noqa: ARG001
        return _json({"value": [{"id": str(i)} for i in range(50)],
                      "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?page=2"})

    async def run():
        client = _client(handler)
        try:
            return await client.get_all("/users", select=["id"], max_items=10)
        finally:
            await client.aclose()

    items, truncated = asyncio.run(run())
    assert len(items) == 10
    assert truncated is True


def test_get_page_returns_count_and_validated_continuation():
    def handler(request, body):  # noqa: ARG001
        assert request.headers["ConsistencyLevel"] == "eventual"
        assert request.url.params["$count"] == "true"
        return _json({
            "@odata.count": 1200,
            "value": [{"id": "a"}, {"id": "b"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/applications?$skiptoken=next",
        })

    async def run():
        client = _client(handler)
        try:
            return await client.get_page(
                "applications", select=["id"], top=250, include_count=True,
            )
        finally:
            await client.aclose()

    page = asyncio.run(run())
    assert [item["id"] for item in page.items] == ["a", "b"]
    assert page.total == 1200
    assert "$skiptoken=next" in page.next_link


def test_get_page_refuses_foreign_or_wrong_collection_continuation_before_request():
    calls = 0

    def handler(request, body):  # noqa: ARG001
        nonlocal calls
        calls += 1
        return _json({"value": []})

    async def run(url: str):
        client = _client(handler)
        try:
            await client.get_page("applications", next_link=url)
        finally:
            await client.aclose()

    for url in (
        "https://example.invalid/v1.0/applications?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/users?$skiptoken=x",
        "http://graph.microsoft.com/v1.0/applications?$skiptoken=x",
        "https://graph.microsoft.com@evil.invalid/v1.0/applications?$skiptoken=x",
    ):
        with pytest.raises(GraphError):
            asyncio.run(run(url))
    assert calls == 0


def test_get_page_reports_retry_after_throttling(monkeypatch):
    slept: list[float] = []
    retries: list[tuple[int, int, float]] = []
    state = {"calls": 0}

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)

    def handler(request, body):  # noqa: ARG001
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"message": "slow"}})
        return _json({"value": [{"id": "a"}]})

    async def run():
        client = _client(handler)
        try:
            async def on_retry(status: int, attempt: int, delay: float):
                retries.append((status, attempt, delay))
            return await client.get_page("applications", on_retry=on_retry)
        finally:
            await client.aclose()

    page = asyncio.run(run())
    assert page.items == [{"id": "a"}]
    assert retries and retries[0][0:2] == (429, 1)
    assert 3 <= retries[0][2] <= 3.5
    # The retry also re-enters the adaptive gate, which holds callers off for the same
    # Retry-After window. Under a real clock that second wait is already elapsed and costs
    # nothing; here `sleep` is faked so time never moves and it is recorded again.
    assert slept[0] == retries[0][2]


def test_get_count_reads_plain_integer_with_eventual_consistency():
    def handler(request, body):  # noqa: ARG001
        assert request.url.path == "/v1.0/applications/$count"
        assert request.headers["ConsistencyLevel"] == "eventual"
        assert request.headers["Accept"] == "text/plain"
        return httpx.Response(200, text="1234")

    async def run():
        client = _client(handler)
        try:
            return await client.get_count("applications")
        finally:
            await client.aclose()

    assert asyncio.run(run()) == 1234


def test_batch_splits_at_twenty_and_preserves_order():
    seen_sizes: list[int] = []

    def handler(request, body):
        seen_sizes.append(len(body["requests"]))
        return _json({"responses": [
            {"id": r["id"], "status": 200, "body": {"value": [{"id": f"obj-{r['id']}"}]}}
            for r in body["requests"]
        ]})

    async def run():
        client = _client(handler)
        try:
            reqs = [GraphRequest(id=str(i), url=f"/groups/{i}/owners") for i in range(45)]
            return await client.batch(reqs)
        finally:
            await client.aclose()

    responses = asyncio.run(run())
    assert seen_sizes == [20, 20, 5]
    assert [r.id for r in responses] == [str(i) for i in range(45)]
    assert responses[3].value() == [{"id": "obj-3"}]


def test_batch_sub_request_403_does_not_lose_the_rest():
    def handler(request, body):
        out = []
        for i, r in enumerate(body["requests"]):
            if i == 1:
                out.append({"id": r["id"], "status": 403, "body": {"error": {"message": "no"}}})
            else:
                out.append({"id": r["id"], "status": 200, "body": {"value": [{"id": "ok"}]}})
        return _json({"responses": out})

    async def run():
        client = _client(handler)
        try:
            return await client.batch([GraphRequest(id=str(i), url="/x") for i in range(3)])
        finally:
            await client.aclose()

    responses = asyncio.run(run())
    assert responses[1].forbidden
    assert responses[0].value() and responses[2].value()


def test_batch_sub_request_retry_honours_header_and_reports_progress(monkeypatch):
    slept: list[float] = []
    retries: list[tuple[int, int, float]] = []
    calls = 0

    async def fake_sleep(delay: float):
        slept.append(delay)

    monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gc.random, "uniform", lambda *_args: 0.0)

    def handler(request, body):  # noqa: ARG001
        nonlocal calls
        calls += 1
        row = body["requests"][0]
        if calls == 1:
            return _json({"responses": [{
                "id": row["id"], "status": 429,
                "headers": {"Retry-After": "4"},
                "body": {"error": {"message": "slow"}},
            }]})
        return _json({"responses": [{
            "id": row["id"], "status": 200, "body": {"value": [{"id": "ok"}]},
        }]})

    async def run():
        client = _client(handler)
        try:
            async def on_retry(status: int, attempt: int, delay: float):
                retries.append((status, attempt, delay))
            return await client.batch([GraphRequest(id="0", url="/objects/0")], on_retry=on_retry)
        finally:
            await client.aclose()

    responses = asyncio.run(run())
    assert responses[0].ok
    assert calls == 2
    # See the note in test_get_page_reports_retry_after_throttling: the retry re-enters the
    # adaptive gate, which under a faked clock records the same window a second time.
    assert slept[0] == 4.0
    assert retries == [(429, 1, 4.0)]


def test_batch_sub_request_retry_is_bounded(monkeypatch):
    calls = 0

    async def fake_sleep(_delay: float):
        return None

    monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gc.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(gc, "_MAX_RETRIES", 2)

    def handler(request, body):  # noqa: ARG001
        nonlocal calls
        calls += 1
        row = body["requests"][0]
        return _json({"responses": [{
            "id": row["id"], "status": 429,
            "headers": {"Retry-After": "1"}, "body": {"error": {"message": "slow"}},
        }]})

    async def run():
        client = _client(handler)
        try:
            return await client.batch([GraphRequest(id="0", url="/objects/0")])
        finally:
            await client.aclose()

    responses = asyncio.run(run())
    assert calls == 3
    assert responses[0].status == 429


def test_get_by_ids_chunks_at_one_thousand():
    chunk_sizes: list[int] = []

    def handler(request, body):
        chunk_sizes.append(len(body["ids"]))
        return _json({"value": [{"id": i, "displayName": i} for i in body["ids"]]})

    async def run():
        client = _client(handler)
        try:
            return await client.get_by_ids([f"id-{i}" for i in range(2500)], ["user"])
        finally:
            await client.aclose()

    resolved = asyncio.run(run())
    assert chunk_sizes == [1000, 1000, 500]
    assert len(resolved) == 2500


def test_retry_after_is_honoured_on_429(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)
    state = {"n": 0}

    def handler(request, body):  # noqa: ARG001
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"message": "slow down"}})
        return _json({"value": [{"id": "a"}]})

    async def run():
        client = _client(handler)
        try:
            return await client.get_all("/users", select=["id"])
        finally:
            await client.aclose()

    items, _ = asyncio.run(run())
    assert [i["id"] for i in items] == ["a"]
    # Retry-After honoured, plus a small jitter so parallel collectors do not retry in lockstep.
    assert slept and 7.0 <= slept[0] <= 7.5


def test_forbidden_raises_permission_error_for_fail_open():
    def handler(request, body):  # noqa: ARG001
        return httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})

    async def run():
        client = _client(handler)
        try:
            await client.get_all("/identityProtection/riskyUsers")
        finally:
            await client.aclose()

    with pytest.raises(GraphPermissionError) as exc:
        asyncio.run(run())
    assert "Insufficient privileges" in exc.value.message
    # Collectors rely on this being a distinct type so one blind domain degrades one pillar.
    assert exc.value.status == 403


def test_delta_returns_token_and_resyncs_on_rejection():
    state = {"n": 0}

    def handler(request, body):  # noqa: ARG001
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(410, json={"error": {"code": "resyncRequired", "message": "gone"}})
        return _json({"value": [{"id": "u1"}], "@odata.deltaLink": "https://graph/delta?$deltatoken=NEW"})

    async def run():
        client = _client(handler)
        try:
            return await client.delta("/users", "OLD-TOKEN")
        finally:
            await client.aclose()

    items, token, resynced = asyncio.run(run())
    assert resynced is True
    assert [i["id"] for i in items] == ["u1"]
    assert "NEW" in token


def test_stats_are_recorded():
    def handler(request, body):  # noqa: ARG001
        return _json({"value": [{"id": "a"}, {"id": "b"}]})

    async def run():
        client = _client(handler)
        try:
            await client.get_all("/users", select=["id"])
            return client.stats.as_dict()
        finally:
            await client.aclose()

    stats = asyncio.run(run())
    assert stats["requests"] == 1 and stats["pages"] == 1 and stats["items"] == 2


def test_beta_is_opt_in():
    async def run():
        off = GraphClient({}, beta=False)
        on = GraphClient({}, beta=True)
        return off.base(beta=True), on.base(beta=True), off.beta_available(True)

    off_base, on_base, available = asyncio.run(run())
    assert off_base == gc.GRAPH_V1        # beta requested but disabled -> stays on v1.0
    assert on_base == gc.GRAPH_BETA
    assert available is False
