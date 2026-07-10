from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api import chats
from app.core.security import Principal
from app.models import Chat


class _FakeDb:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self._next_id = 1

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        return None

    async def refresh(self, value: Any) -> None:
        if isinstance(value, Chat) and not value.id:
            value.id = f"chat-{self._next_id}"
            self._next_id += 1


def _principal() -> Principal:
    return Principal(
        subject="user-1",
        email="user@example.com",
        tenant_id="tenant-1",
        role="admin",
        permissions=frozenset({"chat.use", "workloads.read"}),
    )


def test_resolve_deep_review_workloads_deduplicates_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    workloads = {
        "w1": {"id": "w1", "name": "One"},
        "w2": {"id": "w2", "name": "Two"},
    }
    monkeypatch.setattr(
        "app.workloads.registry.get_workload",
        lambda workload_id: workloads.get(workload_id),
    )

    resolved = chats._resolve_deep_review_workloads(["w1", "w1", " w2 "])
    assert [workload["id"] for workload in resolved] == ["w1", "w2"]

    with pytest.raises(HTTPException) as exc:
        chats._resolve_deep_review_workloads(["missing"])
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fleet_creates_named_chats_and_starts_all_eight_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workloads = {
        "w1": {"id": "w1", "name": "Analytics", "connection_id": "conn-1"},
        "w2": {"id": "w2", "name": "Billing", "connection_id": "conn-2"},
    }
    monkeypatch.setattr(
        "app.workloads.registry.get_workload",
        lambda workload_id: workloads.get(workload_id),
    )
    monkeypatch.setattr(
        "app.core.azure_connections.connection_for_workload",
        lambda workload: {"id": workload["connection_id"]},
    )
    monkeypatch.setattr(chats, "_active_provider", lambda: "test-provider")
    monkeypatch.setattr(chats, "active_model", lambda: "test-model")

    started: list[tuple[str, Any, Any]] = []

    async def fake_start(chat_id, payload, principal, db, *, worker_gate=None):
        started.append((chat_id, payload, worker_gate))
        return None

    monkeypatch.setattr(chats, "_start_message_turn", fake_start)
    db = _FakeDb()
    result = await chats.launch_deep_review_fleet(
        chats.DeepReviewFleetRequest(workload_ids=["w1", "w2"]),
        _principal(),
        db,  # type: ignore[arg-type]
    )

    created = [value for value in db.added if isinstance(value, Chat)]
    assert [chat.title for chat in created] == [
        "Deep review: Analytics",
        "Deep review: Billing",
    ]
    assert [chat.workload_id for chat in created] == ["w1", "w2"]
    assert [chat.connection_id for chat in created] == ["conn-1", "conn-2"]
    assert all(chat.thinking_level == "deep" for chat in created)

    assert result["launched"] == 2
    assert result["agent_count"] == 8
    assert len(started) == 2
    for _, payload, gate in started:
        assert payload.content == chats.DEEP_RELIABILITY_REVIEW_PROMPT
        assert payload.thinking_level == "deep"
        assert payload.deep_agents == [
            "networking",
            "identity",
            "compute",
            "storage",
            "security",
            "reliability",
            "cost",
            "monitoring",
        ]
        assert gate is chats._deep_review_fleet_gate
