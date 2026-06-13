"""Synthetic availability datasources: HTTPS (web) ping and TCP ping.

Both reuse the SSRF guard from :mod:`app.agent.builtins` (blocks loopback, private/
link-local ranges, and the cloud metadata endpoint), so a widget can never be pointed at
internal infrastructure. Results are single-row tables describing the latest probe;
historical rows come from the sampler (see :mod:`app.monitor.sampler`).
"""
from __future__ import annotations

import asyncio
import ssl
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from app.agent.builtins import _host_of_url, _resolve_safe_target

from .base import Column, TableResult

_WEB_TIMEOUT_S = 15.0
_TCP_TIMEOUT_S = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def probe_web(cfg: dict[str, Any]) -> dict[str, Any]:
    """Single HTTPS probe. Returns a flat record (also used by the sampler)."""
    url = str(cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided.", "at": _now_iso()}
    host, herr = _host_of_url(url)
    if herr:
        return {"ok": False, "error": herr, "at": _now_iso(), "url": url}
    _, serr = _resolve_safe_target(host)
    if serr:
        return {"ok": False, "error": serr, "at": _now_iso(), "url": url}
    method = str(cfg.get("method") or "GET").upper()
    if method not in ("GET", "HEAD"):
        method = "GET"
    expect = cfg.get("assert_status")
    assert_body = str(cfg.get("assert_body") or "").strip()
    started = time.perf_counter()
    rec: dict[str, Any] = {"url": url, "host": host, "at": _now_iso()}
    try:
        # Follow redirects MANUALLY, re-validating each hop against the SSRF guard so a
        # public URL can't 30x-redirect us to the metadata endpoint or a private range.
        async with httpx.AsyncClient(timeout=_WEB_TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.request(method, url, headers={"User-Agent": "AzureSupportAgent-Monitor/1.0"})
            hops = 0
            while resp.is_redirect and hops < 5:
                nxt = str(httpx.URL(resp.url).join(resp.headers.get("location", "")))
                nh, nerr = _host_of_url(nxt)
                if nerr is None:
                    _, nerr = _resolve_safe_target(nh)
                if nerr:
                    rec["ok"] = False
                    rec["status"] = resp.status_code
                    rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                    rec["error"] = f"Blocked redirect: {nerr}"
                    return rec
                resp = await client.request(method, nxt, headers={"User-Agent": "AzureSupportAgent-Monitor/1.0"})
                hops += 1
        latency = round((time.perf_counter() - started) * 1000, 1)
        rec["status"] = resp.status_code
        rec["latency_ms"] = latency
        ok = 200 <= resp.status_code < 400
        if expect not in (None, "", 0):
            try:
                ok = resp.status_code == int(expect)
            except (TypeError, ValueError):
                pass
        if ok and assert_body:
            ok = assert_body.lower() in resp.text.lower()
            rec["body_match"] = ok
        rec["ok"] = bool(ok)
        rec["tls_expiry"] = _tls_expiry(host) if urlparse(url).scheme == "https" else ""
    except httpx.HTTPError as exc:
        rec["ok"] = False
        rec["status"] = 0
        rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        rec["error"] = str(exc)[:200]
    return rec


def _tls_expiry(host: str) -> str:
    """Best-effort TLS certificate notAfter for a host (blocking; run rarely)."""
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(__import__("socket").socket(), server_hostname=host) as s:
            s.settimeout(5)
            s.connect((host, 443))
            cert = s.getpeercert()
        return cert.get("notAfter", "") if cert else ""
    except Exception:  # noqa: BLE001
        return ""


async def probe_tcp(cfg: dict[str, Any]) -> dict[str, Any]:
    """Single TCP connect probe. Returns a flat record (also used by the sampler)."""
    host = str(cfg.get("host") or "").strip()
    try:
        port = int(cfg.get("port"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "A numeric port is required.", "at": _now_iso(), "host": host}
    if not (1 <= port <= 65535):
        return {"ok": False, "error": "Port must be 1-65535.", "at": _now_iso(), "host": host}
    ips, serr = _resolve_safe_target(host)
    if serr:
        return {"ok": False, "error": serr, "at": _now_iso(), "host": host}
    target = ips[0]
    started = time.perf_counter()
    rec: dict[str, Any] = {"host": host, "port": port, "at": _now_iso()}
    try:
        fut = asyncio.open_connection(target, port)
        _, writer = await asyncio.wait_for(fut, timeout=_TCP_TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        rec["ok"] = True
        rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    except (asyncio.TimeoutError, OSError) as exc:
        rec["ok"] = False
        rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        rec["error"] = str(exc)[:200] or "Connection failed."
    return rec


def _history_table(kind: str, cfg: dict[str, Any], latest: dict[str, Any]) -> TableResult:
    """Build a time-series table from sampler history (the latest probe is already
    recorded by the resolver before this is called)."""
    from app.monitor import sampler

    target_k = sampler.target_key(kind, cfg)
    history = sampler.get_history(target_k)
    rows: list[list[Any]] = []
    for h in history:
        rows.append([h.get("at"), 1 if h.get("ok") else 0, h.get("latency_ms")])
    if not history:
        rows.append([latest.get("at"), 1 if latest.get("ok") else 0, latest.get("latency_ms")])
    columns = [Column("timestamp", "datetime"), Column("up", "number"), Column("latency_ms", "number")]
    up_count = sum(1 for r in rows if r[1] == 1)
    meta = {
        "source": kind,
        "latest": latest,
        "uptime_pct": round(100 * up_count / len(rows), 1) if rows else None,
        "sample_count": len(rows),
    }
    return TableResult(columns=columns, rows=rows[-500:], meta=meta)


async def resolve_web_ping(cfg: dict[str, Any], conn, params) -> TableResult:
    latest = await probe_web(cfg)
    _record("web_ping", cfg, latest)
    return _history_table("web_ping", cfg, latest)


async def resolve_tcp_ping(cfg: dict[str, Any], conn, params) -> TableResult:
    latest = await probe_tcp(cfg)
    _record("tcp_ping", cfg, latest)
    return _history_table("tcp_ping", cfg, latest)


def _record(kind: str, cfg: dict[str, Any], latest: dict[str, Any]) -> None:
    """Append an on-demand probe to history (best-effort) so charts fill immediately."""
    try:
        from app.monitor import sampler

        sampler.record_sample(kind, cfg, latest)
    except Exception:  # noqa: BLE001
        pass
