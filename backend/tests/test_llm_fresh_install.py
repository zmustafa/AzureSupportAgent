"""Fresh-install defaults for LLM provider config.

Locks in the product behavior: on a *fresh install* (no ``LLM_PROVIDER`` and no
``LLM_API_KEY`` in the environment) EVERY provider seeds as ``disabled`` — the admin
sees the full provider list in the rail, all turned off, and enables one by setting it
up. Env-driven deployments (a real credential, or an explicit ``LLM_PROVIDER``) keep
auto-enabling their provider so existing setups are unchanged.
"""
from __future__ import annotations

from app.core import llm_config
from app.core.config import Settings


def _seed_with(monkeypatch, **overrides):
    """Build the default config as if the process started with the given env settings.

    ``_env_file=None`` ignores any developer ``.env``, and the ``delenv`` calls below clear
    the matching PROCESS environment variables. Both are needed for "a clean host": pydantic
    reads real env vars regardless of ``_env_file``, so without this the test silently
    inverts its own premise on any machine (or CI job) that exports one of them.
    """
    for var in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL",
                "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(llm_config, "get_settings", lambda: settings)
    return llm_config._default_config()


def test_fresh_install_disables_every_provider(monkeypatch):
    cfg = _seed_with(monkeypatch)  # no LLM_PROVIDER, no LLM_API_KEY
    providers = cfg["providers"]
    assert providers, "expected the default provider catalog to be non-empty"
    enabled = [name for name, p in providers.items() if not p.get("disabled")]
    assert enabled == [], f"fresh install must enable nothing, got {enabled}"
    assert all(p.get("disabled") for p in providers.values())


def test_env_api_key_enables_openai(monkeypatch):
    cfg = _seed_with(monkeypatch, llm_api_key="sk-test")
    enabled = [n for n, p in cfg["providers"].items() if not p.get("disabled")]
    assert enabled == ["openai"]


def test_explicit_llm_provider_enables_only_that_provider(monkeypatch):
    cfg = _seed_with(monkeypatch, llm_provider="ollama")
    enabled = [n for n, p in cfg["providers"].items() if not p.get("disabled")]
    assert enabled == ["ollama"]
