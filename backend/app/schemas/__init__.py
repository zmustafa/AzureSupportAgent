"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChatCreate(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None
    # Azure connection (tenant) this chat is bound to.
    connection_id: str | None = None


class ChatOut(BaseModel):
    id: str
    title: str
    provider: str | None = None
    model: str | None
    connection_id: str | None = None
    thinking_level: str = "normal"
    agent_id: str | None = None
    workload_id: str | None = None
    archived: bool
    pinned: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    activity: list | None = None
    images: list | None = None
    provider: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    investigation: dict | None = None
    created_at: datetime

    class Config:
        from_attributes = True

    @staticmethod
    def from_model(m) -> "MessageOut":
        return MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            activity=m.activity_json,
            images=m.images_json,
            provider=m.provider,
            model=m.model,
            duration_ms=getattr(m, "duration_ms", None),
            investigation=getattr(m, "investigation_json", None),
            created_at=m.created_at,
        )


class MessageCreate(BaseModel):
    content: str
    # Optional base64 data-URL images pasted/attached by the user (vision input).
    images: list[str] = []
    # Optional scope chosen by the user from a clarification prompt.
    # subscription_id restricts the investigation; if None and scope_all is False the
    # backend may ask the user to pick. scope_all=True searches everything.
    subscription_id: str | None = None
    subscription_name: str | None = None
    # Optional management-group scope chosen from a clarification prompt. When set, the
    # backend constrains the investigation to this management group (governance scope).
    management_group_id: str | None = None
    management_group_name: str | None = None
    # Optional Azure connection (tenant) chosen for this turn. When set, the agent's
    # MCP session is bound to this tenant's identity; persisted on the chat.
    connection_id: str | None = None
    tenant_id: str | None = None
    tenant_name: str | None = None
    scope_all: bool = False
    # Reasoning effort for this turn: "normal" (standard) or "deep" (structured
    # multi-phase deep investigation: research -> hypotheses -> validation -> conclusion).
    thinking_level: str | None = None
    # Specialist investigation agents (ids) the user chose to dispatch for a deep
    # investigation's "war room". Empty = let the investigator decide / no roster.
    deep_agents: list[str] = []
    # Optional architecture id whose Memory (intended design + known gaps + diagnostic
    # hints) should be injected into a deep investigation as expert context. The user
    # picks this when several architectures share the chat's workload. None = auto-resolve.
    architecture_memory_id: str | None = None
    # Optional custom agent to run this turn as (persona + tools + model). Empty string
    # explicitly clears any saved agent (back to the default assistant); None leaves it.
    agent_id: str | None = None
    # Optional Azure Workload (hand-picked resource scope) to constrain this turn to.
    # Empty string clears it; None leaves the chat's saved workload.
    workload_id: str | None = None
    # When true, regenerate the last assistant answer in place: don't append a new
    # user message; instead drop the previous assistant reply and re-run the turn.
    regenerate: bool = False


class ApprovalDecision(BaseModel):
    decision: str  # approved | rejected
    reason: str | None = Field(default=None, max_length=2000)


class ApprovalOut(BaseModel):
    id: str
    tool_call_id: str
    requested_by: str
    decision: str
    reason: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ToolCallOut(BaseModel):
    id: str
    tool_name: str
    kind: str
    status: str
    arguments_json: dict
    subscription_id: str | None
    created_at: datetime

    class Config:
        from_attributes = True
