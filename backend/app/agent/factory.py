"""Provider factory: builds the active LLM provider from runtime config."""
from __future__ import annotations

from app.agent.claude_provider import ClaudeProvider
from app.agent.codex_provider import CodexProvider
from app.agent.github_copilot import GitHubCopilotChatProvider
from app.agent.openai_provider import OpenAIProvider
from app.agent.provider import LLMProvider
from app.core.config import Settings
from app.core.llm_config import get_active

# Providers that speak the plain OpenAI Chat Completions API (with a base_url).
_OPENAI_COMPATIBLE = {
    "openai",
    "openai_eu",
    "azure_openai",
    "github",
    "ollama",
    "grok",
    "mistral",
    "gemini",
    "openrouter",
    "lmstudio",
}


def build_provider(_settings: Settings | None = None) -> LLMProvider:
    """Build the provider from the runtime LLM config (admin-configurable).

    The `_settings` arg is kept for backward compatibility with existing callers
    but is no longer used — config comes from app.core.llm_config so changes in the
    admin dashboard take effect immediately without a restart.
    """
    return build_provider_for(None, None)


def build_provider_for(
    provider_override: str | None, model_override: str | None
) -> LLMProvider:
    """Build a provider for a specific provider/model (e.g. a per-chat selection),
    falling back to the globally-active provider when overrides are None."""
    cfg = get_active(provider_override, model_override)
    provider = (cfg["provider"] or "openai").lower()

    if provider == "github_copilot":
        # GitHub Copilot uses its own web-chat-thread protocol (not OpenAI-compatible) and
        # authenticates from a browser session (app.agent.github_copilot_auth), which is the
        # source of truth. Do NOT pass the stored config api_key — a stale captured token
        # there would bypass the auto-refresh and 401 once the session rotates.
        return GitHubCopilotChatProvider(
            model=cfg["model"], base_url=cfg["base_url"]
        )
    if provider == "chatgpt":
        # ChatGPT Codex uses the OAuth Responses API (not chat/completions). The OAuth
        # token (auto-refreshed in app.agent.chatgpt_oauth) is the source of truth, so we
        # deliberately do NOT pass the stored config api_key — it may be a stale captured
        # access token that bypasses the refresh flow and 401s after the session rotates.
        return CodexProvider(
            model=cfg["model"], base_url=cfg["base_url"]
        )
    if provider == "claude":
        # Anthropic native Messages API with native tool calling.
        return ClaudeProvider(
            model=cfg["model"], api_key=cfg["api_key"], base_url=cfg["base_url"]
        )
    if provider == "claude_oauth":
        # Claude Pro/Max subscription via OAuth. The token (auto-refreshed in
        # app.agent.claude_oauth) is the source of truth, so we deliberately do NOT pass
        # the stored config api_key. The provider adds the Bearer token + oauth beta
        # header + Claude Code system preamble required for OAuth inference.
        return ClaudeProvider(
            model=cfg["model"], base_url=cfg["base_url"], use_oauth=True
        )
    if provider == "azure_foundry":
        # Azure AI Foundry model-inference endpoint (…services.ai.azure.com/models):
        # OpenAI-compatible chat/completions with an api-version query param + Bearer auth.
        # Normalize the saved endpoint to the "/models" inference root the SDK appends to.
        base = (cfg["base_url"] or "").rstrip("/")
        if base and not base.endswith("/models"):
            base += "/models"
        return OpenAIProvider(
            provider="azure_foundry",
            api_key=cfg["api_key"],
            model=cfg["model"],
            base_url=base,
            api_version=cfg["api_version"] or "2024-05-01-preview",
        )
    if provider in _OPENAI_COMPATIBLE:
        return OpenAIProvider(
            provider=provider,
            api_key=cfg["api_key"],
            model=cfg["model"],
            base_url=cfg["base_url"],
            api_version=cfg["api_version"],
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")


def active_model() -> str:
    """The model name of the currently active provider (for usage records, etc.)."""
    return get_active().get("model", "")
