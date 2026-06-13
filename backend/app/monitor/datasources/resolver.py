"""Datasource dispatcher: resolve a widget binding to a normalized TableResult.

Adds two cross-cutting concerns on top of the per-kind resolvers:

* **Concurrency** — a bounded semaphore caps simultaneous ``az`` CLI processes (each
  spawns a subprocess), so a dashboard with many Azure widgets can't fork-bomb the host.
* **Caching** — a short-TTL in-memory cache keyed by ``(kind, config, params, tenant)``
  means several widgets sharing one query hit Azure once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from app.core.azure_connections import resolve_connection

from .azure import resolve_azure_metrics, resolve_log_analytics, resolve_resource_graph
from .base import TableResult
from .internal import resolve_app_telemetry, resolve_static, resolve_workbook_ref
from .synthetic import resolve_tcp_ping, resolve_web_ping

# Cap concurrent Azure CLI executions (each az call spawns a process).
_AZ_SEMAPHORE = asyncio.Semaphore(4)
_AZ_KINDS = {"resource_graph", "log_analytics", "azure_metrics", "resource_health", "azure_cost"}

# Short-TTL result cache.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DEFAULT_TTL = 20.0  # seconds
_PING_TTL = 8.0
_MAX_CACHE = 256


def _cache_key(kind: str, cfg: dict[str, Any], params: dict[str, Any], tenant_id: str) -> str:
    basis = json.dumps(
        {"k": kind, "c": cfg, "p": params, "t": tenant_id}, sort_keys=True, default=str
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _ttl_for(kind: str) -> float:
    if kind in ("web_ping", "tcp_ping"):
        return _PING_TTL
    return _DEFAULT_TTL


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    expires, value = hit
    if time.time() > expires:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: dict[str, Any], ttl: float) -> None:
    if len(_CACHE) > _MAX_CACHE:
        # Drop the oldest ~quarter to bound memory.
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _MAX_CACHE // 4]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time() + ttl, value)


async def resolve_widget(
    data_source: dict[str, Any],
    *,
    tenant_id: str = "",
    params: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Resolve one widget ``dataSource`` to a serialized TableResult dict.

    ``params`` are dashboard-level parameter values cascaded into queries.
    """
    cfg = dict(data_source or {})
    kind = str(cfg.get("kind") or "none")
    params = params or {}

    if kind == "none":
        return TableResult(meta={"source": "none"}).to_dict()

    key = _cache_key(kind, cfg, params, tenant_id)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            out = dict(cached)
            out.setdefault("meta", {})
            out["meta"] = {**out.get("meta", {}), "cached": True}
            return out

    conn = resolve_connection(cfg.get("connection_id") or None) if kind in _AZ_KINDS else None

    started = time.perf_counter()
    try:
        result = await _dispatch(kind, cfg, conn, params, tenant_id)
    except Exception as exc:  # noqa: BLE001
        result = TableResult.from_error(f"{type(exc).__name__}: {exc}")
    payload = result.to_dict()
    payload.setdefault("meta", {})
    payload["meta"]["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    payload["meta"]["kind"] = kind

    if use_cache and not payload.get("error"):
        _cache_put(key, payload, _ttl_for(kind))
    return payload


async def _dispatch(
    kind: str, cfg: dict[str, Any], conn, params: dict[str, Any], tenant_id: str
) -> TableResult:
    if kind == "resource_graph":
        async with _AZ_SEMAPHORE:
            return await resolve_resource_graph(cfg, conn, params)
    if kind == "log_analytics":
        async with _AZ_SEMAPHORE:
            return await resolve_log_analytics(cfg, conn, params)
    if kind == "azure_metrics":
        async with _AZ_SEMAPHORE:
            return await resolve_azure_metrics(cfg, conn, params)
    if kind == "web_ping":
        return await resolve_web_ping(cfg, conn, params)
    if kind == "tcp_ping":
        return await resolve_tcp_ping(cfg, conn, params)
    if kind == "app_telemetry":
        return await resolve_app_telemetry(cfg, conn, params, tenant_id=tenant_id)
    if kind == "workbook_ref":
        return await resolve_workbook_ref(cfg, conn, params, tenant_id=tenant_id)
    if kind == "static":
        return await resolve_static(cfg, conn, params)
    return TableResult.from_error(f"Datasource '{kind}' is not implemented yet.")


def clear_cache() -> None:
    _CACHE.clear()
