"""Disconnect-resilient turn runner.

Runs an agent turn as a background asyncio task that is NOT tied to the client's
SSE connection. The task owns its own DB session and persists progress at
checkpoints, so the work continues to completion even if the user navigates away
(which closes the SSE stream). Clients can (re)subscribe to a running turn's event
stream at any time and get a replay of everything emitted so far plus live updates.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.durable_jobs import DEFAULT_POLL_SECONDS, DurableJobStore

log = logging.getLogger("app.agent.turn_runner")

# Cap the per-turn replay buffer. A long turn (e.g. a deep investigation streaming
# thousands of token events) would otherwise accumulate every event in memory for the
# life of the run. We keep the most recent N for reconnect/replay; the full answer is
# always persisted to the DB, and a reconnecting client also refetches messages.
_MAX_REPLAY_EVENTS = 3000


class TurnRun:
    """A single in-flight (or just-finished) agent turn for one chat."""

    def __init__(
        self,
        chat_id: str,
        assistant_id: str,
        *,
        tenant_id: str = "default",
        durable_job: dict[str, Any] | None = None,
        store: DurableJobStore | None = None,
        lease_token: str | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.assistant_id = assistant_id
        self.tenant_id = tenant_id or "default"
        self.job_id = str((durable_job or {}).get("id") or "")
        self._store = store
        self._lease_token = lease_token
        # Recent SSE events, for replay to late/reconnecting subscribers (bounded).
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_REPLAY_EVENTS)
        self._subscribers: set[asyncio.Queue] = set()
        self.done = bool(durable_job and durable_job.get("status") != "running")
        # Set when a client explicitly stops the turn (POST /chats/{id}/stop). The
        # worker watches for the resulting CancelledError to persist partial output
        # and emit a final event instead of treating it as a crash.
        self.cancelled = False
        self.task: asyncio.Task | None = None
        # Lightweight live-activity metadata, updated from the emit funnel so the
        # monitor dashboard can show what each in-flight turn is doing right now
        # without subscribing to the full event stream.
        metadata = dict((durable_job or {}).get("metadata") or {})
        self.started_at: float = float(metadata.get("started_at") or time.time())
        self.last_at: float = self.started_at
        self.current_tool: str | None = metadata.get("current_tool")
        self.tool_count: int = int(metadata.get("tool_count") or 0)
        self.kind: str = str(metadata.get("kind") or "chat")
        self._write_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._writer_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

    def emit(self, event: str, data: dict[str, Any]) -> None:
        frame = {"event": event, "data": data}
        self._events.append(frame)
        self._track(event, data)
        if self._write_queue is not None:
            self._write_queue.put_nowait(frame)
        # Push to live subscribers, dropping any whose queue is unexpectedly full
        # (a stuck/dead consumer) so it can't pin memory or block the turn.
        dead: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def _track(self, event: str, data: dict[str, Any]) -> None:
        """Derive coarse live-activity state from emitted events (best-effort)."""
        self.last_at = time.time()
        if event in ("tool_start", "approval_required"):
            self.current_tool = data.get("tool_name") or data.get("name")
            self.tool_count += 1
        elif event == "tool_result":
            self.current_tool = None
        elif event in ("phase", "agents", "hypothesis", "hypothesis_status", "conclusion"):
            self.kind = "deep"

    def live_meta(self) -> dict[str, Any]:
        """A small snapshot of what this turn is currently doing."""
        return {
            "chat_id": self.chat_id,
            "kind": self.kind,
            "started_at": self.started_at,
            "last_at": self.last_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "current_tool": self.current_tool,
            "tool_count": self.tool_count,
        }

    def _durable_meta(self) -> dict[str, Any]:
        return {
            "assistant_id": self.assistant_id,
            "started_at": self.started_at,
            "last_at": self.last_at,
            "current_tool": self.current_tool,
            "tool_count": self.tool_count,
            "kind": self.kind,
        }

    def start_writer(self) -> None:
        if self._store is None or self._lease_token is None or not self.job_id:
            return
        self._write_queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._write_events())

    async def _write_events(self) -> None:
        assert self._write_queue is not None
        assert self._store is not None
        assert self._lease_token is not None
        while True:
            first = await self._write_queue.get()
            if first is None:
                self._write_queue.task_done()
                return
            batch = [first]
            while len(batch) < 100:
                try:
                    item = self._write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    self._write_queue.task_done()
                    try:
                        await self._persist_batch(batch)
                    finally:
                        for _ in batch:
                            self._write_queue.task_done()
                    return
                batch.append(item)
            try:
                await self._persist_batch(batch)
            finally:
                for _ in batch:
                    self._write_queue.task_done()

    async def _persist_batch(self, batch: list[dict[str, Any]]) -> None:
        assert self._store is not None
        assert self._lease_token is not None
        stored = await self._store.append_events(
            job_id=self.job_id,
            lease_token=self._lease_token,
            events=[(str(frame["event"]), dict(frame["data"])) for frame in batch],
            event_limit=_MAX_REPLAY_EVENTS,
            metadata=self._durable_meta(),
        )
        if not stored:
            raise RuntimeError("Turn event write lost its durable lease.")


    def finish(self) -> None:
        self.done = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)  # sentinel: end of stream
            except asyncio.QueueFull:
                self._subscribers.discard(q)
        # Drop subscriber refs; the buffer is kept only briefly (registry expiry).
        self._subscribers.clear()

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered events (replay) then live events until the turn finishes.

        Cancelling this iterator (client disconnect) only unsubscribes — it never
        stops the underlying work, which runs in `self.task`.
        """
        if self._store is not None and self.job_id:
            sent_seq = -1
            while True:
                events = await self._store.events_after(self.job_id, sent_seq)
                for event in events:
                    sent_seq = max(sent_seq, int(event["seq"]))
                    yield {"event": event["event"], "data": event["data"]}
                state = await self._store.load_current(
                    tenant_id=self.tenant_id,
                    feature=TurnRegistry.FEATURE,
                    key=self.chat_id,
                    include_events=False,
                )
                if state is None or state["status"] != "running":
                    for event in await self._store.events_after(self.job_id, sent_seq):
                        sent_seq = max(sent_seq, int(event["seq"]))
                        yield {"event": event["event"], "data": event["data"]}
                    return
                await asyncio.sleep(self._store.poll_seconds)
            return

        q: asyncio.Queue = asyncio.Queue()
        for frame in list(self._events):  # replay history (snapshot)
            q.put_nowait(frame)
        if self.done:
            q.put_nowait(None)
        else:
            self._subscribers.add(q)
        try:
            while True:
                frame = await q.get()
                if frame is None:
                    break
                yield frame
        finally:
            self._subscribers.discard(q)

    async def close_durable(self, *, status: str, error: str = "") -> None:
        if self._write_queue is not None and self._writer_task is not None:
            self._write_queue.put_nowait(None)
            try:
                await self._writer_task
            except Exception:  # noqa: BLE001 - a newer fence must still allow local teardown
                log.warning("Turn event writer stopped before finalization", exc_info=True)
        try:
            if self._store is not None and self._lease_token is not None and self.job_id:
                await self._store.finalize(
                    job_id=self.job_id,
                    lease_token=self._lease_token,
                    status=status,
                    result=None,
                    error=error,
                    retention_seconds=60,
                )
        finally:
            self.finish()

    async def wait(self) -> None:
        """Wait for local execution or a remote owner to reach a terminal state."""
        if self.task is not None:
            await self.task
            return
        if self._store is not None and self.job_id:
            await self._store.wait_for_terminal(self.job_id)


