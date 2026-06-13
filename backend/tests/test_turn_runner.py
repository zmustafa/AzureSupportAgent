"""Tests for the turn registry's live-activity tracking (powers the Monitor live ops)."""
from app.agent.turn_runner import TurnRegistry, TurnRun


def test_tool_start_sets_current_tool_and_counts():
    run = TurnRun("chat-1", "msg-1")
    run.emit("tool_start", {"tool_name": "az_resource_graph"})
    assert run.current_tool == "az_resource_graph"
    assert run.tool_count == 1


def test_tool_result_clears_current_tool():
    run = TurnRun("chat-1", "msg-1")
    run.emit("tool_start", {"tool_name": "az_resource_graph"})
    run.emit("tool_result", {"summary": "ok"})
    assert run.current_tool is None
    assert run.tool_count == 1  # the count is not decremented


def test_multiple_tools_increment_count():
    run = TurnRun("chat-1", "msg-1")
    run.emit("tool_start", {"tool_name": "a"})
    run.emit("tool_result", {})
    run.emit("tool_start", {"tool_name": "b"})
    assert run.tool_count == 2
    assert run.current_tool == "b"


def test_deep_investigation_promotes_kind():
    run = TurnRun("chat-1", "msg-1")
    assert run.kind == "chat"
    run.emit("phase", {"phase": "research"})
    assert run.kind == "deep"


def test_approval_required_tracked_as_tool():
    run = TurnRun("chat-1", "msg-1")
    run.emit("approval_required", {"tool_name": "az_delete"})
    assert run.current_tool == "az_delete"
    assert run.tool_count == 1


def test_live_meta_shape():
    run = TurnRun("chat-1", "msg-1")
    run.emit("tool_start", {"tool_name": "x"})
    meta = run.live_meta()
    assert meta["chat_id"] == "chat-1"
    assert meta["kind"] == "chat"
    assert meta["current_tool"] == "x"
    assert meta["tool_count"] == 1
    assert meta["elapsed_s"] >= 0


def test_registry_live_snapshot_only_active():
    reg = TurnRegistry()
    run = TurnRun("chat-1", "msg-1")
    reg._runs["chat-1"] = run  # noqa: SLF001 - white-box test of the registry
    assert reg.is_active("chat-1") is True
    assert "chat-1" in reg.live_snapshot()
    run.done = True
    assert reg.is_active("chat-1") is False
    assert reg.live_snapshot() == {}
    assert reg.active_chat_ids() == []
