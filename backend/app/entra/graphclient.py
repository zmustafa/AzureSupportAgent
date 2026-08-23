"""Shared Microsoft Graph client for tenant-wide Entra collection.

The rest of the app talks to Graph either through the vendored stdio MCP server (one
object per call) or ad-hoc ``httpx`` calls. Neither can page, batch or survive throttling,
which caps app-registration collection at 200 objects today. This client is the P0
prerequisite for every Entra collector.

Capabilities
------------
* **Paging** — follows ``@odata.nextLink`` with an optional ``max_items`` cap that is
  reported (never silently truncating; a silently short list is worse than an error).
* **Selection** — every call sends ``$select`` so we never drag full objects across.
* **Advanced queries** — ``advanced=True`` adds ``ConsistencyLevel: eventual`` + ``$count``
  which Graph requires for ``not`` / ``endsWith`` / ``$search`` filters.
* **Batching** — ``POST /$batch`` at the Graph maximum of 20 sub-requests, with per
  sub-request status and per sub-request 429 retry.
* **Bulk resolve** — ``POST /directoryObjects/getByIds`` chunked at the Graph maximum 1000.
* **Throttling** — honours ``Retry-After`` exactly, exponential backoff with jitter on
  429/503/504, and an adaptive concurrency gate that halves its width on 429 and widens
  again as requests succeed.
* **Delta** — ``/delta`` with token round-trip for the large collections.
* **Fail-open** — a 403 raises :class:`GraphPermissionError` which collectors catch to mark
  their domain blind; the rest of the snapshot still builds.

Read-only by construction: the only exposed verbs are GET and the two read-only POSTs
(``$batch`` of GETs, ``getByIds``). There is deliberately no ``post`` / ``patch`` /
``delete`` helper — this product never writes to the directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urlsplit

import httpx

from app.azure.credentials import get_graph_token

log = logging.getLogger("app.entra.graphclient")

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

BATCH_MAX = 20          # Graph JSON batching hard limit
GETBYIDS_MAX = 1000     # directoryObjects/getByIds hard limit
_MAX_RETRIES = 5
_MAX_BACKOFF_S = 60.0
_DEFAULT_TIMEOUT = 60.0
_GRAPH_HOST = "graph.microsoft.com"
#: Consecutive clean responses before the adaptive gate widens by one.
_GATE_GROW_AFTER = 8


# --------------------------------------------------------------------------- errors
class GraphError(Exception):
    """A Graph call failed in a way the caller may want to record."""

    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.status} {self.message}" + (f" ({self.path})" if self.path else "")


class GraphAuthError(GraphError):
    """No usable Graph token for this connection (401 / token acquisition failure)."""


class GraphPermissionError(GraphError):
    """403 — the identity lacks the permission this call needs.

    Collectors catch this and mark their domain ``blind`` with the scope name, rather than
    failing the whole snapshot."""

    def __init__(self, message: str, path: str = "", scope: str = "") -> None:
        super().__init__(403, message, path)
        self.scope = scope


# --------------------------------------------------------------------------- stats
@dataclass
class GraphStats:
    """Per-client instrumentation, surfaced by ``GET /entra/diagnostics``."""

    requests: int = 0
    batches: int = 0
    batch_subrequests: int = 0
    pages: int = 0
    items: int = 0
    throttled: int = 0
    retries: int = 0
    forbidden: int = 0
    ms: float = 0.0
    truncated_calls: int = 0
    top_retries: int = 0
    #: Times the adaptive gate halved its width, and the narrowest it reached.
    gate_narrowed: int = 0
    gate_min_limit: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "batches": self.batches,
            "batch_subrequests": self.batch_subrequests,
            "pages": self.pages,
            "items": self.items,
            "throttled": self.throttled,
            "retries": self.retries,
            "forbidden": self.forbidden,
            "ms": round(self.ms, 1),
            "truncated_calls": self.truncated_calls,
            "top_retries": self.top_retries,
            "gate_narrowed": self.gate_narrowed,
            "gate_min_limit": self.gate_min_limit,
            "errors": self.errors[:20],
        }


@dataclass
class GraphRequest:
    """One sub-request inside a ``$batch``."""

    id: str
    url: str                      # relative, e.g. "/applications/{id}/owners?$select=id"
    method: str = "GET"
    headers: dict[str, str] | None = None


@dataclass
class GraphResponse:
    """One sub-response from a ``$batch``."""

    id: str
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    throttled: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def forbidden(self) -> bool:
        return self.status == 403

    def value(self) -> list[dict[str, Any]]:
        """``value`` array of a successful collection sub-response (empty otherwise)."""
        if self.ok and isinstance(self.body, dict):
            v = self.body.get("value")
            if isinstance(v, list):
                return v
        return []


@dataclass
class GraphPage:
    """One page of a Graph collection plus its opaque continuation metadata."""

    items: list[dict[str, Any]]
    next_link: str = ""
    total: int | None = None


# --------------------------------------------------------------------------- throttle gate
class AdaptiveGate:
    """Concurrency gate that narrows on 429 and widens again as requests succeed.

    A plain semaphore is not enough once the fan-out is wide. Each request backs off in
    isolation, but the semaphore immediately admits a replacement, so a throttled tenant
    keeps being hammered right through the ``Retry-After`` window it just asked us to wait
    out. This shares one verdict across every caller on the client: multiplicative decrease
    on 429, additive increase on sustained success — the control law TCP uses.
    """

    def __init__(self, ceiling: int, *, floor: int = 1) -> None:
        self.ceiling = max(1, int(ceiling))
        self.floor = max(1, min(int(floor), self.ceiling))
        self._limit = float(self.ceiling)
        self._in_flight = 0
        self._resume_at = 0.0
        self._streak = 0
        self._cond = asyncio.Condition()
        self.narrowed = 0
        self.min_limit = self.ceiling

    @property
    def limit(self) -> int:
        return max(self.floor, int(self._limit))

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def acquire(self) -> None:
        pause = self._resume_at - self._now()
        if pause > 0:
            # Everyone waits out the window, not just the request that was refused. One
            # sleep, never a re-check loop: re-checking spins hard if the clock does not
            # advance, and a 429 arriving while we queue is already covered by the
            # narrowed width plus that request's own backoff.
            await asyncio.sleep(min(pause, _MAX_BACKOFF_S))
        async with self._cond:
            while self._in_flight >= self.limit:
                await self._cond.wait()
            self._in_flight += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    def record_ok(self) -> None:
        self._streak += 1
        if self._streak >= _GATE_GROW_AFTER and self._limit < self.ceiling:
            self._streak = 0
            self._limit = min(float(self.ceiling), self._limit + 1.0)

    def record_throttled(self, retry_after: float = 0.0) -> None:
        """Halve the width and, when Graph named a wait, hold every caller off for it."""
        self._streak = 0
        was = self.limit
        self._limit = max(float(self.floor), self._limit / 2.0)
        if self.limit < was:
            self.narrowed += 1
            self.min_limit = min(self.min_limit, self.limit)
        if retry_after > 0:
            self._resume_at = max(self._resume_at, self._now() + min(retry_after, _MAX_BACKOFF_S))


# --------------------------------------------------------------------------- client
class GraphClient:
    """Bounded, throttle-aware Microsoft Graph reader for one Azure connection.

    Use as an async context manager so the underlying HTTP pool is closed::

        async with GraphClient(conn) as gc:
            users = await gc.get_all("/users", select=["id", "displayName"])
    """

    # Default in-flight request ceiling. Collectors that fan out (batched $batch chunks,
    # sharded sign-in windows) size themselves against this so they cannot starve the
    # domains sharing the same client.
    MAX_CONCURRENCY = 8

    def __init__(
        self,
        conn: dict[str, Any] | None,
        *,
        concurrency: int = MAX_CONCURRENCY,
        beta: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._conn = conn or {}
        self._beta_enabled = bool(beta)
        self._gate = AdaptiveGate(max(1, int(concurrency)))
        self._timeout = timeout
        self._token: str | None = None
        self._token_error: str = ""
        self._client: httpx.AsyncClient | None = None
        self.stats = GraphStats()
        self.stats.gate_min_limit = self._gate.ceiling

    # -- lifecycle ---------------------------------------------------------------
    async def __aenter__(self) -> "GraphClient":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @property
    def beta_enabled(self) -> bool:
        return self._beta_enabled

    def base(self, beta: bool = False) -> str:
        return GRAPH_BETA if (beta and self._beta_enabled) else GRAPH_V1

    def beta_available(self, beta: bool) -> bool:
        """True when a beta-only call may proceed (beta requested AND enabled)."""
        return (not beta) or self._beta_enabled

    # -- token -------------------------------------------------------------------
    async def token(self) -> str:
        """Acquire (once) and cache the Graph token for this connection."""
        if self._token:
            return self._token
        tok, err = await get_graph_token(self._conn)
        if not tok:
            self._token_error = err or "Could not acquire a Microsoft Graph token."
            raise GraphAuthError(401, self._token_error)
        self._token = tok
        return tok

    async def probe_token(self) -> tuple[str | None, str]:
        """Non-raising token acquisition — returns ``(token, error)``."""
        try:
            return await self.token(), ""
        except GraphAuthError as exc:
            return None, exc.message

    # -- core request ------------------------------------------------------------
    async def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        on_retry: Callable[[int, int, float], Awaitable[None]] | None = None,
    ) -> httpx.Response:
        """Issue one request with retry/backoff. Raises on 401/403 and terminal failures."""
        tok = await self.token()
        hdrs = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"

        attempt = 0
        while True:
            started = time.monotonic()
            netfail: Exception | None = None
            await self._gate.acquire()
            try:
                resp = await self._http().request(method, url, json=json_body, headers=hdrs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                netfail = exc
            finally:
                # Freed before any backoff sleep, so a retry never squats on a permit.
                await self._gate.release()

            if netfail is not None:
                self.stats.ms += (time.monotonic() - started) * 1000
                attempt += 1
                if attempt > _MAX_RETRIES:
                    raise GraphError(0, f"Network error after {attempt} attempts: {netfail}", url) from netfail
                self.stats.retries += 1
                delay = self._backoff(attempt, None)
                if on_retry is not None:
                    try:
                        await on_retry(0, attempt, delay)
                    except Exception:  # noqa: BLE001 - telemetry must not break a request
                        pass
                await asyncio.sleep(delay)
                continue

            self.stats.requests += 1
            self.stats.ms += (time.monotonic() - started) * 1000

            if resp.status_code in (429, 503, 504):
                attempt += 1
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                if resp.status_code == 429:
                    self.stats.throttled += 1
                    # Narrow the whole client, not just this request's next attempt.
                    self._throttle(delay)
                if attempt > _MAX_RETRIES:
                    raise GraphError(resp.status_code, f"Throttled/unavailable after {attempt} attempts", url)
                self.stats.retries += 1
                if on_retry is not None:
                    try:
                        await on_retry(resp.status_code, attempt, delay)
                    except Exception:  # noqa: BLE001 - telemetry must not break a request
                        pass
                await asyncio.sleep(delay)
                continue

            self._gate.record_ok()

            if resp.status_code == 401:
                # One re-auth attempt (token may have expired mid-collection).
                if attempt == 0 and self._token:
                    attempt += 1
                    self._token = None
                    try:
                        tok = await self.token()
                    except GraphAuthError:
                        raise
                    hdrs["Authorization"] = f"Bearer {tok}"
                    continue
                raise GraphAuthError(401, self._error_text(resp) or "Unauthorized.", url)

            if resp.status_code == 403:
                self.stats.forbidden += 1
                raise GraphPermissionError(self._error_text(resp) or "Forbidden.", url)

            if resp.status_code >= 400:
                raise GraphError(resp.status_code, self._error_text(resp) or resp.reason_phrase, url)

            return resp

    def _throttle(self, delay: float) -> None:
        """Record a 429 against the shared gate and mirror its state into stats."""
        self._gate.record_throttled(delay)
        self.stats.gate_narrowed = self._gate.narrowed
        self.stats.gate_min_limit = self._gate.min_limit

    @property
    def concurrency(self) -> int:
        """Configured in-flight ceiling. Fan-outs should not ask for more than this."""
        return self._gate.ceiling

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """``Retry-After`` when Graph supplies it, else capped exponential with jitter.

        Jitter matters: without it, a fan-out of 8 collectors retries in lockstep and gets
        throttled again at exactly the same moment."""
        delay = 0.0
        if retry_after:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 0.0
        if delay <= 0:
            delay = min(_MAX_BACKOFF_S, 2.0 ** attempt)
        return min(_MAX_BACKOFF_S, delay + random.uniform(0, 0.5))

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or "")[:400]
                return msg
        except (json.JSONDecodeError, ValueError):
            pass
        return (resp.text or "")[:400]

    # -- public reads ------------------------------------------------------------
    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        beta: bool = False,
        advanced: bool = False,
    ) -> dict[str, Any]:
        """GET a single Graph resource and return its JSON body."""
        url = path if path.startswith("http") else f"{self.base(beta)}{path}"
        headers = {"ConsistencyLevel": "eventual"} if advanced else None
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{httpx.QueryParams(params)}"
        resp = await self._send("GET", url, headers=headers)
        body = resp.json() if resp.content else {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def validate_page_link(url: str, *, collection_path: str) -> str:
        """Validate an opaque Graph continuation before attaching the bearer token.

        Checkpoints survive process restarts and are stored on disk. Treat their URL as
        untrusted at the next use: only HTTPS Microsoft Graph links for the same v1.0
        collection are accepted. This prevents a tampered checkpoint from becoming an
        authenticated SSRF request.
        """
        parsed = urlsplit(str(url or ""))
        expected_path = f"/v1.0/{collection_path.strip('/')}"
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() != _GRAPH_HOST
            or parsed.port not in (None, 443)
            or parsed.path.rstrip("/").lower() != expected_path.rstrip("/").lower()
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GraphError(400, "Invalid Microsoft Graph continuation link.", expected_path)
        return url

    async def get_page(
        self,
        path: str,
        *,
        select: Sequence[str] | None = None,
        expand: str | None = None,
        top: int = 250,
        next_link: str = "",
        include_count: bool = False,
        on_retry: Callable[[int, int, float], Awaitable[None]] | None = None,
    ) -> GraphPage:
        """Read exactly one page of a v1.0 Graph collection.

        Unlike :meth:`get_all`, this exposes the continuation to trusted server code so a
        long-running collector can checkpoint after every page and resume after restart.
        The continuation never crosses the public API boundary.
        """
        collection = path.strip("/")
        if not collection or "/" in collection:
            raise ValueError("A top-level Graph collection path is required.")
        headers = {"ConsistencyLevel": "eventual"} if include_count else None
        if next_link:
            url = self.validate_page_link(next_link, collection_path=collection)
        else:
            params: dict[str, Any] = {"$top": max(1, min(999, int(top)))}
            if select:
                params["$select"] = ",".join(select)
            if expand:
                params["$expand"] = expand
            if include_count:
                params["$count"] = "true"
            url = f"{GRAPH_V1}/{collection}?{httpx.QueryParams(params)}"
        resp = await self._send("GET", url, headers=headers, on_retry=on_retry)
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            raise GraphError(502, "Microsoft Graph returned an invalid collection payload.", path)
        raw_items = body.get("value")
        if not isinstance(raw_items, list):
            raise GraphError(502, "Microsoft Graph collection is missing its value array.", path)
        items = [item for item in raw_items if isinstance(item, dict)]
        next_url = str(body.get("@odata.nextLink") or "")
        if next_url:
            self.validate_page_link(next_url, collection_path=collection)
        total_raw = body.get("@odata.count")
        try:
            total = int(total_raw) if total_raw is not None else None
        except (TypeError, ValueError):
            total = None
        self.stats.pages += 1
        self.stats.items += len(items)
        return GraphPage(items=items, next_link=next_url, total=total)

    async def get_count(self, collection: str) -> int | None:
        """Return a top-level v1.0 collection count, or ``None`` if not provided."""
        name = str(collection or "").strip("/")
        if not name or "/" in name:
            raise ValueError("A top-level Graph collection is required.")
        resp = await self._send(
            "GET",
            f"{GRAPH_V1}/{name}/$count",
            headers={"ConsistencyLevel": "eventual", "Accept": "text/plain"},
        )
        try:
            value = int(resp.text.strip())
        except (TypeError, ValueError):
            return None
        return max(0, value)

    async def get_all(
        self,
        path: str,
        *,
        select: Sequence[str] | None = None,
        filter: str | None = None,  # noqa: A002 - mirrors the OData name
        expand: str | None = None,
        orderby: str | None = None,
        search: str | None = None,
        top: int = 999,
        max_items: int | None = None,
        advanced: bool = False,
        beta: bool = False,
        extra_params: dict[str, Any] | None = None,
        on_page: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Page a Graph collection.

        Returns ``(items, truncated)``. ``truncated`` is True when ``max_items`` cut the
        result short — callers MUST propagate it to the domain meta so the UI can say so.

        ``on_page(pages, items)`` is awaited after each page. Sign-in aggregation over a
        busy tenant is 200 sequential pages; without this the progress log sits on one line
        for minutes and is indistinguishable from a hang.
        """
        params: dict[str, Any] = {}
        if select:
            params["$select"] = ",".join(select)
        if filter:
            params["$filter"] = filter
        if expand:
            params["$expand"] = expand
        if orderby:
            params["$orderby"] = orderby
        if search:
            params["$search"] = search
        if top:
            params["$top"] = int(top)
        if advanced:
            params["$count"] = "true"
        if extra_params:
            params.update(extra_params)

        url = f"{self.base(beta)}{path}?{httpx.QueryParams(params)}"
        headers = {"ConsistencyLevel": "eventual"} if advanced else None

        items: list[dict[str, Any]] = []
        truncated = False
        pages = 0
        while url:
            try:
                resp = await self._send("GET", url, headers=headers)
            except GraphError as exc:
                # Several Graph collections (roleDefinitions, authenticationStrengthPolicies,
                # authenticationContextClassReferences, ...) reject `$top` outright with a 400
                # and lose the entire domain. Retrying once without it is cheap and turns a
                # whole-domain failure into a normal read. Verified by measurement.
                if top and not items and _rejects_top(exc):
                    self.stats.top_retries += 1
                    return await self.get_all(
                        path, select=select, filter=filter, expand=expand, orderby=orderby,
                        search=search, top=0, max_items=max_items, advanced=advanced,
                        beta=beta, extra_params=extra_params, on_page=on_page,
                    )
                raise
            self.stats.pages += 1
            pages += 1
            body = resp.json() if resp.content else {}
            page = body.get("value") if isinstance(body, dict) else None
            if isinstance(page, list):
                items.extend(page)
                self.stats.items += len(page)
            if on_page is not None:
                try:
                    await on_page(pages, len(items))
                except Exception:  # noqa: BLE001 - progress is cosmetic, never fail a read
                    pass
            if max_items is not None and len(items) >= max_items:
                del items[max_items:]
                truncated = True
                self.stats.truncated_calls += 1
                break
            url = (body.get("@odata.nextLink") or "") if isinstance(body, dict) else ""
        return items, truncated

    async def batch(
        self,
        requests: Sequence[GraphRequest],
        *,
        beta: bool = False,
        on_retry: Callable[[int, int, float], Awaitable[None]] | None = None,
        _attempt: int = 0,
    ) -> list[GraphResponse]:
        """Run N sub-requests through ``$batch`` (auto-chunked at 20, chunks run concurrently).

        Sub-request failures are returned, never raised — one 403 on an owners lookup must
        not lose the other 19 results.

        Chunks are dispatched concurrently and bounded by the shared connection semaphore in
        :meth:`_send`. Running them serially made a tenant-wide owner fan-out (80,000 groups
        = 4,000 round-trips) take long enough that the only way to ship it was to cap the
        input, which is exactly the kind of silent incompleteness this product exists to
        avoid. Throttling is still handled correctly because every chunk goes through
        ``_send`` and retries its own 429 sub-requests.
        """
        chunks = [requests[i:i + BATCH_MAX] for i in range(0, len(requests), BATCH_MAX)]
        if not chunks:
            return []

        async def _run_chunk(chunk: Sequence[GraphRequest]) -> dict[str, GraphResponse]:
            payload = {
                "requests": [
                    {
                        "id": r.id,
                        "method": r.method,
                        "url": r.url,
                        **({"headers": r.headers} if r.headers else {}),
                    }
                    for r in chunk
                ]
            }
            resp = await self._send(
                "POST",
                f"{self.base(beta)}/$batch",
                json_body=payload,
                on_retry=on_retry,
            )
            self.stats.batches += 1
            self.stats.batch_subrequests += len(chunk)
            body = resp.json() if resp.content else {}
            responses = body.get("responses") if isinstance(body, dict) else None
            by_id: dict[str, GraphResponse] = {}
            for item in responses or []:
                if not isinstance(item, dict):
                    continue
                status = int(item.get("status") or 0)
                gr = GraphResponse(
                    id=str(item.get("id") or ""),
                    status=status,
                    body=item.get("body"),
                    headers={
                        str(k).lower(): str(v)
                        for k, v in (item.get("headers") or {}).items()
                    } if isinstance(item.get("headers"), dict) else {},
                    throttled=status == 429,
                )
                if gr.throttled:
                    self.stats.throttled += 1
                if gr.forbidden:
                    self.stats.forbidden += 1
                by_id[gr.id] = gr
            # Retry only throttled sub-requests. Graph places Retry-After in each batch
            # sub-response's `headers` object, not its error body. Keep the retry bounded
            # exactly like a normal request so one permanently throttled object cannot
            # recurse forever and hold a tenant-wide collection open.
            retryable = [r for r in chunk if by_id.get(r.id) and by_id[r.id].throttled]
            if retryable and _attempt < _MAX_RETRIES:
                delay = 0.0
                for r in retryable:
                    try:
                        delay = max(delay, float(by_id[r.id].headers.get("retry-after") or 0))
                    except (TypeError, ValueError):
                        pass
                attempt = _attempt + 1
                delay = min(
                    _MAX_BACKOFF_S,
                    (delay if delay > 0 else 2.0 ** attempt) + random.uniform(0, 0.5),
                )
                # A throttled sub-request is the same tenant-wide signal as a throttled GET.
                self._throttle(delay)
                self.stats.retries += 1
                if on_retry is not None:
                    try:
                        await on_retry(429, attempt, delay)
                    except Exception:  # noqa: BLE001 - telemetry must not break a request
                        pass
                await asyncio.sleep(delay)
                for gr in await self.batch(
                    retryable,
                    beta=beta,
                    on_retry=on_retry,
                    _attempt=attempt,
                ):
                    by_id[gr.id] = gr
            return by_id

        results = await asyncio.gather(*(_run_chunk(c) for c in chunks))
        merged: dict[str, GraphResponse] = {}
        for part in results:
            merged.update(part)
        return [merged.get(r.id, GraphResponse(id=r.id, status=0, body=None)) for r in requests]

    async def get_by_ids(
        self, ids: Sequence[str], types: Sequence[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Resolve directory objects by id in bulk (chunked at the Graph limit of 1000)."""
        wanted = [i for i in dict.fromkeys(ids) if i]
        resolved: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), GETBYIDS_MAX):
            chunk = wanted[start:start + GETBYIDS_MAX]
            payload: dict[str, Any] = {"ids": chunk}
            if types:
                payload["types"] = list(types)
            try:
                resp = await self._send("POST", f"{self.base()}/directoryObjects/getByIds", json_body=payload)
            except GraphPermissionError:
                raise
            except GraphError as exc:
                self.stats.errors.append(f"getByIds: {exc}")
                continue
            body = resp.json() if resp.content else {}
            for obj in (body.get("value") or []) if isinstance(body, dict) else []:
                if isinstance(obj, dict) and obj.get("id"):
                    resolved[str(obj["id"])] = obj
                    self.stats.items += 1
        return resolved

    async def delta(
        self,
        resource: str,
        token: str | None = None,
        *,
        select: Sequence[str] | None = None,
        max_items: int | None = None,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Run a delta query.

        Returns ``(items, delta_token, resynced)``. ``resynced`` is True when Graph rejected
        the supplied token (410 / ``resync``) and we transparently restarted from scratch —
        the caller must then treat ``items`` as a full replacement, not a patch."""
        resynced = False
        if token:
            url = token if token.startswith("http") else f"{self.base()}{resource}/delta?$deltatoken={token}"
        else:
            params: dict[str, Any] = {}
            if select:
                params["$select"] = ",".join(select)
            url = f"{self.base()}{resource}/delta" + (f"?{httpx.QueryParams(params)}" if params else "")

        items: list[dict[str, Any]] = []
        next_token = ""
        while url:
            try:
                resp = await self._send("GET", url)
            except GraphError as exc:
                if exc.status in (410, 400) and token and not resynced:
                    resynced = True
                    return await _restart_delta(self, resource, select, max_items)
                raise
            self.stats.pages += 1
            body = resp.json() if resp.content else {}
            page = body.get("value") if isinstance(body, dict) else None
            if isinstance(page, list):
                items.extend(page)
                self.stats.items += len(page)
            if max_items is not None and len(items) >= max_items:
                del items[max_items:]
                self.stats.truncated_calls += 1
                break
            delta_link = body.get("@odata.deltaLink") if isinstance(body, dict) else None
            if delta_link:
                next_token = str(delta_link)
                break
            url = (body.get("@odata.nextLink") or "") if isinstance(body, dict) else ""
        return items, next_token, resynced


def _rejects_top(exc: GraphError) -> bool:
    """True when Graph refused the request specifically because of ``$top``.

    Some Graph collections (``roleDefinitions``, ``authenticationStrengthPolicies``,
    ``authenticationContextClassReferences``) disallow paging options and answer with a 400
    that would otherwise lose the whole domain.
    """
    if exc.status != 400:
        return False
    msg = (exc.message or "").lower()
    return (
        "'top' is not allowed" in msg
        or "query option 'top'" in msg
        or "invalid/unsupported query request" in msg
    )


async def _restart_delta(
    client: GraphClient,
    resource: str,
    select: Sequence[str] | None,
    max_items: int | None,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Full re-collection after Graph invalidated a delta token."""
    items, token, _ = await client.delta(resource, None, select=select, max_items=max_items)
    return items, token, True
