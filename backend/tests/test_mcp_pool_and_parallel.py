"""Edge-case tests for the Tier-3 perf work:

1. MCP session pooling (app.mcp.client.MCPClient):
   - a single long-lived session is reused across many call_tool()s (no per-call spawn);
   - concurrent call_tool()s overlap on one session (multiplexed by request id);
   - the pool self-heals: after the owner task dies, the next call restarts it;
   - a failed session spawn falls back to a one-shot session (never hangs);
   - MCP_POOL=0 disables pooling (per-call sessions).

2. Parallel in-turn tool execution:
   - DeepInvestigator._tool_loop runs a turn's tool calls concurrently while emitting
     tool_result events + tool messages in the ORIGINAL order (tool_call_id alignment);
   - Orchestrator.run executes read tools in parallel but keeps write tools GATED
     (approval_required, never executed) and preserves ordering.

These pin the behavior that must stay reliable for production.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.agent.provider import StreamEvent, ToolCallRequest, ToolSpec
from app.mcp.client import MCPClient


# --------------------------------------------------------------------------- fakes
class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, content: list[str], is_error: bool = False) -> None:
        self.content = [_FakeBlock(c) for c in content]
        self.isError = is_error


class _FakeSession:
    """A stand-in MCP ClientSession that records concurrency and per-call names."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.calls.append(name)
            return _FakeResult([f"ok:{name}"])
        finally:
            self.active -= 1


def _session_patch(sessions: list[_FakeSession], entries: list[int]):
    """Build a fake MCPClient._session that hands out `sessions[i]` on the i-th enter
    and records every enter in `entries` (so we can count spawns)."""

    @contextlib.asynccontextmanager
    async def fake_session(self):  # noqa: ANN001 - matches method signature
        idx = len(entries)
        entries.append(1)
        yield sessions[min(idx, len(sessions) - 1)]

    return fake_session


def _new_client() -> MCPClient:
    return MCPClient(command="dummy", args=[], read_only=True)


# --------------------------------------------------------------------------- pooling
@pytest.mark.asyncio
async def test_pool_reuses_single_session(monkeypatch):
    sess = _FakeSession()
    entries: list[int] = []
    monkeypatch.setattr(MCPClient, "_session", _session_patch([sess], entries))
    c = _new_client()
    try:
        for i in range(5):
            r = await c.call_tool("arm", {"i": i})
            assert r["isError"] is False
            assert r["content"] == ["ok:arm"]
        # Exactly ONE session spawn for all five calls.
        assert len(entries) == 1
        assert sess.calls == ["arm"] * 5
    finally:
        await c.aclose()
    assert c._pool_task is None


@pytest.mark.asyncio
async def test_pool_concurrent_calls_overlap_on_one_session(monkeypatch):
    sess = _FakeSession(delay=0.05)
    entries: list[int] = []
    monkeypatch.setattr(MCPClient, "_session", _session_patch([sess], entries))
    c = _new_client()
    try:
        results = await asyncio.gather(*(c.call_tool("t", {"i": i}) for i in range(6)))
        assert all(r["isError"] is False for r in results)
        assert len(entries) == 1           # one spawn
        assert sess.max_active > 1         # calls genuinely overlapped
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_pool_restarts_after_owner_dies(monkeypatch):
    s1, s2 = _FakeSession(), _FakeSession()
    entries: list[int] = []
    monkeypatch.setattr(MCPClient, "_session", _session_patch([s1, s2], entries))
    c = _new_client()
    try:
        await c.call_tool("a", {})
        assert len(entries) == 1
        # Simulate the owner task dying (e.g. npx crash).
        task = c._pool_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        # Next call must lazily restart a fresh owner -> a new session spawn.
        r = await c.call_tool("b", {})
        assert r["content"] == ["ok:b"]
        assert len(entries) == 2
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_pool_start_failure_falls_back_to_oneshot(monkeypatch):
    good = _FakeSession()
    entries: list[int] = []

    @contextlib.asynccontextmanager
    async def flaky_session(self):  # noqa: ANN001
        n = len(entries)
        entries.append(1)
        if n == 0:
            # First enter = the pool owner trying to spawn: fail it.
            raise RuntimeError("npx spawn failed")
        yield good

    monkeypatch.setattr(MCPClient, "_session", flaky_session)
    c = _new_client()
    try:
        # Pool start fails, so call_tool falls back to a one-shot session and still works.
        r = await c.call_tool("a", {})
        assert r["isError"] is False
        assert r["content"] == ["ok:a"]
        assert len(entries) >= 2  # failed owner spawn + one-shot fallback
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_pool_disabled_via_env_uses_oneshot(monkeypatch):
    monkeypatch.setenv("MCP_POOL", "0")
    sess = _FakeSession()
    entries: list[int] = []
    monkeypatch.setattr(MCPClient, "_session", _session_patch([sess], entries))
    c = _new_client()  # reads MCP_POOL at construction
    try:
        for _ in range(3):
            await c.call_tool("a", {})
        assert len(entries) == 3          # a fresh session per call
        assert c._pool_task is None
    finally:
        c.close()


