"""Connector framework: external services exposed to the agent as callable tools.

A *connector* (Teams, Outlook, Jira, Grafana, …) is admin-configured in Settings and,
once enabled, contributes one or more *tools* the LLM can call during a turn — exactly
like Azure SRE Agent's "connectors provide tools" model. Tool results use the same
``{"isError": bool, "content": [...]}`` shape as the MCP client, so the orchestrator's
existing tool-call loop dispatches connector and MCP tools uniformly.

Secrets live in the encrypted connector registry; handlers receive the decrypted config.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# A handler executes a tool call: (decrypted connector config, arguments) -> result.
ToolHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ConnectorTool:
    """One callable capability contributed by a connector."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (OpenAI tools format)
    kind: str  # "read" | "write"
    handler: ToolHandler


@dataclass
class FieldSpec:
    """Describes a single config field for the connector setup form."""

    key: str
    label: str
    type: str = "text"  # text | password | textarea | url | select
    placeholder: str = ""
    secret: bool = False
    options: list[str] = field(default_factory=list)
    optional: bool = False
    help: str = ""


@dataclass
class ConnectorType:
    """Static metadata + tool factory for a kind of connector."""

    id: str
    label: str
    description: str
    # Auth/config "modes" (e.g. webhook vs graph). Each mode has its own field set.
    modes: dict[str, list[FieldSpec]]
    # Build the live tools for a configured connector (config has `mode` + fields).
    build_tools: Callable[[dict[str, Any]], list[ConnectorTool]]


def ok(text: str) -> dict[str, Any]:
    """A successful tool result in the shared MCP-compatible shape."""
    return {"isError": False, "content": [text]}


def err(text: str) -> dict[str, Any]:
    """A failed tool result in the shared MCP-compatible shape."""
    return {"isError": True, "content": [text]}


class ConnectorToolset:
    """Combined, name-indexed view of the tools from a set of enabled connectors.

    The orchestrator asks this for tool specs and routes any call whose name we own to
    the right connector handler, passing the connector's decrypted config.
    """

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ConnectorTool, dict[str, Any]]] = {}

    def add_connector(self, conn: dict[str, Any], tools: list[ConnectorTool]) -> None:
        for t in tools:
            self._tools[t.name] = (t, conn)

    def has(self, name: str) -> bool:
        return name in self._tools

    def kind(self, name: str) -> str:
        entry = self._tools.get(name)
        return entry[0].kind if entry else "write"

    def specs(self) -> list[dict[str, Any]]:
        """Tool definitions for the LLM (name/description/parameters)."""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t, _ in self._tools.values()
        ]

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(name)
        if entry is None:
            return err(f"Unknown connector tool '{name}'.")
        tool, conn = entry
        try:
            return await tool.handler(conn, arguments or {})
        except Exception as exc:  # noqa: BLE001 - surface tool failures to the model
            from app.core.utils import format_error

            return err(f"{name} failed: {format_error(exc)}")
