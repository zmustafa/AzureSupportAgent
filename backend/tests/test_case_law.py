"""Tests for Case Law: writing an investigation RCA back into architecture Memory."""
from __future__ import annotations

import importlib

from app.architectures import memory as mem
from app.architectures import memory_revisions as revs


def _isolate(tmp_path, monkeypatch):
    """Point the JSON stores at a temp dir so tests don't touch real .data."""
    monkeypatch.setattr(mem, "_PATH", tmp_path / "architecture_memory.json")
    monkeypatch.setattr(revs, "_PATH", tmp_path / "architecture_memory_revisions.json")


def test_append_known_issue_creates_memory_and_section(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    entry = mem.build_known_issue_entry(
        root_cause="SNAT port exhaustion",
        summary="Outbound connections exhausted SNAT ports under load.",
        severity="warning",
        evidence=["Conn timeouts in logs"],
        actions=["Add a NAT gateway"],
        confidence=82,
        chat_title="Prod outage",
        message_id="msg-1",
    )
    saved, appended = mem.append_known_issue(
        "arch-1", entry_markdown=entry, dedupe_token="ref msg-1",
        actor="me@example.com", tenant_id="t1", workload_id="wl-1", title="Web app",
    )
    assert appended is True
    assert saved is not None
    ki = next(s for s in saved["sections"] if s["key"] == "known_issues")
    assert "SNAT port exhaustion" in ki["content"]
    assert "ref msg-1" in ki["content"]
    assert saved["workload_id"] == "wl-1"


def test_append_known_issue_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    entry = mem.build_known_issue_entry(root_cause="DNS misconfig", message_id="msg-9")
    mem.append_known_issue("arch-2", entry_markdown=entry, dedupe_token="ref msg-9", tenant_id="t1")
    saved2, appended2 = mem.append_known_issue(
        "arch-2", entry_markdown=entry, dedupe_token="ref msg-9", tenant_id="t1",
    )
    assert appended2 is False  # already present → no duplicate
    ki = next(s for s in saved2["sections"] if s["key"] == "known_issues")
    assert ki["content"].count("DNS misconfig") == 1


def test_append_preserves_existing_known_issues(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # Seed an existing memory with hand-written known issues.
    mem.upsert_memory(
        "arch-3", tenant_id="t1",
        sections=[{"key": "known_issues", "label": "Known issues & past incidents",
                   "content": "Prior: cache stampede on cold start."}],
    )
    entry = mem.build_known_issue_entry(root_cause="New: thread pool starvation", message_id="msg-3")
    saved, appended = mem.append_known_issue("arch-3", entry_markdown=entry, dedupe_token="ref msg-3", tenant_id="t1")
    assert appended is True
    ki = next(s for s in saved["sections"] if s["key"] == "known_issues")
    assert "cache stampede" in ki["content"]  # old content kept
    assert "thread pool starvation" in ki["content"]  # new appended


def test_known_issues_is_injected_into_investigations():
    # Retrieval side of the loop must already be wired (no writeback needed for this).
    importlib.reload(mem)
    assert "known_issues" in mem.INVESTIGATION_PRIORITY_KEYS
    rendered = mem.render_for_investigation(
        {"sections": [{"key": "known_issues", "label": "Known issues & past incidents",
                       "content": "Recurring SNAT exhaustion under load."}]}
    )
    assert "SNAT exhaustion" in rendered
