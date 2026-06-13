"""Disconnect-resilient turn runner.

Runs an agent turn as a background asyncio task that is NOT tied to the client's
SSE connection. The task owns its own DB session and persists progress at
checkpoints, so the work continues to completion even if the user navigates away
(which closes the SSE stream). Clients can (re)subscribe to a running turn's event
stream at any time and get a replay of everything emitted so far plus live updates.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

# Cap the per-turn replay buffer. A long turn (e.g. a deep investigation streaming
# thousands of token events) would otherwise accumulate every event in memory for the
# life of the run. We keep the most recent N for reconnect/replay; the full answer is
# always persisted to the DB, and a reconnecting client also refetches messages.
_MAX_REPLAY_EVENTS = 3000


class TurnRun:
    """A single in-flight (or just-finished) agent turn for one chat."""

    def __init__(self, chat_id: str, assistant_id: str) -> None:
        self.chat_id = chat_id
        self.assistant_id = assistant_id
        # Recent SSE events, for replay to late/reconnecting subscribers (bounded).
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_REPLAY_EVENTS)
        self._subscribers: set[asyncio.Queue] = set()
        self.done = False
        # Set when a client explicitly stops the turn (POST /chats/{id}/stop). The
        # worker watches for the resulting CancelledError to persist partial output
        # and emit a final event instead of treating it as a crash.
        self.cancelled = False
        self.task: asyncio.Task | None = None
        # Lightweight live-activity metadata, updated from the emit funnel so the
        # monitor dashboard can show what each in-flight turn is doing right now
        # without subscribing to the full event stream.
        self.started_at: float = time.time()
        self.last_at: float = self.started_at
        self.current_tool: str | None = None
        self.tool_count: int = 0
        self.kind: str = "chat"  # promoted to "deep" once an investigation phase emits

    def emit(self, event: str, data: dict[str, Any]) -> None:
        frame = {"event": event, "data": data}
        self._events.append(frame)
        self._track(event, data)
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


class TurnRegistry:
    """Process-wide registry of active turns, keyed by chat id."""

    def __init__(self) -> None:
        self._runs: dict[str, TurnRun] = {}
        # Keep strong refs to fire-and-forget expiry tasks so they aren't GC'd
        # mid-flight (an asyncio pitfall: tasks held only weakly may be collected).
        self._expiry_tasks: set[asyncio.Task] = set()

    def get(self, chat_id: str) -> TurnRun | None:
        return self._runs.get(chat_id)

    def is_active(self, chat_id: str) -> bool:
        run = self._runs.get(chat_id)
        return run is not None and not run.done

    def cancel(self, chat_id: str) -> bool:
        """Stop an in-flight turn for this chat by cancelling its background task.

        Returns True if a running turn was found and cancellation was requested. The
        worker handles the resulting CancelledError to persist whatever it produced
        so far. A no-op (returns False) if no turn is running.
        """
        run = self._runs.get(chat_id)
        if run is None or run.done:
            return False
        run.cancelled = True
        if run.task is not None and not run.task.done():
            run.task.cancel()
        return True

    def active_chat_ids(self) -> list[str]:
        """All chat ids with an in-flight (not-yet-finished) turn."""
        return [cid for cid, run in self._runs.items() if not run.done]

    def live_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-chat live-activity metadata for every in-flight turn."""
        return {
            cid: run.live_meta() for cid, run in self._runs.items() if not run.done
        }


    def start(
        self,
        chat_id: str,
        assistant_id: str,
        worker,  # async callable(run: TurnRun) -> None
    ) -> TurnRun:
        run = TurnRun(chat_id, assistant_id)
        self._runs[chat_id] = run

        async def _runner() -> None:
            try:
                await worker(run)
            finally:
                run.finish()
                # Keep finished runs briefly so a just-returning client can still
                # replay the final events, then drop to avoid leaking memory.
                async def _expire() -> None:
                    await asyncio.sleep(60)
                    if self._runs.get(chat_id) is run:
                        del self._runs[chat_id]

                task = asyncio.create_task(_expire())
                self._expiry_tasks.add(task)
                task.add_done_callback(self._expiry_tasks.discard)

        run.task = asyncio.create_task(_runner())
        return run


# Singleton registry.
registry = TurnRegistry()
