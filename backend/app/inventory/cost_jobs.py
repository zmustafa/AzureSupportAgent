"""Detached background jobs for Inventory Cost refreshes.

A Cost Management refresh belongs to the server, not to the browser request that started it.
The UI starts a job and polls its snapshot, so changing tabs/routes or closing the Inventory
component cannot cancel Azure queries.  Terminal results still live in the existing permanent
cost cache; these in-memory job snapshots are short-lived progress telemetry only.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.inventory import cost

logger = logging.getLogger("app.inventory.cost_jobs")

_TERMINAL = {"succeeded", "partial", "failed"}
_RETAIN_SECONDS = 3600


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""


def _scope_key(scope: str) -> str:
    return ",".join(sorted({token.strip() for token in (scope or "").split(",") if token.strip()}))


@dataclass
class CostJob:
    id: str
    tenant_id: str
    connection_id: str
    scope: str
    force: bool
    status: str = "queued"
    message: str = "Cost refresh queued."
    subscriptions_total: int = 0
    subscriptions_visible: int = 0
    subscriptions_omitted: int = 0
    subscriptions_done: int = 0
    subscriptions_succeeded: int = 0
    subscriptions_failed: int = 0
    active_subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        elapsed_to = self.ended_at or time.time()
        started = self.started_at or self.created_at
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "scope": self.scope,
            "force": self.force,
            "status": self.status,
            "message": self.message,
            "subscriptions_total": self.subscriptions_total,
            "subscriptions_visible": self.subscriptions_visible,
            "subscriptions_omitted": self.subscriptions_omitted,
            "subscriptions_done": self.subscriptions_done,
            "subscriptions_succeeded": self.subscriptions_succeeded,
            "subscriptions_failed": self.subscriptions_failed,
            "active_subscriptions": list(self.active_subscriptions.values()),
            "recent_events": self.events[-12:],
            "result": self.result if self.status in _TERMINAL else None,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "elapsed_ms": max(0, int((elapsed_to - started) * 1000)),
        }


class CostJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, CostJob] = {}
        self._latest: dict[tuple[str, str, str], str] = {}
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def key(tenant_id: str, connection_id: str, scope: str) -> tuple[str, str, str]:
        return (tenant_id or "", connection_id or "", _scope_key(scope))

    def start(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        scope: str,
        force: bool,
        connection: dict[str, Any] | None,
        subscriptions: list[str],
    ) -> dict[str, Any]:
        self._prune()
        key = self.key(tenant_id, connection_id, scope)
        existing_id = self._latest.get(key)
        existing = self._jobs.get(existing_id or "")
        if existing is not None and existing.status not in _TERMINAL:
            return existing.public()

        job = CostJob(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            connection_id=connection_id,
            scope=_scope_key(scope),
            force=force,
        )
        job.events.append(
            {
                "type": "queued",
                "at": _iso(time.time()),
                "message": "Refresh accepted by the server; it will continue if you navigate away.",
            }
        )
        self._jobs[job.id] = job
        self._latest[key] = job.id
        task = asyncio.create_task(self._run(job, connection, subscriptions))
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.public()

    def latest(self, tenant_id: str, connection_id: str, scope: str) -> dict[str, Any] | None:
        self._prune()
        job_id = self._latest.get(self.key(tenant_id, connection_id, scope))
        job = self._jobs.get(job_id or "")
        return job.public() if job is not None else None

    def get(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            return None
        return job.public()

    async def _run(
        self,
        job: CostJob,
        connection: dict[str, Any] | None,
        subscriptions: list[str],
    ) -> None:
        job.status = "running"
        job.started_at = time.time()
        job.message = "Starting Azure Cost Management queries."

        async def progress(event: dict[str, Any]) -> None:
            self._apply_event(job, event)

        try:
            result = await cost.get_cost(
                connection,
                subscriptions,
                job.tenant_id,
                job.connection_id,
                force=job.force,
                scope=job.scope,
                progress=progress,
            )
            job.result = result
            errors = result.get("errors") or []
            job.status = "partial" if errors else "succeeded"
            job.error = "; ".join(str(error) for error in errors[:3])
            job.message = (
                f"Cost refresh completed with {len(errors)} subscription error(s)."
                if errors
                else "Cost refresh complete. Shared cost data is now up to date."
            )
        except asyncio.CancelledError:
            # Application shutdown is the only owner-driven cancellation. A browser disconnect
            # never reaches this task because it is detached from the request lifecycle.
            job.status = "failed"
            job.error = "Cost refresh was interrupted by application shutdown."
            job.message = job.error
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inventory cost refresh job failed")
            job.status = "failed"
            job.error = str(exc)[:1000] or "Unexpected cost refresh failure."
            job.message = job.error
        finally:
            job.ended_at = time.time()
            job.active_subscriptions.clear()
            job.events.append(
                {
                    "type": job.status,
                    "at": _iso(job.ended_at),
                    "message": job.message,
                }
            )

    def _apply_event(self, job: CostJob, event: dict[str, Any]) -> None:
        event = dict(event)
        event["at"] = _iso(time.time())
        event_type = str(event.get("type") or "progress")
        subscription_id = str(event.get("subscription_id") or "")
        if event_type == "started":
            job.subscriptions_total = int(event.get("subscriptions_total") or 0)
            job.subscriptions_visible = int(event.get("subscriptions_visible") or 0)
            job.subscriptions_omitted = int(event.get("subscriptions_omitted") or 0)
        elif event_type == "subscription_started" and subscription_id:
            job.active_subscriptions[subscription_id] = {
                "subscription_id": subscription_id,
                "index": event.get("index"),
                "started_at": event["at"],
                "attempt": 1,
            }
        elif event_type == "subscription_retry" and subscription_id:
            active = job.active_subscriptions.setdefault(
                subscription_id,
                {"subscription_id": subscription_id, "started_at": event["at"]},
            )
            active["attempt"] = event.get("attempt")
            active["retry_delay_seconds"] = event.get("delay_seconds")
        elif event_type in {"subscription_done", "subscription_error"}:
            job.active_subscriptions.pop(subscription_id, None)
            job.subscriptions_done = max(
                job.subscriptions_done, int(event.get("subscriptions_done") or 0)
            )
            if event_type == "subscription_done":
                job.subscriptions_succeeded += 1
            else:
                job.subscriptions_failed += 1
        job.message = str(event.get("message") or job.message)
        job.events.append(event)
        if len(job.events) > 100:
            del job.events[:-100]

    def _prune(self) -> None:
        now = time.time()
        stale_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL and job.ended_at and now - job.ended_at > _RETAIN_SECONDS
        ]
        for job_id in stale_ids:
            job = self._jobs.pop(job_id, None)
            if job is None:
                continue
            key = self.key(job.tenant_id, job.connection_id, job.scope)
            if self._latest.get(key) == job_id:
                self._latest.pop(key, None)


manager = CostJobManager()
