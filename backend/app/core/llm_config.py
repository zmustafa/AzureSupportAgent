"""Runtime LLM provider configuration.

Persisted to a small JSON file so admins can change provider, model, and API keys
at runtime (via the admin dashboard) WITHOUT restarting the backend. build_provider
reads the active config on every request, so changes take effect immediately.

Supported providers (both OpenAI-compatible):
- openai:        api.openai.com (keys start with sk-...)
- github:        GitHub Models / Copilot (OpenAI-compatible, GitHub PAT)
- azure_openai:  Azure OpenAI endpoint + deployment
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings

_CONFIG_PATH = Path(__file__).resolve().parents[2] / ".data" / "llm_config.json"

# Default GitHub Models (Copilot) OpenAI-compatible inference endpoint.
GITHUB_BASE_URL = "https://models.github.ai/inference"
# GitHub Copilot subscription API (Claude, Gemini, GPT-5.x, o-series, etc.).
GITHUB_COPILOT_BASE_URL = "https://api.githubcopilot.com"
# Default local Ollama OpenAI-compatible endpoint.
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# ChatGPT Codex (OAuth) backend used by the Codex CLI.
CHATGPT_BASE_URL = "https://chatgpt.com/backend-api/codex"
# OpenAI EU data-residency endpoint (same API as US, hosted in the EU region).
OPENAI_EU_BASE_URL = "https://eu.api.openai.com/v1"
# OpenAI-compatible third-party endpoints (ported from BuddyAI's GetDefaultBaseUrl).
GROK_BASE_URL = "https://api.x.ai/v1"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
CLAUDE_BASE_URL = "https://api.anthropic.com"

# Curated fallback model lists shown when the live model list can't be fetched.
OPENAI_FALLBACK_MODELS = [
    "gpt-5.5",
    "gpt-5.1",
    "gpt-5",
    "gpt-4.1",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o4-mini",
]
GITHUB_FALLBACK_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o3",
    "openai/o4-mini",
    "microsoft/Phi-4",
    "meta/Llama-3.3-70B-Instruct",
    "mistral-ai/Mistral-Large-2411",
]
GITHUB_COPILOT_FALLBACK_MODELS = [
    "claude-opus-4.8",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2",
    "gpt-4.1",
]
OLLAMA_FALLBACK_MODELS = [
    "llama3.2",
    "llama3.1",
    "qwen2.5",
    "mistral",
    "phi4",
    "gemma2",
]
CHATGPT_FALLBACK_MODELS = [
    # Only these work via Codex with a ChatGPT account (verified against the API).
    # Other model ids (gpt-5.x-codex, gpt-5.1, gpt-5, gpt-4.1, o3, o4-mini, etc.)
    # return 400 "not supported when using Codex with a ChatGPT account".
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
]
GROK_FALLBACK_MODELS = [
    "grok-4-0709",
    "grok-4",
    "grok-code-fast-1",
    "grok-3",
    "grok-3-mini",
    "grok-3-fast",
    "grok-2-vision-1212",
]
MISTRAL_FALLBACK_MODELS = [
    "mistral-large-latest",
    "mistral-small-latest",
    "ministral-8b-latest",
    "ministral-3b-latest",
    "pixtral-12b-2409",
    "codestral-latest",
    "open-mistral-nemo",
]
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
OPENROUTER_FALLBACK_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.7-sonnet",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
    "x-ai/grok-2-1212",
]
LMSTUDIO_FALLBACK_MODELS = [
    "local-model",
]
CLAUDE_FALLBACK_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
]


def _default_config() -> dict[str, Any]:
    """Seed config from env settings so an existing .env setup keeps working."""
    s = get_settings()
    return {
        "active_provider": s.llm_provider or "openai",
        "providers": {
            "openai": {
                "api_key": s.llm_api_key or "",
                "model": s.llm_model or "gpt-4.1",
                "base_url": "",
            },
            "openai_eu": {
                "api_key": "",
                "model": "gpt-5.5",
                "base_url": OPENAI_EU_BASE_URL,
            },
            "github": {
                "api_key": "",
                "model": "openai/gpt-4o",
                "base_url": GITHUB_BASE_URL,
            },
            "github_copilot": {
                "api_key": "",  # Optional override; usually read from BuddyAI's cache.
                "model": "claude-sonnet-4.6",
                "base_url": GITHUB_COPILOT_BASE_URL,
            },
            "ollama": {
                "api_key": "ollama",  # Ollama ignores the key but the client needs one.
                "model": "llama3.2",
                "base_url": OLLAMA_BASE_URL,
            },
            "chatgpt": {
                "api_key": "",  # OAuth access token, set via the sign-in flow.
                "model": "gpt-5.5",
                "base_url": CHATGPT_BASE_URL,
            },
            "azure_openai": {
                "api_key": s.llm_api_key or "",
                "model": s.azure_openai_deployment or s.llm_model or "",
                "base_url": s.azure_openai_endpoint or "",
                "api_version": s.azure_openai_api_version or "2024-10-21",
            },
            "grok": {"api_key": "", "model": "grok-4", "base_url": GROK_BASE_URL},
            "mistral": {"api_key": "", "model": "mistral-large-latest", "base_url": MISTRAL_BASE_URL},
            "gemini": {"api_key": "", "model": "gemini-2.5-flash", "base_url": GEMINI_BASE_URL},
            "openrouter": {
                "api_key": "",
                "model": "openai/gpt-4o",
                "base_url": OPENROUTER_BASE_URL,
            },
            "lmstudio": {"api_key": "lmstudio", "model": "local-model", "base_url": LMSTUDIO_BASE_URL},
            "claude": {"api_key": "", "model": "claude-sonnet-4-6", "base_url": CLAUDE_BASE_URL},
        },
    }


def load_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            # Merge over defaults so new providers/fields appear for old files.
            base = _default_config()
            base["active_provider"] = data.get("active_provider", base["active_provider"])
            for name, prov in data.get("providers", {}).items():
                base["providers"].setdefault(name, {}).update(prov)
            return base
        except (json.JSONDecodeError, OSError):
            pass
    return _default_config()


def save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_active(
    provider_override: str | None = None, model_override: str | None = None
) -> dict[str, Any]:
    """Resolved provider config for building the LLM client.

    With no arguments, returns the globally-active provider. Pass `provider_override`
    and/or `model_override` to resolve a specific provider/model (e.g. a per-chat
    selection) using that provider's saved credentials.
    """
    cfg = load_config()
    provider = (provider_override or cfg.get("active_provider", "openai")).lower()
    prov = cfg.get("providers", {}).get(provider, {})
    base_url = prov.get("base_url", "")
    _defaults = {
        "openai_eu": OPENAI_EU_BASE_URL,
        "github": GITHUB_BASE_URL,
        "github_copilot": GITHUB_COPILOT_BASE_URL,
        "ollama": OLLAMA_BASE_URL,
        "chatgpt": CHATGPT_BASE_URL,
        "grok": GROK_BASE_URL,
        "mistral": MISTRAL_BASE_URL,
        "gemini": GEMINI_BASE_URL,
        "openrouter": OPENROUTER_BASE_URL,
        "lmstudio": LMSTUDIO_BASE_URL,
        "claude": CLAUDE_BASE_URL,
    }
    if not base_url and provider in _defaults:
        base_url = _defaults[provider]
    # Ollama / LM Studio need a non-empty key for the OpenAI client even though it's ignored.
    api_key = prov.get("api_key", "")
    if provider in ("ollama", "lmstudio") and not api_key:
        api_key = provider
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model_override or prov.get("model", ""),
        "base_url": base_url,
        "api_version": prov.get("api_version", "2024-10-21"),
    }


def public_config() -> dict[str, Any]:
    """Config safe to send to the UI: keys masked, never the raw secret."""
    cfg = load_config()
    providers = {}
    for name, prov in cfg.get("providers", {}).items():
        key = prov.get("api_key", "")
        providers[name] = {
            "model": prov.get("model", ""),
            "base_url": prov.get("base_url", ""),
            "api_version": prov.get("api_version", ""),
            "free_only": bool(prov.get("free_only", False)),
            "disabled": bool(prov.get("disabled", False)),
            "hidden_models": list(prov.get("hidden_models", []) or []),
            "has_key": bool(key),
            "key_hint": (key[:6] + "…" + key[-2:]) if len(key) > 10 else "",
        }
    return {"active_provider": cfg.get("active_provider", "openai"), "providers": providers}
