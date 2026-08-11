"""Provider tool-surface capabilities and hard-limit validation.

The application routes to a much smaller operating budget, but adapters still enforce
provider hard limits as defense in depth. Unknown/OpenAI-compatible endpoints receive a
conservative limit instead of relying on a provider 400.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.provider import ToolSpec


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    model: str
    max_tool_definitions: int
    recommended_initial_tools: int = 24
    recommended_max_tools: int = 32
    native_tool_search: bool = False
    api: str = "chat_completions"


_OPENAI_CHAT_PROVIDERS = frozenset({
    "openai", "openai_eu", "azure_openai", "azure_foundry", "github",
    "github_copilot", "gemini", "grok", "mistral", "openrouter", "ollama",
    "lmstudio",
})


def _openai_native_search_model(model: str) -> bool:
    value = (model or "").lower()
    # Tool search is a Responses API feature. Keep this allowlist deliberately narrow;
    # unsupported models retain the provider-neutral local router.
    return value.startswith(("gpt-5.4", "gpt-5.5"))


def capabilities_for(provider: str | None, model: str | None = None) -> ProviderCapabilities:
    name = (provider or "openai").strip().lower()
    model_name = (model or "").strip()
    if name in {"openai", "openai_eu"}:
        return ProviderCapabilities(
            provider=name,
            model=model_name,
            max_tool_definitions=128,
            native_tool_search=_openai_native_search_model(model_name),
            api="responses" if _openai_native_search_model(model_name) else "chat_completions",
        )
    if name in _OPENAI_CHAT_PROVIDERS:
        return ProviderCapabilities(name, model_name, 128)
    if name in {"claude", "claude_oauth"}:
        return ProviderCapabilities(name, model_name, 128, api="messages")
    if name == "chatgpt":
        # ChatGPT Codex uses a Responses-shaped endpoint but currently receives the
        # compact ReAct catalog, not arbitrary native function definitions.
        return ProviderCapabilities(name, model_name, 128, api="codex_responses")
    return ProviderCapabilities(name, model_name, 128)


class ToolDefinitionLimitError(RuntimeError):
    """Raised before a provider request would exceed its tool-definition limit."""

    def __init__(self, provider: str, offered: int, limit: int) -> None:
        self.provider = provider
        self.offered = offered
        self.limit = limit
        super().__init__(
            "Tool catalog exceeds the provider limit: "
            f"{provider} accepts at most {limit} tool definitions, but {offered} were selected. "
            "The request was stopped before contacting the model. Reduce the routing budget "
            "or review the effective tool policy."
        )


def validate_tool_count(
    provider: str,
    model: str,
    tools: list[ToolSpec] | None,
    *,
    include_deferred: bool = True,
) -> ProviderCapabilities:
    caps = capabilities_for(provider, model)
    if not tools:
        return caps
    offered = len(tools) if include_deferred else sum(not t.defer_loading for t in tools)
    if offered > caps.max_tool_definitions:
        raise ToolDefinitionLimitError(provider, offered, caps.max_tool_definitions)
    return caps
