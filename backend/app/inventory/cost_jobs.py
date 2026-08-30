"""Durable detached jobs for Inventory Cost refreshes."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DurableJobContext, DurableJobExecutor, JobOutcome, as_utc, utcnow
from app.inventory import cost

logger = logging.getLogger("app.inventory.cost_jobs")

_FEATURE = "inventory.cost"
_TERMINAL = {"succeeded", "partial", "failed"}
_RETAIN_SECONDS = 3600
_INTERRUPTED = "Cost refresh was interrupted before completion."


def _scope_key(scope: str) -> str:
    return ",".join(sorted({token.strip() for token in (scope or "").split(",") if token.strip()}))


def _job_key(connection_id: str, scope: str) -> str:
    raw = f"{connection_id or ''}\n{_scope_key(scope)}".encode()
    return hashlib.sha256(raw).hexdigest()


class CostJobManager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = 0.25,
    ) -> None:
        self._executor = DurableJobExecutor(
            _FEATURE,
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            retention_seconds=_RETAIN_SECONDS,
            event_limit=100,
        )

    @staticmethod
    def key(tenant_id: str, connection_id: str, scope: str) -> tuple[str, str, str]:
        return (tenant_id or "", connection_id or "", _scope_key(scope))

    @staticmethod
    def _from_durable(durable: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(durable.get("metadata") or {})
        events = [
            dict(event.get("data") or {})
            for event in durable.get("events") or []
            if event.get("event") == "progress"
        ]
        status = str(metadata.get("status") or "queued")
        if durable["status"] == "done":
            status = str(metadata.get("status") or "succeeded")
        elif durable["status"] in {"error", "cancelled"}:
            status = "failed"
        started = durable.get("started_at")
        ended = durable.get("finished_at")
        try:
            start_dt = datetime.fromisoformat(started) if started else None
            end_dt = datetime.fromisoformat(ended) if ended else datetime.now(timezone.utc)
            if start_dt and start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            elapsed_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000)) if start_dt else 0
        except ValueError:
            elapsed_ms = 0
        return {
            "id": durable["id"],
            "connection_id": metadata.get("connection_id", ""),
            "scope": metadata.get("scope", ""),
            "force": bool(metadata.get("force")),
            "status": status,
            "message": metadata.get("message", "Cost refresh queued."),
            "subscriptions_total": int(metadata.get("subscriptions_total") or 0),
            "subscriptions_visible": int(metadata.get("subscriptions_visible") or 0),
            "subscriptions_omitted": int(metadata.get("subscriptions_omitted") or 0),
            "subscriptions_done": int(metadata.get("subscriptions_done") or 0),
            "subscriptions_succeeded": int(metadata.get("subscriptions_succeeded") or 0),
            "subscriptions_failed": int(metadata.get("subscriptions_failed") or 0),
            "active_subscriptions": list((metadata.get("active_subscriptions") or {}).values()),
            "recent_events": events[-12:],
            "result": durable.get("result") if status in _TERMINAL else None,
            "error": durable.get("error") or metadata.get("error", ""),
            "created_at": durable["started_at"],
            "started_at": durable["started_at"],
            "ended_at": durable.get("finished_at") or "",
            "elapsed_ms": elapsed_ms,
        }

    async def _load(self, tenant_id: str, key: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        if durable is None:
            return None
        if durable["status"] == "running" and durable.get("lease_expires_at"):
            expires = as_utc(datetime.fromisoformat(durable["lease_expires_at"]))
            if expires is not None and expires <= utcnow():
                await self._executor.store.interrupt_expired(
                    tenant_id=tenant_id,
                    feature=_FEATURE,
                    key=key,
                    error=_INTERRUPTED,
                    retention_seconds=_RETAIN_SECONDS,
                )
                durable = await self._executor.store.load_current(
                    tenant_id=tenant_id, feature=_FEATURE, key=key
                )
                if durable is None:
                    return None
        return self._from_durable(durable)

    async def start(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        scope: str,
        force: bool,
        connection: dict[str, Any] | None,
        subscriptions: list[str],
    ) -> dict[str, Any]:
        normalized_scope = _scope_key(scope)
        key = _job_key(connection_id, normalized_scope)
        initial = {
            "connection_id": connection_id,
            "scope": normalized_scope,
            "force": force,
            "status": "queued",
            "message": "Cost refresh queued.",
            "subscriptions_total": 0,
            "subscriptions_visible": 0,
            "subscriptions_omitted": 0,
            "subscriptions_done": 0,
            "subscriptions_succeeded": 0,
            "subscriptions_failed": 0,
            "active_subscriptions": {},
            "error": "",
        }

        async def _run(context: DurableJobContext) -> JobOutcome:
            context.metadata.update(initial)
            context.metadata.update(
                {"status": "running", "message": "Starting Azure Cost Management queries."}
            )

            async def progress(raw_event: dict[str, Any]) -> None:
                event = dict(raw_event)
                event["at"] = datetime.now(timezone.utc).isoformat()
                event_type = str(event.get("type") or "progress")
                subscription_id = str(event.get("subscription_id") or "")
                state = context.metadata
                active = dict(state.get("active_subscriptions") or {})
                if event_type == "started":
                    state["subscriptions_total"] = int(event.get("subscriptions_total") or 0)
                    state["subscriptions_visible"] = int(event.get("subscriptions_visible") or 0)
                    state["subscriptions_omitted"] = int(event.get("subscriptions_omitted") or 0)
                elif event_type == "subscription_started" and subscription_id:
                    active[subscription_id] = {
                        "subscription_id": subscription_id,
                        "index": event.get("index"),
                        "started_at": event["at"],
                        "attempt": 1,
                    }
                elif event_type == "subscription_retry" and subscription_id:
                    item = active.setdefault(
                        subscription_id,
                        {"subscription_id": subscription_id, "started_at": event["at"]},
                    )
                    item["attempt"] = event.get("attempt")
                    item["retry_delay_seconds"] = event.get("delay_seconds")
                elif event_type in {"subscription_done", "subscription_error"}:
                    active.pop(subscription_id, None)
                    state["subscriptions_done"] = max(
                        int(state.get("subscriptions_done") or 0),
                        int(event.get("subscriptions_done") or 0),
                    )
                    counter = (
                        "subscriptions_succeeded"
                        if event_type == "subscription_done"
                        else "subscriptions_failed"
                    )
                    state[counter] = int(state.get(counter) or 0) + 1
                state["active_subscriptions"] = active
                state["message"] = str(event.get("message") or state["message"])
                await context.emit("progress", event, metadata=state)

            await progress(
                {
                    "type": "queued",
                    "message": "Refresh accepted by the server; it will continue if you navigate away.",
                }
            )
            try:
                result = await cost.get_cost(
                    connection,
                    subscriptions,
                    tenant_id,
                    connection_id,
                    force=force,
                    scope=normalized_scope,
                    progress=progress,
                )
                errors = result.get("errors") or []
                status = "partial" if errors else "succeeded"
                error = "; ".join(str(item) for item in errors[:3])
                message = (
                    f"Cost refresh completed with {len(errors)} subscription error(s)."
                    if errors
                    else "Cost refresh complete. Shared cost data is now up to date."
                )
                context.metadata.update(
                    {
                        "status": status,
                        "message": message,
                        "error": error,
                        "active_subscriptions": {},
                    }
                )
                await context.emit(
                    "progress",
                    {"type": status, "at": datetime.now(timezone.utc).isoformat(), "message": message},
                    metadata=context.metadata,
                )
                return JobOutcome(result=result, error=error)
            except Exception as exc:  # noqa: BLE001 - record bounded status
                logger.exception("Inventory cost refresh job failed")
                message = str(exc)[:1000] or "Unexpected cost refresh failure."
                context.metadata.update(
                    {"status": "failed", "message": message, "error": message, "active_subscriptions": {}}
                )
                await context.emit(
                    "progress",
                    {"type": "failed", "at": datetime.now(timezone.utc).isoformat(), "message": message},
                    metadata=context.metadata,
                )
                return JobOutcome(status="error", error=message)

        claim = await self._executor.start(
            tenant_id=tenant_id, key=key, metadata=initial, runner=_run
        )
        durable = await self._executor.store.load_current(
            tenant_id=tenant_id, feature=_FEATURE, key=key
        )
        return self._from_durable(durable or claim.job)

    async def latest(
        self, tenant_id: str, connection_id: str, scope: str
    ) -> dict[str, Any] | None:
        return await self._load(tenant_id, _job_key(connection_id, scope))

    async def get(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        durable = await self._executor.store.load_by_id(
            tenant_id=tenant_id, feature=_FEATURE, job_id=job_id
        )
        if durable is None:
            return None
        return await self._load(tenant_id, durable["key"])


manager = CostJobManager()