# ------------------------------------------------------------- parallel tool loop
class _FakeProvider:
    """Yields one tool-calls round, then a final-answer round."""

    def __init__(self, tool_calls: list[ToolCallRequest]) -> None:
        self._tool_calls = tool_calls
        self._n = 0

    async def stream(self, messages, tools=None, max_tokens=None):
        self._n += 1
        if self._n == 1:
            yield StreamEvent(type="tool_calls", tool_calls=list(self._tool_calls))
            yield StreamEvent(type="done", prompt_tokens=1, completion_tokens=1)
        else:
            yield StreamEvent(type="token", text="final")
            yield StreamEvent(type="done", prompt_tokens=1, completion_tokens=1)


@pytest.mark.asyncio
async def test_deep_tool_loop_runs_parallel_but_emits_in_order(monkeypatch):
    from app.core.config import get_settings
    from app.agent.deep_investigation import DeepInvestigator

    di = DeepInvestigator(get_settings(), focus=[])

    calls = [ToolCallRequest(id=f"c{i}", name=f"t{i}", arguments={}) for i in range(4)]
    di._provider = _FakeProvider(calls)
    di._fast_provider = di._provider
    di._tool_specs = [ToolSpec(name="t", description="", parameters={})]

    active = 0
    max_active = 0

    async def fake_call_tool(name: str, arguments: dict):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.03)
            return {"isError": False, "content": [f"r:{name}"]}
        finally:
            active -= 1

    di._call_tool = fake_call_tool  # instance attr shadows the bound method

    result: dict = {}
    events = []
    async for ev in di._tool_loop("sys", "user", 3, result):
        events.append(ev)

    tool_results = [e for e in events if e.type == "tool_result"]
    # Order preserved and each result mapped to the right tool.
    assert [e.data["tool_name"] for e in tool_results] == ["t0", "t1", "t2", "t3"]
    for e in tool_results:
        assert e.data["result"]["content"] == [f"r:{e.data['tool_name']}"]
    # The four calls genuinely ran concurrently.
    assert max_active > 1
    assert result["text"] == "final"

    di.close()


# --------------------------------------------------------- orchestrator gating
class _MixedProvider:
    """One round with two read calls + one write call, then a final answer."""

    def __init__(self) -> None:
        self._n = 0

    async def stream(self, messages, tools=None, max_tokens=None):
        self._n += 1
        if self._n == 1:
            yield StreamEvent(
                type="tool_calls",
                tool_calls=[
                    ToolCallRequest(id="r0", name="arm", arguments={"command": "list"}),
                    ToolCallRequest(id="r1", name="monitor", arguments={"command": "metrics list"}),
                    ToolCallRequest(id="w0", name="storage", arguments={"command": "blob delete"}),
                ],
            )
            yield StreamEvent(type="done", prompt_tokens=1, completion_tokens=1)
        else:
            yield StreamEvent(type="token", text="done")
            yield StreamEvent(type="done", prompt_tokens=1, completion_tokens=1)


@pytest.mark.asyncio
async def test_orchestrator_parallel_reads_but_writes_stay_gated(monkeypatch):
    from app.core.config import get_settings
    from app.agent.orchestrator import Orchestrator

    orch = Orchestrator(get_settings(), write_policy_override="gated")
    orch._provider = _MixedProvider()

    async def fake_load_tools():
        return [], {}

    orch._load_tools = fake_load_tools

    active = 0
    max_active = 0
    executed: list[str] = []

    async def fake_call_tool(name: str, arguments: dict):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.03)
            executed.append(name)
            return {"isError": False, "content": [f"r:{name}"]}
        finally:
            active -= 1

    orch._mcp.call_tool = fake_call_tool

    events = []
    async for ev in orch.run([{"role": "user", "content": "go"}]):
        events.append(ev)

    approvals = [e for e in events if e.type == "approval_required"]
    tool_results = [e for e in events if e.type == "tool_result"]

    # The write tool is gated (surfaced for approval) and NEVER executed.
    assert [a.data["tool_name"] for a in approvals] == ["storage"]
    assert "storage" not in executed
    # Both reads executed, in parallel, and their results emitted in order.
    assert set(executed) == {"arm", "monitor"}
    assert [e.data["tool_name"] for e in tool_results] == ["arm", "monitor"]
    assert max_active > 1

    orch.close()
