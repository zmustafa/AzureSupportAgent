"""Background availability sampler: probes web/TCP-ping widgets on a cadence and keeps
a rolling history so availability/latency widgets can chart uptime over time.

History is stored in a single JSON file (``.data/monitor_ping_history.json``) — bounded
per target — to avoid a DB migration. The sampler walks every saved dashboard, finds
``web_ping`` / ``tcp_ping`` widgets, and probes each due target. On-demand widget runs
ALSO append to this history (see datasources.synthetic), so charts fill in immediately.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("app.monitor.sampler")

# Backend dir is parents[2] of this file (app/monitor/sampler.py -> backend/).
_PATH = Path(__file__).resolve().parents[2] / ".data" / "monitor_ping_history.json"

TICK_SECONDS = 60
DEFAULT_SAMPLE_EVERY_S = 300  # probe each target at most every 5 min by default
MAX_SAMPLES_PER_TARGET = 500
_lock = asyncio.Lock()


def target_key(kind: str, cfg: dict[str, Any]) -> str:
    """Stable id for a ping target so history accumulates across runs/dashboards."""
    if kind == "web_ping":
        basis = f"web|{(cfg.get('url') or '').strip().lower()}"
    else:
        basis = f"tcp|{(cfg.get('host') or '').strip().lower()}|{cfg.get('port')}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"targets": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
    tmp.replace(_PATH)


def get_history(target_key_: str) -> list[dict[str, Any]]:
    data = _read()
    t = data.get("targets", {}).get(target_key_)
    if not t:
        return []
    return list(t.get("samples", []))[-MAX_SAMPLES_PER_TARGET:]


def record_sample(kind: str, cfg: dict[str, Any], sample: dict[str, Any]) -> None:
    """Append one probe result to a target's history (bounded)."""
    key = target_key(kind, cfg)
    data = _read()
    targets = data.setdefault("targets", {})
    entry = targets.setdefault(key, {"kind": kind, "label": cfg.get("url") or cfg.get("host") or "", "samples": []})
    entry["label"] = cfg.get("url") or (f"{cfg.get('host')}:{cfg.get('port')}" if cfg.get("host") else entry.get("label", ""))
    entry["last_sampled"] = time.time()
    entry["samples"].append({
        "at": sample.get("at") or datetime.now(timezone.utc).isoformat(),
        "ok": bool(sample.get("ok")),
        "latency_ms": sample.get("latency_ms"),
        "status": sample.get("status"),
    })
    entry["samples"] = entry["samples"][-MAX_SAMPLES_PER_TARGET:]
    _write(data)


def _due(key: str, every_s: int) -> bool:
    data = _read()
    entry = data.get("targets", {}).get(key)
    if not entry:
        return True
    last = entry.get("last_sampled", 0)
    return (time.time() - float(last or 0)) >= every_s


def _collect_ping_widgets() -> list[tuple[str, dict[str, Any]]]:
    """Every (kind, cfg) ping target across all saved dashboards (deduped by target)."""
    from app.monitor import registry as dash_registry

    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for dash in dash_registry.list_dashboards(None):
        for w in dash.get("widgets", []):
            ds = w.get("dataSource", {}) or {}
            kind = ds.get("kind")
            if kind in ("web_ping", "tcp_ping"):
                out[target_key(kind, ds)] = (kind, ds)
    return list(out.values())


class PingSampler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("Monitor ping sampler started (tick=%ss)", TICK_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.warning("Ping sampler tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        from app.monitor.datasources.synthetic import probe_tcp, probe_web

        targets = _collect_ping_widgets()
        for kind, cfg in targets:
            every = DEFAULT_SAMPLE_EVERY_S
            try:
                every = max(60, min(86_400, int(cfg.get("sample_every_s", DEFAULT_SAMPLE_EVERY_S))))
            except (TypeError, ValueError):
                pass
            if not _due(target_key(kind, cfg), every):
                continue
            try:
                sample = await (probe_web(cfg) if kind == "web_ping" else probe_tcp(cfg))
                async with _lock:
                    record_sample(kind, cfg, sample)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Probe failed for %s: %s", cfg, exc)


sampler = PingSampler()