class TurnRegistry:
    """Process-wide registry of active turns, keyed by chat id."""

    FEATURE = "chat-turn"

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._store = DurableJobStore(
            session_factory=session_factory,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
        )
        self._runs: dict[str, TurnRun] = {}
        # Keep strong refs to fire-and-forget expiry tasks so they aren't GC'd
        # mid-flight (an asyncio pitfall: tasks held only weakly may be collected).
        self._expiry_tasks: set[asyncio.Task] = set()

    async def get(self, chat_id: str, *, tenant_id: str = "default") -> TurnRun | None:
        durable = await self._store.load_current(
            tenant_id=tenant_id, feature=self.FEATURE, key=chat_id, include_events=False
        )
        if durable is None:
            return None
        local = self._runs.get(chat_id)
        if local is not None and local.job_id == durable["id"]:
            local.done = durable["status"] != "running"
            return local
        metadata = dict(durable.get("metadata") or {})
        run = TurnRun(
            chat_id,
            str(metadata.get("assistant_id") or ""),
            tenant_id=tenant_id,
            durable_job=durable,
            store=self._store,
        )
        return run

    async def is_active(self, chat_id: str, *, tenant_id: str = "default") -> bool:
        jobs = await self._store.list_current(
            tenant_id=tenant_id,
            feature=self.FEATURE,
            key_prefix=chat_id,
            active_only=True,
        )
        return any(job["key"] == chat_id for job in jobs)

    async def cancel(self, chat_id: str, *, tenant_id: str = "default") -> bool:
        """Stop an in-flight turn for this chat by cancelling its background task.

        Returns True if a running turn was found and cancellation was requested. The
        worker handles the resulting CancelledError to persist whatever it produced
        so far. A no-op (returns False) if no turn is running.
        """
        requested = await self._store.request_cancel(
            tenant_id=tenant_id, feature=self.FEATURE, key=chat_id
        )
        run = self._runs.get(chat_id)
        if requested and run is not None and not run.done:
            run.cancelled = True
            if run.task is not None and not run.task.done():
                run.task.cancel()
        return requested

    async def active_chat_ids(self, *, tenant_id: str = "default") -> list[str]:
        """All chat ids with an in-flight (not-yet-finished) turn."""
        jobs = await self._store.list_current(
            tenant_id=tenant_id, feature=self.FEATURE, active_only=True
        )
        return [str(job["key"]) for job in jobs]

    async def live_snapshot(
        self, *, tenant_id: str = "default"
    ) -> dict[str, dict[str, Any]]:
        """Per-chat live-activity metadata for every in-flight turn."""
        jobs = await self._store.list_current(
            tenant_id=tenant_id, feature=self.FEATURE, active_only=True
        )
        now = time.time()
        snapshot: dict[str, dict[str, Any]] = {}
        for job in jobs:
            meta = dict(job.get("metadata") or {})
            started_at = float(meta.get("started_at") or now)
            snapshot[str(job["key"])] = {
                "chat_id": job["key"],
                "kind": meta.get("kind", "chat"),
                "started_at": started_at,
                "last_at": float(meta.get("last_at") or started_at),
                "elapsed_s": round(now - started_at, 1),
                "current_tool": meta.get("current_tool"),
                "tool_count": int(meta.get("tool_count") or 0),
            }
        return snapshot


    async def start(
        self,
        chat_id: str,
        assistant_id: str,
        worker,  # async callable(run: TurnRun) -> None
        *,
        tenant_id: str = "default",
    ) -> TurnRun:
        started_at = time.time()
        claim = await self._store.claim(
            tenant_id=tenant_id,
            feature=self.FEATURE,
            key=chat_id,
            metadata={
                "assistant_id": assistant_id,
                "started_at": started_at,
                "last_at": started_at,
                "current_tool": None,
                "tool_count": 0,
                "kind": "chat",
            },
        )
        durable = await self._store.load_current(
            tenant_id=tenant_id, feature=self.FEATURE, key=chat_id, include_events=False
        )
        if durable is None:
            raise RuntimeError("Claimed durable turn could not be loaded.")
        local = self._runs.get(chat_id)
        if not claim.acquired:
            if local is not None and local.job_id == durable["id"]:
                return local
            metadata = dict(durable.get("metadata") or {})
            return TurnRun(
                chat_id,
                str(metadata.get("assistant_id") or assistant_id),
                tenant_id=tenant_id,
                durable_job=durable,
                store=self._store,
            )

        run = TurnRun(
            chat_id,
            assistant_id,
            tenant_id=tenant_id,
            durable_job=durable,
            store=self._store,
            lease_token=claim.lease_token,
        )
        run.start_writer()
        self._runs[chat_id] = run

        async def _runner() -> None:
            status = "done"
            error = ""
            try:
                await worker(run)
            except asyncio.CancelledError:
                status = "cancelled"
                error = "Turn was cancelled."
                raise
            except Exception as exc:
                status = "error"
                error = str(exc)[:1500]
                # The chat id is request-derived and an exception can echo model/tool input.
                # Persist the bounded detail in the tenant-scoped durable job, but keep the
                # process log static so forged newlines cannot create synthetic log records.
                log.error("Chat turn failed outside its worker")
            finally:
                if run._watchdog_task is not None:  # noqa: SLF001 - paired lifecycle
                    run._watchdog_task.cancel()  # noqa: SLF001
                await asyncio.shield(run.close_durable(status=status, error=error))
                async def _expire() -> None:
                    await asyncio.sleep(60)
                    if self._runs.get(chat_id) is run:
                        del self._runs[chat_id]
                    await self._store.cleanup(feature=self.FEATURE)

                task = asyncio.create_task(_expire())
                self._expiry_tasks.add(task)
                task.add_done_callback(self._expiry_tasks.discard)

        run.task = asyncio.create_task(_runner())
        run._watchdog_task = asyncio.create_task(self._watch(run))  # noqa: SLF001
        return run

    async def _watch(self, run: TurnRun) -> None:
        assert run._lease_token is not None  # noqa: SLF001
        heartbeat_interval = max(self._store.poll_seconds, self._store.lease_seconds / 3)
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + heartbeat_interval
        try:
            while not run.done:
                await asyncio.sleep(self._store.poll_seconds)
                if loop.time() >= next_heartbeat:
                    owned, cancel_requested = await self._store.heartbeat(
                        job_id=run.job_id, lease_token=run._lease_token  # noqa: SLF001
                    )
                    next_heartbeat = loop.time() + heartbeat_interval
                else:
                    owned, cancel_requested = await self._store.lease_state(
                        job_id=run.job_id, lease_token=run._lease_token  # noqa: SLF001
                    )
                if not owned or cancel_requested:
                    run.cancelled = cancel_requested
                    if run.task is not None and not run.task.done():
                        run.task.cancel()
                    return
        except asyncio.CancelledError:
            return


# Singleton registry.
registry = TurnRegistry()
