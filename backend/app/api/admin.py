"""Admin dashboard endpoints: tools, approvals, usage, audit. Admin role required."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.config import get_settings
from app.core.db import get_db, _is_sqlite
from app.agent import chatgpt_oauth, claude_oauth, github_copilot_auth
from app.core.llm_config import (
    CHATGPT_FALLBACK_MODELS,
    CLAUDE_FALLBACK_MODELS,
    GEMINI_FALLBACK_MODELS,
    GITHUB_COPILOT_FALLBACK_MODELS,
    GITHUB_FALLBACK_MODELS,
    GROK_FALLBACK_MODELS,
    LMSTUDIO_FALLBACK_MODELS,
    LOCAL_PROVIDERS,
    MISTRAL_FALLBACK_MODELS,
    OLLAMA_BASE_URL,
    OLLAMA_FALLBACK_MODELS,
    OPENAI_FALLBACK_MODELS,
    OPENROUTER_FALLBACK_MODELS,
    load_config,
    public_config,
    save_config,
    set_provider_enabled,
)
from app.core.security import Principal, require_permission
from app.mcp.client import build_mcp_client
from app.models import (
    Approval,
    AssessmentRun,
    AuditLog,
    Chat,
    Message,
    ScheduledTask,
    TaskRun,
    ToolCall,
    Usage,
)
from app.schemas import ApprovalDecision, ApprovalOut, ToolCallOut

router = APIRouter(prefix="/admin", tags=["admin"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). Admin config remains admin-only because only the admin
# role carries settings.write. See app.auth.permissions for the catalog.
require_admin = require_permission("settings.write")
settings = get_settings()


# Dialect-agnostic date bucketing for the monitor activity charts: SQLite uses
# strftime, PostgreSQL uses to_char. Returned expressions yield the same string format.
def _day_bucket(date_col):
    if _is_sqlite:
        return func.strftime("%Y-%m-%d", date_col)
    return func.to_char(date_col, "YYYY-MM-DD")


def _hour_bucket(date_col):
    if _is_sqlite:
        return func.strftime("%Y-%m-%d %H", date_col)
    return func.to_char(date_col, "YYYY-MM-DD HH24")


# ---------------------------------------------------------------------------
# Application settings (custom instructions, behavior toggles)
# ---------------------------------------------------------------------------
class AppSettingsUpdate(BaseModel):
    custom_instructions: str | None = Field(default=None, max_length=20000)
    response_style: str | None = None
    max_tokens: int | None = None
    auto_title: bool | None = None
    scope_clarification: bool | None = None
    mgmt_group_clarification: bool | None = None
    propose_problems: bool | None = None
    suggestions: bool | None = None
    deep_parallel_enabled: bool | None = None
    deep_parallel_count: int | None = None
    progress_detail: str | None = None
    retention_days: int | None = None
    mcp_read_only: bool | None = None
    entra_mcp_enabled: bool | None = None
    auto_execute_writes: bool | None = None
    max_tool_iterations: int | None = None
    tool_result_limit: int | None = None
    tool_discovery_limit: int | None = None
    request_timeout_seconds: int | None = None
    command_execution_enabled: bool | None = None
    command_allowlist: list[str] | None = None
    command_timeout_seconds: int | None = None
    assessment_severity_weights: dict[str, int] | None = None
    assessment_score_good: int | None = None
    assessment_score_warn: int | None = None
    architecture_category_colors: dict[str, str] | None = None
    # Policy exemption guardrails (enforced on create/extend).
    policy_exemption_require_justification: bool | None = None
    policy_exemption_max_expiry_days: int | None = None
    policy_exemption_block_never_expires: bool | None = None
    # Change Explorer.
    changeexplorer_resolve_identities: bool | None = None
    changeexplorer_change_limit: int | None = None


@router.get("/settings")
async def get_app_settings(_: Principal = Depends(require_admin)):
    from app.core.app_settings import ALLOWED_COMMAND_BINARIES, RESPONSE_STYLES, load_settings

    return {
        "settings": load_settings(),
        "response_styles": list(RESPONSE_STYLES.keys()),
        "command_binaries": list(ALLOWED_COMMAND_BINARIES),
    }


@router.put("/settings")
async def update_app_settings(
    payload: AppSettingsUpdate,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.app_settings import save_settings

    updated = save_settings(payload.model_dump(exclude_none=True))
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="settings.update",
            target="app_settings",
        )
    )
    await db.commit()
    return {"settings": updated}


# ---------------------------------------------------------------------------
# AI prompts (admin-editable instructions used behind the scenes)
# ---------------------------------------------------------------------------
class AiPromptsUpdate(BaseModel):
    values: dict[str, str]


@router.get("/ai-prompts")
async def get_ai_prompts(_: Principal = Depends(require_admin)):
    from app.core.ai_prompts import list_prompts

    return {"prompts": list_prompts()}


@router.put("/ai-prompts")
async def update_ai_prompts(
    payload: AiPromptsUpdate,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.ai_prompts import list_prompts, save_prompts

    save_prompts(payload.values)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="ai_prompts.update",
            target=",".join(payload.values.keys())[:512],
        )
    )
    await db.commit()
    return {"prompts": list_prompts()}


@router.post("/ai-prompts/{prompt_id}/reset")
async def reset_ai_prompt(
    prompt_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.ai_prompts import list_prompts, reset_prompt

    if not reset_prompt(prompt_id):
        raise HTTPException(status_code=404, detail="Unknown prompt.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="ai_prompts.reset",
            target=prompt_id,
        )
    )
    await db.commit()
    return {"prompts": list_prompts()}


# ---------------------------------------------------------------------------
# LLM provider configuration (OpenAI + GitHub Copilot / Models)
# ---------------------------------------------------------------------------
class ProviderUpdate(BaseModel):
    model: str | None = None
    api_key: str | None = None  # only set when the admin enters a new key
    base_url: str | None = None
    api_version: str | None = None
    free_only: bool | None = None  # OpenRouter: only show :free models
    disabled: bool | None = None  # hide this provider from the chat model picker
    # Per-provider list of model ids to hide from the chat model picker (the admin
    # can still see them in the Settings → Manage visibility panel to unhide).
    hidden_models: list[str] | None = None


class LLMConfigUpdate(BaseModel):
    active_provider: str | None = None
    providers: dict[str, ProviderUpdate] | None = None


@router.get("/llm/config")
async def get_llm_config(_: Principal = Depends(require_admin)):
    """Current LLM configuration (keys masked)."""
    return public_config()


@router.put("/llm/config")
async def update_llm_config(
    payload: LLMConfigUpdate,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update active provider, per-provider model and API keys.

    An empty/omitted api_key leaves the existing key untouched (so the masked key
    in the UI doesn't overwrite the real secret).
    """
    cfg = load_config()
    if payload.active_provider:
        if payload.active_provider not in cfg.get("providers", {}):
            raise HTTPException(status_code=400, detail="Unknown provider")
        cfg["active_provider"] = payload.active_provider
    if payload.providers:
        for name, upd in payload.providers.items():
            prov = cfg.setdefault("providers", {}).setdefault(name, {})
            if upd.model is not None:
                prov["model"] = upd.model
            if upd.base_url is not None:
                prov["base_url"] = upd.base_url
            if upd.api_version is not None:
                prov["api_version"] = upd.api_version
            if upd.free_only is not None:
                prov["free_only"] = upd.free_only
            if upd.disabled is not None:
                prov["disabled"] = upd.disabled
            if upd.hidden_models is not None:
                # De-duplicate + sort so the file stays stable on disk.
                prov["hidden_models"] = sorted({m.strip() for m in upd.hidden_models if isinstance(m, str) and m.strip()})
            # Only replace the key when a non-empty new value is provided.
            if upd.api_key:
                prov["api_key"] = upd.api_key
            # Setting up a provider auto-enables it: a fresh install ships every
            # provider disabled, and saving a credential (an API key, or a base URL for
            # a local provider) is what "sets it up". An explicit disabled flag in the
            # same request always wins.
            if upd.disabled is None:
                credential_added = bool(upd.api_key) or (
                    name in LOCAL_PROVIDERS
                    and upd.base_url is not None
                    and upd.base_url.strip() != ""
                )
                if credential_added:
                    prov["disabled"] = False
    save_config(cfg)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.config_update",
            target=cfg.get("active_provider"),
            metadata_json={"active_provider": cfg.get("active_provider")},
        )
    )
    await db.commit()
    return public_config()


@router.get("/llm/models")
async def list_llm_models(
    provider: str,
    free_only: bool | None = None,
    include_hidden: bool = False,
    _: Principal = Depends(require_admin),
):
    """List available models for a provider. Fetches live where possible, with a
    curated fallback list. Uses the saved key for that provider.

    For OpenRouter, `free_only` (or the saved provider setting) limits the list to
    free models (ids ending in ':free').

    By default, models the admin has hidden via Settings → Manage visibility are
    filtered out (so the chat model picker doesn't show them). Pass
    ``include_hidden=true`` to see the full list (used by the admin UI itself)."""
    cfg = load_config()
    prov = cfg.get("providers", {}).get(provider, {})
    result = await _fetch_provider_models(provider, prov, free_only)
    hidden = set(prov.get("hidden_models", []) or [])
    if not include_hidden and hidden:
        result["models"] = [m for m in result.get("models", []) if m not in hidden]
    return result


async def _fetch_provider_models(
    provider: str, prov: dict[str, Any], free_only: bool | None
) -> dict[str, Any]:
    """Internal: fetch the raw model catalogue for a provider (live + fallback).
    Caller is responsible for applying the per-provider hidden-models filter."""
    base_url = (prov.get("base_url") or "").strip()
    if base_url:
        from app.core.ssrf import check_url

        blocked = check_url(base_url, allow_private=provider in LOCAL_PROVIDERS)
        if blocked:
            return {"models": [], "error": blocked}
    api_key = prov.get("api_key", "")

    if provider in ("openai", "openai_eu"):
        models_url = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/models"
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        models_url,
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                if r.status_code == 200:
                    ids = sorted(
                        m["id"]
                        for m in r.json().get("data", [])
                        if m.get("id", "").startswith(("gpt-", "o1", "o3", "o4"))
                    )
                    if ids:
                        return {"models": ids}
            except Exception:  # noqa: BLE001 - fall back to curated list
                pass
        return {"models": OPENAI_FALLBACK_MODELS}

    if provider == "github":
        base = prov.get("base_url") or "https://models.github.ai"
        catalog = base.replace("/inference", "") + "/catalog/models"
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        catalog, headers={"Authorization": f"Bearer {api_key}"}
                    )
                if r.status_code == 200:
                    data = r.json()
                    items = data if isinstance(data, list) else data.get("models", [])
                    ids = sorted(
                        {m.get("id") or m.get("name") for m in items if (m.get("id") or m.get("name"))}
                    )
                    if ids:
                        return {"models": ids}
            except Exception:  # noqa: BLE001 - fall back to curated list
                pass
        return {"models": GITHUB_FALLBACK_MODELS}

    if provider == "github_copilot":
        # Fetch the account's live, selectable Copilot models (same set the GitHub
        # Copilot model picker shows). Falls back to a curated list if not signed in.
        try:
            from app.agent import github_copilot as _ghc

            ids = await _ghc.list_models()
            if ids:
                return {"models": ids}
        except Exception:  # noqa: BLE001 - fall back to curated list
            pass
        return {"models": GITHUB_COPILOT_FALLBACK_MODELS}

    if provider == "ollama":
        base = prov.get("base_url") or OLLAMA_BASE_URL
        # Native Ollama tags endpoint lists locally pulled models.
        tags_url = base.replace("/v1", "") + "/api/tags"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(tags_url)
            if r.status_code == 200:
                ids = sorted(m.get("name", "") for m in r.json().get("models", []))
                ids = [m for m in ids if m]
                if ids:
                    return {"models": ids}
        except Exception:  # noqa: BLE001 - Ollama may not be running
            pass
        return {"models": OLLAMA_FALLBACK_MODELS}

    if provider == "chatgpt":
        return {"models": CHATGPT_FALLBACK_MODELS}

    if provider in ("claude", "claude_oauth"):
        return {"models": CLAUDE_FALLBACK_MODELS}

    if provider == "azure_openai":
        # List the resource's live deployments (the names usable as the "model").
        base = (prov.get("base_url") or "").rstrip("/")
        if base and api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"{base}/openai/deployments?api-version=2023-03-15-preview",
                        headers={"api-key": api_key},
                    )
                if r.status_code == 200:
                    ids = sorted(
                        {
                            d.get("id") or d.get("model")
                            for d in r.json().get("data", [])
                            if (d.get("id") or d.get("model"))
                        }
                    )
                    if ids:
                        return {"models": ids}
            except Exception:  # noqa: BLE001 - no usable list endpoint; user types the deployment
                pass
        return {"models": []}

    if provider == "azure_foundry":
        # Azure AI Foundry (…services.ai.azure.com/models) has no standard model-list
        # endpoint, but the SAME resource's sibling …openai.azure.com host exposes the
        # deployments list. Best-effort: derive it and list the deployed models.
        base = (prov.get("base_url") or "").rstrip("/")
        list_host = (
            base.replace("/models", "").replace(".services.ai.azure.com", ".openai.azure.com")
        )
        if list_host and api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"{list_host}/openai/deployments?api-version=2023-03-15-preview",
                        headers={"api-key": api_key},
                    )
                if r.status_code == 200:
                    ids = sorted(
                        {
                            d.get("id") or d.get("model")
                            for d in r.json().get("data", [])
                            if (d.get("id") or d.get("model"))
                        }
                    )
                    if ids:
                        return {"models": ids}
            except Exception:  # noqa: BLE001 - no list endpoint; user types the model name
                pass
        return {"models": []}

    # OpenAI-compatible third parties: try a live /models fetch, else curated list.
    _compat = {
        "grok": GROK_FALLBACK_MODELS,
        "mistral": MISTRAL_FALLBACK_MODELS,
        "gemini": GEMINI_FALLBACK_MODELS,
        "openrouter": OPENROUTER_FALLBACK_MODELS,
        "lmstudio": LMSTUDIO_FALLBACK_MODELS,
    }
    if provider in _compat:
        # OpenRouter free-only: explicit query param overrides the saved setting.
        want_free = (
            free_only
            if free_only is not None
            else bool(prov.get("free_only"))
        ) and provider == "openrouter"

        def _filter_free(ids: list[str]) -> list[str]:
            if not want_free:
                return ids
            free = [m for m in ids if m.endswith(":free")]
            return free or ids  # don't show an empty list if none match

        base = (prov.get("base_url") or "").rstrip("/")
        if base:
            try:
                headers = {}
                if api_key and provider != "lmstudio":
                    headers["Authorization"] = f"Bearer {api_key}"
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(f"{base}/models", headers=headers)
                if r.status_code == 200:
                    ids = sorted(
                        {m.get("id") for m in r.json().get("data", []) if m.get("id")}
                    )
                    ids = _filter_free(ids)
                    if ids:
                        return {"models": ids}
            except Exception:  # noqa: BLE001 - fall back to curated list
                pass
        return {"models": _filter_free(_compat[provider])}

    return {"models": []}


class ProviderTestRequest(BaseModel):
    provider: str


@router.post("/llm/test")
async def test_llm_provider(
    payload: ProviderTestRequest,
    _: Principal = Depends(require_admin),
):
    """Make a tiny live call to verify a provider's SAVED credentials actually work.

    Returns {ok, detail}. Always tests the persisted configuration (save first, then
    test) — so a bad just-typed key can never be silently exercised, and the test
    reflects exactly what the chat will use.
    """
    from app.agent.factory import build_provider_for

    provider = (payload.provider or "").lower().strip()
    cfg = load_config()
    if provider not in cfg.get("providers", {}):
        raise HTTPException(status_code=400, detail="Unknown provider")

    try:
        prov = build_provider_for(provider, None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Could not initialize provider: {exc}"}

    try:
        got_text = False
        async for ev in prov.stream(
            [{"role": "user", "content": "Reply with the single word: pong"}],
            None,
        ):
            if getattr(ev, "type", "") == "token" and getattr(ev, "text", ""):
                got_text = True
                break
            if getattr(ev, "type", "") == "error":
                return {"ok": False, "detail": getattr(ev, "text", "Provider returned an error")}
        if got_text:
            return {"ok": True, "detail": "Provider responded successfully."}
        return {"ok": False, "detail": "Provider returned no output."}
    except Exception as exc:  # noqa: BLE001
        # Surface the cleanest line of the error (e.g. the 401 message) to the UI.
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return {"ok": False, "detail": msg}


# --- Staged connection diagnostics (SSE) ----------------------------------------------
# Emits a sequence of step events so the admin sees what's happening at each phase:
#   1. config     — load + validate saved provider config
#   2. endpoint   — resolve the API endpoint URL & DNS-resolve the host
#   3. connect    — TCP/TLS connect to the host:port
#   4. auth       — verify credentials (OAuth token / API key / no-auth ack)
#   5. request    — send a 1-token chat completion ("pong")
#   6. first_token— time-to-first-token (TTFB)
#   7. complete   — overall verdict + total elapsed
# Each event payload: {step, status: "ok"|"error"|"warn"|"skip", title, detail, ms}.


def _ms_since(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


def _ev(step: str, status: str, title: str, detail: str = "", ms: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"step": step, "status": status, "title": title, "detail": detail}
    if ms is not None:
        payload["ms"] = ms
    return {"event": "step", "data": json.dumps(payload)}


async def _diagnose_provider(provider: str):
    """Run the staged diagnostics for `provider`, yielding SSE events per phase."""
    cfg = load_config()
    providers = cfg.get("providers", {}) or {}
    if provider not in providers:
        yield _ev("config", "error", "Provider not configured", f"Unknown provider '{provider}'.", 0)
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Unknown provider"})}
        return

    overall_t0 = time.perf_counter()

    # ---- 1. Config validation ----------------------------------------------------
    t0 = time.perf_counter()
    # Apply same defaults used at runtime.
    from app.core.llm_config import get_active

    resolved = get_active(provider, None)
    eff_base = (resolved.get("base_url") or "").strip()
    eff_model = (resolved.get("model") or "").strip()
    eff_key = (resolved.get("api_key") or "").strip()

    missing: list[str] = []
    if not eff_model:
        missing.append("model")
    # Key requirements vary per provider. OAuth/local providers carry no api_key.
    needs_key = provider not in ("ollama", "lmstudio", "chatgpt", "github_copilot", "claude_oauth")
    if needs_key and not eff_key:
        missing.append("api_key")
    if provider in ("openai_eu", "azure_openai", "github", "ollama", "chatgpt", "claude",
                    "grok", "mistral", "gemini", "openrouter", "lmstudio", "github_copilot") and not eff_base:
        missing.append("base_url")
    if missing:
        yield _ev(
            "config", "error", "Configuration incomplete",
            f"Missing: {', '.join(missing)}. Save the form first, then test.", _ms_since(t0),
        )
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"Missing {', '.join(missing)}"})}
        return
    cfg_detail = f"model={eff_model}"
    if eff_base:
        cfg_detail += f" · endpoint={eff_base}"
    if eff_key and needs_key:
        masked = (eff_key[:4] + "…" + eff_key[-2:]) if len(eff_key) > 8 else "set"
        cfg_detail += f" · key={masked}"
    elif not needs_key:
        cfg_detail += " · auth=oauth/local"
    yield _ev("config", "ok", "Configuration loaded", cfg_detail, _ms_since(t0))

    # ---- 2. Endpoint resolution (DNS) -------------------------------------------
    t0 = time.perf_counter()
    # For plain OpenAI the saved base_url may be empty (uses the SDK default).
    probe_base = eff_base or ("https://api.openai.com/v1" if provider == "openai" else "")
    parsed = urlparse(probe_base) if probe_base else None
    host = parsed.hostname if parsed else ""
    scheme = (parsed.scheme if parsed else "https") or "https"
    port = (parsed.port if parsed else None) or (443 if scheme == "https" else 80)
    if not host:
        yield _ev("endpoint", "error", "Endpoint URL invalid", f"Could not parse host from '{probe_base}'.", _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Invalid endpoint URL"})}
        return
    from app.core.ssrf import check_url

    ssrf_blocked = check_url(probe_base, allow_private=provider in LOCAL_PROVIDERS)
    if ssrf_blocked:
        yield _ev("endpoint", "error", "Endpoint blocked", ssrf_blocked, _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": ssrf_blocked})}
        return
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        ips = sorted({i[4][0] for i in infos})
        yield _ev(
            "endpoint", "ok", f"Resolved {host}",
            f"{len(ips)} address{'es' if len(ips) != 1 else ''} · {', '.join(ips[:3])}{' …' if len(ips) > 3 else ''} · port {port}",
            _ms_since(t0),
        )
    except Exception as exc:  # noqa: BLE001
        yield _ev("endpoint", "error", f"DNS lookup failed for {host}", str(exc), _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"DNS: {exc}"})}
        return

    # ---- 3. TCP / TLS connect ---------------------------------------------------
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
            # HEAD on the base URL is enough to prove the socket + TLS handshake works.
            resp = await client.request("HEAD", probe_base, follow_redirects=False)
        ms = _ms_since(t0)
        yield _ev(
            "connect", "ok", f"Connected to {host}",
            f"{scheme.upper()} handshake complete · HTTP {resp.status_code} from HEAD probe", ms,
        )
    except httpx.ConnectError as exc:
        yield _ev("connect", "error", f"Cannot reach {host}:{port}", str(exc), _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"Connect: {exc}"})}
        return
    except httpx.ConnectTimeout:
        yield _ev("connect", "error", "Connection timed out", f"No response from {host}:{port} within 10s.", _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Connect timeout"})}
        return
    except Exception as exc:  # noqa: BLE001
        # Some hosts reject HEAD with 400/405 — that still proves we connected.
        ms = _ms_since(t0)
        yield _ev("connect", "warn", f"Reached {host}, unusual response", str(exc)[:200], ms)

    # ---- 4. Authentication ------------------------------------------------------
    t0 = time.perf_counter()
    if provider == "github_copilot":
        st = github_copilot_auth.status()
        if not st.get("has_token"):
            yield _ev("auth", "error", "Not signed in to GitHub Copilot", "Use the Sign in button above.", _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Sign-in required"})}
            return
        if st.get("expired"):
            yield _ev("auth", "warn", "Copilot token expired", "Will auto-refresh on first request.", _ms_since(t0))
        else:
            yield _ev("auth", "ok", "Copilot OAuth token valid", st.get("api_base_url", ""), _ms_since(t0))
    elif provider == "chatgpt":
        st = chatgpt_oauth.status()
        if not st.get("has_token"):
            yield _ev("auth", "error", "Not signed in to ChatGPT", "Use the Sign in button above.", _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Sign-in required"})}
            return
        if st.get("expired"):
            yield _ev("auth", "warn", "ChatGPT token expired", "Will auto-refresh on first request.", _ms_since(t0))
        else:
            yield _ev("auth", "ok", "ChatGPT OAuth token valid", f"account {st.get('account_id', '')}", _ms_since(t0))
    elif provider == "claude_oauth":
        st = claude_oauth.status()
        if not st.get("has_token"):
            yield _ev("auth", "error", "Not signed in to Claude", "Use the Sign in button above.", _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Sign-in required"})}
            return
        if st.get("expired"):
            yield _ev("auth", "warn", "Claude token expired", "Will auto-refresh on first request.", _ms_since(t0))
        else:
            yield _ev("auth", "ok", "Claude OAuth token valid", "", _ms_since(t0))
    elif provider in ("ollama", "lmstudio"):
        yield _ev("auth", "skip", "No authentication needed", "Local server — API key is ignored.", _ms_since(t0))
    else:
        # Probe a cheap, auth-only endpoint (GET /models). Works for OpenAI-compatible APIs.
        # Azure OpenAI uses a different shape — fall back to "skipped".
        probe_url = ""
        headers: dict[str, str] = {}
        if provider == "azure_openai":
            # Azure: validate the key+endpoint pair via the deployments list.
            base = eff_base.rstrip("/")
            api_version = resolved.get("api_version") or "2024-10-21"
            probe_url = f"{base}/openai/deployments?api-version={api_version}"
            headers = {"api-key": eff_key}
        elif provider == "claude":
            # Anthropic doesn't expose a cheap list endpoint without auth; HEAD /v1/messages 405s.
            # Use a tiny messages request as the actual request step instead.
            yield _ev("auth", "skip", "Auth check deferred", "Anthropic has no list endpoint — verified during request step.", _ms_since(t0))
            probe_url = ""
        elif provider == "azure_foundry":
            # Foundry's model-inference endpoint has no cheap auth-only GET; verify during
            # the request step (the SDK posts to /models/chat/completions with Bearer).
            yield _ev("auth", "skip", "Auth check deferred", "Verified during request step.", _ms_since(t0))
            probe_url = ""
        else:
            base = eff_base.rstrip("/") if eff_base else "https://api.openai.com/v1"
            probe_url = f"{base}/models"
            headers = {"Authorization": f"Bearer {eff_key}"}
        if probe_url:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.get(probe_url, headers=headers)
                ms = _ms_since(t0)
                if r.status_code == 200:
                    n = 0
                    try:
                        data = r.json()
                        items = data.get("data") if isinstance(data, dict) else data
                        if isinstance(items, list):
                            n = len(items)
                    except Exception:  # noqa: BLE001
                        pass
                    yield _ev(
                        "auth", "ok", "Credentials accepted",
                        f"HTTP 200 from /models · {n} model{'s' if n != 1 else ''} visible" if n else "HTTP 200 from /models",
                        ms,
                    )
                elif r.status_code in (401, 403):
                    detail = ""
                    try:
                        body = r.json()
                        detail = (body.get("error", {}) or {}).get("message", "") or str(body)[:200]
                    except Exception:  # noqa: BLE001
                        detail = (r.text or "")[:200]
                    yield _ev("auth", "error", f"Authentication rejected (HTTP {r.status_code})", detail, ms)
                    yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"Auth failed: HTTP {r.status_code}"})}
                    return
                else:
                    yield _ev(
                        "auth", "warn", f"Unexpected status (HTTP {r.status_code})",
                        "Will still try a request to confirm.", ms,
                    )
            except Exception as exc:  # noqa: BLE001
                yield _ev("auth", "warn", "Auth probe inconclusive", f"{exc} — will still try a request.", _ms_since(t0))

    # ---- 5/6/7. Request → first token → complete --------------------------------
    t0 = time.perf_counter()
    try:
        from app.agent.factory import build_provider_for

        prov_obj = build_provider_for(provider, None)
    except Exception as exc:  # noqa: BLE001
        yield _ev("request", "error", "Could not initialize provider", str(exc), _ms_since(t0))
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": str(exc)})}
        return

    request_emitted = False
    first_token_t0 = time.perf_counter()
    got_text = False
    last_error: str | None = None
    try:
        # Mark "request sent" as soon as we begin streaming.
        yield _ev("request", "ok", "Probe request sent", f"prompt='Reply with: pong' · model={eff_model}", _ms_since(t0))
        request_emitted = True
        first_token_t0 = time.perf_counter()
        async for evx in prov_obj.stream(
            [{"role": "user", "content": "Reply with the single word: pong"}],
            None,
        ):
            etype = getattr(evx, "type", "")
            if etype == "token" and getattr(evx, "text", ""):
                got_text = True
                ttfb = _ms_since(first_token_t0)
                yield _ev("first_token", "ok", "First token received", f"TTFB {ttfb} ms", ttfb)
                break
            if etype == "error":
                last_error = getattr(evx, "text", "") or "Provider returned an error"
                break
    except Exception as exc:  # noqa: BLE001
        last_error = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__

    if not request_emitted:
        yield _ev("request", "error", "Probe request failed to send", last_error or "Unknown error", _ms_since(t0))
    if last_error and not got_text:
        yield _ev("first_token", "error", "No tokens received", last_error, _ms_since(first_token_t0))
        total_ms = _ms_since(overall_t0)
        yield _ev("complete", "error", "Test failed", last_error, total_ms)
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": last_error})}
        return
    if not got_text:
        yield _ev("first_token", "error", "No tokens received", "Provider returned an empty stream.", _ms_since(first_token_t0))
        total_ms = _ms_since(overall_t0)
        yield _ev("complete", "error", "Test failed", "Provider returned no output.", total_ms)
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Provider returned no output."})}
        return

    total_ms = _ms_since(overall_t0)
    yield _ev("complete", "ok", "All checks passed", f"End-to-end {total_ms} ms · provider is ready.", total_ms)
    yield {"event": "done", "data": json.dumps({"ok": True, "detail": f"Provider responded in {total_ms} ms"})}


@router.post("/llm/test/stream")
async def test_llm_provider_stream(
    payload: ProviderTestRequest,
    _: Principal = Depends(require_admin),
):
    """Staged SSE diagnostics for a provider — emits one event per phase (config,
    endpoint/DNS, connect, auth, request, first-token, complete) so the admin can
    see exactly where a connection fails."""
    provider = (payload.provider or "").lower().strip()

    async def _gen():
        try:
            async for ev in _diagnose_provider(provider):
                yield ev
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


# --- Staged "Refresh models" diagnostics (SSE) ----------------------------------------
# Same shape as the Test connection stream, but the terminal phase fetches the model
# catalogue from the provider so the admin sees where the list came from and how many
# models were returned. The final `done` payload includes `models: [...]` so the UI can
# update the model dropdown with the freshly-fetched list.


class ProviderRefreshRequest(BaseModel):
    provider: str
    free_only: bool | None = None


async def _diagnose_models(provider: str, free_only: bool | None):
    cfg = load_config()
    if provider not in cfg.get("providers", {}):
        yield _ev("config", "error", "Provider not configured", f"Unknown '{provider}'.", 0)
        yield {
            "event": "done",
            "data": json.dumps({"ok": False, "detail": "Unknown provider", "models": []}),
        }
        return

    overall_t0 = time.perf_counter()

    # ---- 1. Configuration -------------------------------------------------------
    t0 = time.perf_counter()
    from app.core.llm_config import get_active

    resolved = get_active(provider, None)
    eff_base = (resolved.get("base_url") or "").strip()
    eff_key = (resolved.get("api_key") or "").strip()
    needs_key = provider not in ("ollama", "lmstudio", "chatgpt", "github_copilot", "claude_oauth")
    key_part = ""
    if eff_key and needs_key:
        masked = (eff_key[:4] + "…" + eff_key[-2:]) if len(eff_key) > 8 else "set"
        key_part = f" · key={masked}"
    elif not needs_key:
        key_part = " · auth=oauth/local"
    yield _ev(
        "config", "ok", "Configuration loaded",
        f"endpoint={eff_base or '(provider default)'}{key_part}",
        _ms_since(t0),
    )

    # Providers without a remote /models endpoint serve a bundled fallback list. Skip
    # the network probes for those so we don't show fake "warn" steps.
    fallback_only = provider in ("chatgpt", "claude")
    probe_base = eff_base or ("https://api.openai.com/v1" if provider == "openai" else "")

    if fallback_only or not probe_base:
        yield _ev("endpoint", "skip", "No remote model endpoint", "Provider uses a bundled fallback model list.", 0)
        yield _ev("connect", "skip", "No connect needed", "", 0)
    else:
        # ---- 2. Endpoint resolution (DNS) ---------------------------------------
        t0 = time.perf_counter()
        parsed = urlparse(probe_base)
        host = parsed.hostname or ""
        scheme = (parsed.scheme or "https") or "https"
        port = parsed.port or (443 if scheme == "https" else 80)
        if not host:
            yield _ev("endpoint", "error", "Endpoint URL invalid", f"Could not parse host from '{probe_base}'.", _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Invalid endpoint URL", "models": []})}
            return
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            ips = sorted({i[4][0] for i in infos})
            yield _ev(
                "endpoint", "ok", f"Resolved {host}",
                f"{len(ips)} address{'es' if len(ips) != 1 else ''} · {', '.join(ips[:3])}{' …' if len(ips) > 3 else ''} · port {port}",
                _ms_since(t0),
            )
        except Exception as exc:  # noqa: BLE001
            yield _ev("endpoint", "error", f"DNS lookup failed for {host}", str(exc), _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"DNS: {exc}", "models": []})}
            return

        # ---- 3. TCP / TLS connect -----------------------------------------------
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                resp = await client.request("HEAD", probe_base, follow_redirects=False)
            yield _ev(
                "connect", "ok", f"Connected to {host}",
                f"{scheme.upper()} handshake complete · HTTP {resp.status_code} from HEAD probe",
                _ms_since(t0),
            )
        except httpx.ConnectError as exc:
            yield _ev("connect", "error", f"Cannot reach {host}:{port}", str(exc), _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": f"Connect: {exc}", "models": []})}
            return
        except httpx.ConnectTimeout:
            yield _ev("connect", "error", "Connection timed out", f"No response from {host}:{port} within 10s.", _ms_since(t0))
            yield {"event": "done", "data": json.dumps({"ok": False, "detail": "Connect timeout", "models": []})}
            return
        except Exception as exc:  # noqa: BLE001
            yield _ev("connect", "warn", f"Reached {host}, unusual response", str(exc)[:200], _ms_since(t0))

    # ---- 4. Fetch model catalogue ----------------------------------------------
    t0 = time.perf_counter()
    ids: list[str] = []
    fetch_err: str | None = None
    try:
        # Always fetch the full catalogue here (include hidden) — the admin needs to
        # see every model to manage visibility; chat-picker filtering happens elsewhere.
        prov_cfg = load_config().get("providers", {}).get(provider, {})
        res = await _fetch_provider_models(provider, prov_cfg, free_only)
        ids = list(res.get("models", []))
    except Exception as exc:  # noqa: BLE001
        fetch_err = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    fetch_ms = _ms_since(t0)

    if fetch_err:
        yield _ev("fetch", "error", "Model fetch failed", fetch_err, fetch_ms)
        total_ms = _ms_since(overall_t0)
        yield _ev("complete", "error", "Refresh failed", fetch_err, total_ms)
        yield {"event": "done", "data": json.dumps({"ok": False, "detail": fetch_err, "models": []})}
        return

    sample = ", ".join(ids[:4]) + (" …" if len(ids) > 4 else "")
    yield _ev(
        "fetch",
        "ok" if ids else "warn",
        f"Fetched {len(ids)} model{'s' if len(ids) != 1 else ''}",
        sample or "(provider returned no models)",
        fetch_ms,
    )

    total_ms = _ms_since(overall_t0)
    yield _ev(
        "complete",
        "ok" if ids else "warn",
        "Refresh complete" if ids else "Refresh returned no models",
        f"End-to-end {total_ms} ms",
        total_ms,
    )
    yield {
        "event": "done",
        "data": json.dumps(
            {
                "ok": bool(ids),
                "detail": f"Fetched {len(ids)} models in {total_ms} ms",
                "models": ids,
            }
        ),
    }


@router.post("/llm/models/stream")
async def refresh_llm_models_stream(
    payload: ProviderRefreshRequest,
    _: Principal = Depends(require_admin),
):
    """Staged SSE for the 'Refresh models' button — emits per-phase events plus a
    final `models` list so the admin sees what was fetched and from where."""
    provider = (payload.provider or "").lower().strip()
    free_only = payload.free_only

    async def _gen():
        try:
            async for ev in _diagnose_models(provider, free_only):
                yield ev
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


@router.get("/llm/oauth/chatgpt/status")
async def chatgpt_oauth_status(_: Principal = Depends(require_admin)):
    """Report ChatGPT (Codex) sign-in / token status for the admin UI."""
    return chatgpt_oauth.status()


@router.post("/llm/oauth/chatgpt/refresh")
async def chatgpt_oauth_refresh(_: Principal = Depends(require_admin)):
    """Force a ChatGPT token refresh using the Codex CLI refresh token."""
    try:
        st = await chatgpt_oauth.force_refresh()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "status": st}


@router.post("/llm/oauth/chatgpt/authorize-url")
async def chatgpt_oauth_authorize_url(_: Principal = Depends(require_admin)):
    """Start a sign-in and return the OpenAI authorize URL to open in any browser.

    The admin opens the link, signs in, and is redirected to a localhost callback URL
    (which won't load). They copy that final URL and POST it to '/complete'. This works
    even when the app is not hosted on localhost (prod)."""
    try:
        return {"ok": True, **chatgpt_oauth.build_authorize_url()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ChatgptCallbackBody(BaseModel):
    callback_url: str


@router.post("/llm/oauth/chatgpt/complete")
async def chatgpt_oauth_complete(
    body: ChatgptCallbackBody,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Finish a paste-URL sign-in: exchange the code from the pasted redirect URL."""
    try:
        st = await chatgpt_oauth.complete_with_callback_url(body.callback_url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Signing in sets up the provider — make it available in the model picker.
    set_provider_enabled("chatgpt", True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.chatgpt_oauth_complete",
            target="chatgpt",
        )
    )
    await db.commit()
    return {"ok": True, "status": st}


@router.get("/llm/oauth/claude/status")
async def claude_oauth_status(_: Principal = Depends(require_admin)):
    """Report Claude (Pro/Max) sign-in / token status for the admin UI."""
    return claude_oauth.status()


@router.post("/llm/oauth/claude/refresh")
async def claude_oauth_refresh(_: Principal = Depends(require_admin)):
    """Force a Claude OAuth token refresh using the stored refresh token."""
    try:
        st = await claude_oauth.force_refresh()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "status": st}


@router.post("/llm/oauth/claude/authorize-url")
async def claude_oauth_authorize_url(_: Principal = Depends(require_admin)):
    """Start a sign-in and return the Claude authorize URL to open in any browser.

    The admin opens the link, signs in, and is shown a code on the console callback page.
    They copy that code (shown as 'code#state') and POST it to '/complete'. This works
    even when the app is not hosted on localhost (prod)."""
    try:
        return {"ok": True, **claude_oauth.build_authorize_url()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ClaudeCallbackBody(BaseModel):
    callback_url: str


@router.post("/llm/oauth/claude/complete")
async def claude_oauth_complete(
    body: ClaudeCallbackBody,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Finish a paste-code sign-in: exchange the code shown on the Claude callback page."""
    try:
        st = await claude_oauth.complete_with_callback_url(body.callback_url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Signing in sets up the provider — make it available in the model picker.
    set_provider_enabled("claude_oauth", True)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.claude_oauth_complete",
            target="claude_oauth",
        )
    )
    await db.commit()
    return {"ok": True, "status": st}


@router.post("/llm/oauth/claude/signout")
async def claude_oauth_signout(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sign out of Claude: forget the stored tokens and disable the provider."""
    st = claude_oauth.sign_out()
    set_provider_enabled("claude_oauth", False)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.claude_oauth_signout",
            target="claude_oauth",
        )
    )
    await db.commit()
    return {"ok": True, "status": st}


@router.post("/llm/oauth/chatgpt/signout")
async def chatgpt_oauth_signout(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Forget the stored ChatGPT tokens and browser session."""
    st = chatgpt_oauth.sign_out()
    # Signing out tears down the provider — hide it again until re-configured.
    set_provider_enabled("chatgpt", False)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.chatgpt_oauth_signout",
            target="chatgpt",
        )
    )
    await db.commit()
    return {"ok": True, "status": st}


@router.post("/llm/oauth/github/device/start")
async def github_copilot_device_start(_: Principal = Depends(require_admin)):
    """Begin the GitHub OAuth device flow (headless, remote-friendly sign-in).

    Returns a short user code + verification URL. The user opens the URL on any device,
    enters the code, then the UI polls /device/poll until a Copilot token is minted. No
    server-side browser is involved, so this works in a headless container."""
    try:
        return {"ok": True, **(await github_copilot_auth.start_device_flow())}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm/oauth/github/device/poll")
async def github_copilot_device_poll(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Poll the in-progress GitHub device flow once. See start for the full flow."""
    try:
        result = await github_copilot_auth.poll_device_flow()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("status") == "authorized":
        # Signing in sets up the provider — make it available in the model picker.
        set_provider_enabled("github_copilot", True)
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="llm.github_copilot_login",
                target="github_copilot",
                metadata_json={"api_base_url": result.get("api_base_url", ""), "method": "device_flow"},
            )
        )
        await db.commit()
        return {"ok": True, "status": result, "config": public_config()}
    return {"ok": True, "status": result}


@router.post("/llm/oauth/github/refresh")
async def github_copilot_refresh(_: Principal = Depends(require_admin)):
    """Headlessly re-sniff a fresh token using the persisted browser session."""
    token = await github_copilot_auth.refresh_token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Could not refresh — the browser session has expired. Sign in again.",
        )
    return {"ok": True, "status": github_copilot_auth.status()}


@router.post("/llm/oauth/github/signout")
async def github_copilot_signout(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Forget the cached token and browser session."""
    github_copilot_auth.sign_out()
    # Signing out tears down the provider — hide it again until re-configured.
    set_provider_enabled("github_copilot", False)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="llm.github_copilot_signout",
            target="github_copilot",
        )
    )
    await db.commit()
    return {"ok": True, "status": github_copilot_auth.status()}


@router.get("/llm/oauth/github/status")
async def github_copilot_status(_: Principal = Depends(require_admin)):
    """Report GitHub Copilot sign-in / token status for the admin UI."""
    return github_copilot_auth.status()


@router.get("/mcp/tools")
async def list_mcp_tools(_: Principal = Depends(require_admin)):
    """Discover the tools exposed by the Azure MCP server, with read/write class."""
    client = build_mcp_client(settings)
    try:
        tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"MCP unavailable: {exc}") from exc
    finally:
        client.close()
    return [
        {"name": t.name, "description": t.description, "kind": t.kind} for t in tools
    ]


@router.get("/builtin/tools")
async def list_builtin_tools(_: Principal = Depends(require_admin)):
    """The first-party built-in utility tools (web fetch + network diagnostics) and
    whether they're enabled for the agent."""
    from app.agent.builtins import builtin_tool_catalog
    from app.core.app_settings import load_settings

    s = load_settings()
    enabled = bool(s.get("builtin_tools_enabled", True))
    disabled = set(s.get("builtin_tools_disabled") or [])
    return {
        "enabled": enabled,
        "disabled": sorted(disabled),
        "egress_denylist": s.get("network_egress_denylist") or [],
        "egress_allowlist": s.get("network_egress_allowlist") or [],
        "tools": [
            {**t, "active": enabled and t["name"] not in disabled}
            for t in builtin_tool_catalog()
        ],
    }


@router.get("/entra/tools")
async def list_entra_tools(_: Principal = Depends(require_admin)):
    """Discover the tools exposed by the EntraID (Microsoft Graph) MCP server, with
    read/write class. Authenticates with the default Azure connection's service
    principal (the same identity the agent uses for directory queries)."""
    from app.core.app_settings import load_settings
    from app.core.azure_connections import get_default_connection
    from app.mcp.client import build_entra_mcp_client

    connection = get_default_connection()
    client = build_entra_mcp_client(settings, connection=connection)
    try:
        tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"EntraID MCP unavailable: {exc}") from exc
    finally:
        client.close()
    enabled = bool(load_settings().get("entra_mcp_enabled", False))
    return {
        "enabled": enabled,
        "connection_configured": connection is not None,
        "tools": [
            {"name": t.name, "description": t.description, "kind": t.kind} for t in tools
        ],
    }


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Approval)
        .where(
            Approval.tenant_id == principal.tenant_id,
            Approval.decision == "pending",
        )
        .order_by(Approval.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/approvals/{approval_id}", response_model=ApprovalOut)
async def decide_approval(
    approval_id: str,
    decision: ApprovalDecision,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    approval = await db.get(Approval, approval_id)
    if approval is None or approval.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    if decision.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid decision")

    approval.decision = decision.decision
    approval.reason = decision.reason
    approval.approver_id = principal.subject
    approval.decided_at = datetime.now(timezone.utc)

    tc = await db.get(ToolCall, approval.tool_call_id)
    if tc is not None:
        tc.status = "approved" if decision.decision == "approved" else "rejected"
        tc.approved_by = principal.subject

    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=f"approval.{decision.decision}",
            target=approval.tool_call_id,
            metadata_json={"reason": decision.reason},
        )
    )
    await db.commit()
    await db.refresh(approval)
    return approval


@router.get("/tool-calls", response_model=list[ToolCallOut])
async def list_tool_calls(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    result = await db.execute(
        select(ToolCall)
        .where(ToolCall.tenant_id == principal.tenant_id)
        .order_by(ToolCall.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/usage")
async def usage_summary(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Usage.provider,
            Usage.model,
            func.count(Usage.id),
            func.coalesce(func.sum(Usage.prompt_tokens), 0),
            func.coalesce(func.sum(Usage.completion_tokens), 0),
        )
        .where(Usage.tenant_id == principal.tenant_id)
        .group_by(Usage.provider, Usage.model)
    )
    from app.core.pricing import estimate_cost, is_priced

    return [
        {
            "provider": row[0] or "",
            "model": row[1],
            "requests": row[2],
            "prompt_tokens": int(row[3]),
            "completion_tokens": int(row[4]),
            "cost_usd": estimate_cost(row[1], int(row[3]), int(row[4])),
            "estimated": not is_priced(row[1]),
        }
        for row in result.all()
    ]


@router.get("/audit")
async def audit_log(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 25,
    offset: int = 0,
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    total = await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.tenant_id == principal.tenant_id)
    )
    total_count = int(total.scalar() or 0)

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == principal.tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        {
            "id": a.id,
            "actor_id": a.actor_id,
            "action": a.action,
            "target": a.target,
            "provider": a.provider,
            "model": a.model,
            "metadata": a.metadata_json,
            "created_at": a.created_at,
        }
        for a in result.scalars().all()
    ]
    return {"items": items, "total": total_count, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Continuous SIEM export of the audit log (admin-only) — multiple destinations
# ---------------------------------------------------------------------------
class SiemDestinationInput(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    type: str | None = None
    endpoint: str | None = None
    token: str | None = None
    clear_token: bool | None = None
    auth_header: str | None = None
    auth_scheme: str | None = None
    splunk_index: str | None = None
    splunk_sourcetype: str | None = None
    verify_tls: bool | None = None
    batch_size: int | None = None


def _siem_audit_target(values: dict[str, Any]) -> str:
    # Never include the secret in the audit target.
    return ",".join(k for k in values.keys() if k != "token")[:512]


@router.get("/siem-export")
async def get_siem_export(_: Principal = Depends(require_admin)):
    from app.core.siem_export import list_destinations

    return list_destinations()


@router.post("/siem-export")
async def add_siem_destination(
    payload: SiemDestinationInput,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.siem_export import add_destination

    values = payload.model_dump(exclude_none=True)
    result = add_destination(values)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="siem_export.add",
            target=_siem_audit_target(values),
        )
    )
    await db.commit()
    return result


@router.put("/siem-export/{dest_id}")
async def update_siem_destination(
    dest_id: str,
    payload: SiemDestinationInput,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.siem_export import update_destination

    values = payload.model_dump(exclude_none=True)
    result = update_destination(dest_id, values)
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown destination.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="siem_export.update",
            target=f"{dest_id}:{_siem_audit_target(values)}"[:512],
        )
    )
    await db.commit()
    return result


@router.delete("/siem-export/{dest_id}")
async def delete_siem_destination(
    dest_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.siem_export import delete_destination, list_destinations

    if not delete_destination(dest_id):
        raise HTTPException(status_code=404, detail="Unknown destination.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="siem_export.delete",
            target=dest_id,
        )
    )
    await db.commit()
    return list_destinations()


@router.post("/siem-export/{dest_id}/test")
async def test_siem_destination(
    dest_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.core.siem_export import send_test_event

    result = await send_test_event(dest_id)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="siem_export.test",
            target=f"{dest_id}:" + ("ok" if result.get("ok") else f"error: {str(result.get('error'))[:180]}"),
        )
    )
    await db.commit()
    return result


@router.post("/siem-export/{dest_id}/flush")
async def flush_siem_destination(dest_id: str, _: Principal = Depends(require_admin)):
    """Force an immediate delivery of any pending audit rows for one destination."""
    from app.core.siem_export import flush_destination

    return await flush_destination(dest_id, force=True)


@router.post("/siem-export/{dest_id}/reset-cursor")
async def reset_siem_cursor(
    dest_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reset a destination's cursor so export re-sends from the earliest audit row."""
    from app.core.siem_export import list_destinations, reset_cursor

    if not reset_cursor(dest_id):
        raise HTTPException(status_code=404, detail="Unknown destination.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="siem_export.reset_cursor",
            target=dest_id,
        )
    )
    await db.commit()
    return list_destinations()


@router.get("/monitor")
async def monitor_overview(
    days: int | None = None,
    workload_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Central monitoring dashboard: one aggregated snapshot of everything happening
    in this tenant — activity volume, token usage, tool-call health, automations, and
    a recent-activity feed. All counts are tenant-scoped.

    Optional ``days`` (1-30) scopes the activity aggregations to a trailing window so the
    Stats screen can be filtered by date range; omitted = lifetime (the default dashboard).
    Optional ``workload_id`` scopes the Azure Well-Architected posture section to a single
    workload's latest assessment (the rest of the snapshot stays tenant-wide)."""
    window = None if days is None else max(1, min(30, int(days)))
    return await build_monitor_overview(
        db, principal.tenant_id, days=window, posture_workload_id=(workload_id or None)
    )


async def build_monitor_overview(
    db: AsyncSession,
    tenant_id: str,
    *,
    days: int | None = None,
    posture_workload_id: str | None = None,
) -> dict[str, Any]:
    """Build the aggregated Monitor overview snapshot (tenant-scoped).

    Extracted from the /monitor endpoint so the ``app_telemetry`` Monitor datasource can
    reuse the exact same aggregation without an HTTP round-trip.

    ``days``: when set (1-30), activity-based aggregations are scoped to the trailing
    window; current-state metrics (pending approvals, schedules, connectors, Azure
    posture, live turns) are always lifetime. ``None`` (default) = lifetime everywhere,
    preserving the original Monitor dashboard + datasource behavior.

    ``posture_workload_id``: when set, the Azure posture section reflects ONLY that
    workload's latest succeeded run (the dropdown options still list every assessed
    workload so the filter can be changed)."""
    from datetime import timedelta

    from sqlalchemy import true

    tid = tenant_id
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    # Time-window clause for the selected range (no-op when lifetime).
    since = (now - timedelta(days=days)) if days else None

    def tw(col):
        return (col >= since) if since is not None else true()

    async def scalar(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    # ---- Headline totals ----------------------------------------------------
    # Activity counts honor the selected window (``tw``); current-state counts are lifetime.
    chat_ids = select(Chat.id).where(Chat.tenant_id == tid)
    if since is not None:
        # In a windowed view, "chats" means chats ACTIVE in the period (had a message).
        total_chats = await scalar(
            select(func.count(func.distinct(Message.chat_id))).where(
                Message.chat_id.in_(chat_ids), tw(Message.created_at)
            )
        )
    else:
        total_chats = await scalar(
            select(func.count(Chat.id)).where(Chat.tenant_id == tid)
        )
    total_messages = await scalar(
        select(func.count(Message.id)).where(Message.chat_id.in_(chat_ids), tw(Message.created_at))
    )
    total_tool_calls = await scalar(
        select(func.count(ToolCall.id)).where(ToolCall.tenant_id == tid, tw(ToolCall.created_at))
    )
    tool_calls_24h = await scalar(
        select(func.count(ToolCall.id)).where(
            ToolCall.tenant_id == tid, ToolCall.created_at >= since_24h
        )
    )
    messages_24h = await scalar(
        select(func.count(Message.id)).where(
            Message.chat_id.in_(chat_ids), Message.created_at >= since_24h
        )
    )
    pending_approvals = await scalar(
        select(func.count(Approval.id)).where(
            Approval.tenant_id == tid, Approval.decision == "pending"
        )
    )
    total_runs = await scalar(
        select(func.count(TaskRun.id)).where(TaskRun.tenant_id == tid, tw(TaskRun.started_at))
    )
    active_schedules = await scalar(
        select(func.count(ScheduledTask.id)).where(
            ScheduledTask.tenant_id == tid,
            ScheduledTask.status == "on",
            ScheduledTask.deleted_at.is_(None),
        )
    )
    total_schedules = await scalar(
        select(func.count(ScheduledTask.id)).where(
            ScheduledTask.tenant_id == tid, ScheduledTask.deleted_at.is_(None)
        )
    )

    # Registry-backed counts (best-effort; never block the dashboard).
    try:
        from app.automations.agents import list_agents

        agent_count = len(list_agents())
    except Exception:  # noqa: BLE001
        agent_count = 0
    try:
        from app.connectors.registry import list_connectors

        connectors = list_connectors()
        connector_count = len(connectors)
        connector_ok = sum(1 for c in connectors if c.get("status") == "ok")
    except Exception:  # noqa: BLE001
        connector_count = 0
        connector_ok = 0

    # ---- Token usage --------------------------------------------------------
    usage_rows = (
        await db.execute(
            select(
                Usage.model,
                func.count(Usage.id),
                func.coalesce(func.sum(Usage.prompt_tokens), 0),
                func.coalesce(func.sum(Usage.completion_tokens), 0),
            )
            .where(Usage.tenant_id == tid, tw(Usage.created_at))
            .group_by(Usage.model)
        )
    ).all()
    from app.core.pricing import estimate_cost as _estimate_cost

    by_model = sorted(
        (
            {
                "model": r[0],
                "requests": int(r[1]),
                "prompt": int(r[2]),
                "completion": int(r[3]),
                "total": int(r[2]) + int(r[3]),
                "cost_usd": _estimate_cost(r[0], int(r[2]), int(r[3])),
            }
            for r in usage_rows
        ),
        key=lambda m: m["total"],
        reverse=True,
    )
    tokens = {
        "prompt": sum(m["prompt"] for m in by_model),
        "completion": sum(m["completion"] for m in by_model),
        "total": sum(m["total"] for m in by_model),
        "requests": sum(m["requests"] for m in by_model),
        "cost_usd": round(sum(m["cost_usd"] for m in by_model), 4),
        "by_model": by_model[:6],
    }

    # ---- Tool-call health ---------------------------------------------------
    status_rows = (
        await db.execute(
            select(ToolCall.status, func.count(ToolCall.id))
            .where(ToolCall.tenant_id == tid, tw(ToolCall.created_at))
            .group_by(ToolCall.status)
        )
    ).all()
    by_status = {row[0]: int(row[1]) for row in status_rows}
    kind_rows = (
        await db.execute(
            select(ToolCall.kind, func.count(ToolCall.id))
            .where(ToolCall.tenant_id == tid, tw(ToolCall.created_at))
            .group_by(ToolCall.kind)
        )
    ).all()
    by_kind = {row[0]: int(row[1]) for row in kind_rows}
    top_tool_rows = (
        await db.execute(
            select(ToolCall.tool_name, func.count(ToolCall.id))
            .where(ToolCall.tenant_id == tid, tw(ToolCall.created_at))
            .group_by(ToolCall.tool_name)
            .order_by(func.count(ToolCall.id).desc())
            .limit(8)
        )
    ).all()
    top_tools = [{"name": r[0], "count": int(r[1])} for r in top_tool_rows]
    failed_recent = [
        {
            "tool_name": t.tool_name,
            "status": t.status,
            "kind": t.kind,
            "chat_id": t.chat_id,
            "created_at": t.created_at,
        }
        for t in (
            await db.execute(
                select(ToolCall)
                .where(ToolCall.tenant_id == tid, ToolCall.status == "failed", tw(ToolCall.created_at))
                .order_by(ToolCall.created_at.desc())
                .limit(6)
            )
        ).scalars().all()
    ]
    tool_calls = {
        "by_status": by_status,
        "by_kind": by_kind,
        "top_tools": top_tools,
        "failed_recent": failed_recent,
        "succeeded": int(by_status.get("succeeded", 0)),
        "failed": int(by_status.get("failed", 0)),
    }

    # ---- Activity over the last 14 days (UTC day buckets) -------------------
    start_14d = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)

    async def per_day(date_col, where_clause) -> dict[str, int]:
        bucket = _day_bucket(date_col)
        rows = (
            await db.execute(
                select(bucket, func.count())
                .where(where_clause)
                .group_by(bucket)
            )
        ).all()
        return {r[0]: int(r[1]) for r in rows if r[0]}

    msgs_by_day = await per_day(
        Message.created_at,
        (Message.chat_id.in_(chat_ids)) & (Message.created_at >= start_14d),
    )
    tools_by_day = await per_day(
        ToolCall.created_at,
        (ToolCall.tenant_id == tid) & (ToolCall.created_at >= start_14d),
    )
    runs_by_day = await per_day(
        TaskRun.started_at,
        (TaskRun.tenant_id == tid) & (TaskRun.started_at >= start_14d),
    )
    activity_14d = []
    for i in range(14):
        d = (start_14d + timedelta(days=i)).strftime("%Y-%m-%d")
        activity_14d.append(
            {
                "date": d,
                "messages": msgs_by_day.get(d, 0),
                "tool_calls": tools_by_day.get(d, 0),
                "runs": runs_by_day.get(d, 0),
            }
        )

    # ---- Range activity series (driven by the selected window) -------------
    # Adaptive granularity: hourly buckets for short ranges (≤2 days) so the line has
    # detail, daily buckets otherwise. Length follows the window (default 14 daily when
    # lifetime, so the Stats screen always has a series even with no ?days=).
    win_days = days or 14
    use_hourly = win_days <= 2

    async def per_hour_full(date_col, where_clause) -> dict[str, int]:
        bucket = _hour_bucket(date_col)
        rows = (
            await db.execute(select(bucket, func.count()).where(where_clause).group_by(bucket))
        ).all()
        return {r[0]: int(r[1]) for r in rows if r[0]}

    activity_range: list[dict[str, Any]] = []
    if use_hourly:
        n_hours = win_days * 24
        start_r = (now - timedelta(hours=n_hours - 1)).replace(minute=0, second=0, microsecond=0)
        rmsgs = await per_hour_full(
            Message.created_at, (Message.chat_id.in_(chat_ids)) & (Message.created_at >= start_r)
        )
        rtools = await per_hour_full(
            ToolCall.created_at, (ToolCall.tenant_id == tid) & (ToolCall.created_at >= start_r)
        )
        rruns = await per_hour_full(
            TaskRun.started_at, (TaskRun.tenant_id == tid) & (TaskRun.started_at >= start_r)
        )
        for i in range(n_hours):
            ts = start_r + timedelta(hours=i)
            k = ts.strftime("%Y-%m-%d %H")
            activity_range.append(
                {
                    "ts": ts.isoformat(),
                    "bucket": "hour",
                    "messages": rmsgs.get(k, 0),
                    "tool_calls": rtools.get(k, 0),
                    "runs": rruns.get(k, 0),
                }
            )
    else:
        start_r = (now - timedelta(days=win_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        rmsgs = await per_day(
            Message.created_at, (Message.chat_id.in_(chat_ids)) & (Message.created_at >= start_r)
        )
        rtools = await per_day(
            ToolCall.created_at, (ToolCall.tenant_id == tid) & (ToolCall.created_at >= start_r)
        )
        rruns = await per_day(
            TaskRun.started_at, (TaskRun.tenant_id == tid) & (TaskRun.started_at >= start_r)
        )
        for i in range(win_days):
            ts = start_r + timedelta(days=i)
            k = ts.strftime("%Y-%m-%d")
            activity_range.append(
                {
                    "ts": ts.isoformat(),
                    "bucket": "day",
                    "messages": rmsgs.get(k, 0),
                    "tool_calls": rtools.get(k, 0),
                    "runs": rruns.get(k, 0),
                }
            )

    # ---- Activity punch-card: weekday × hour heat matrix over the window ----
    # Aggregated from hourly buckets (≤30×24 rows) so it's dialect-agnostic — we parse the
    # "YYYY-MM-DD HH" labels in Python into (weekday 0=Mon … 6=Sun, hour 0-23).
    heat_start = since if since is not None else (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    heat_msgs = await per_hour_full(
        Message.created_at, (Message.chat_id.in_(chat_ids)) & (Message.created_at >= heat_start)
    )
    heat_tools = await per_hour_full(
        ToolCall.created_at, (ToolCall.tenant_id == tid) & (ToolCall.created_at >= heat_start)
    )
    heat_matrix = [[0] * 24 for _ in range(7)]
    for label, cnt in list(heat_msgs.items()) + list(heat_tools.items()):
        try:
            dt = datetime.strptime(label, "%Y-%m-%d %H")
        except (ValueError, TypeError):
            continue
        heat_matrix[dt.weekday()][dt.hour] += int(cnt)
    heatmap = {"matrix": heat_matrix, "max": max((max(row) for row in heat_matrix), default=0)}

    # ---- Automations --------------------------------------------------------
    run_status_rows = (
        await db.execute(
            select(TaskRun.status, func.count(TaskRun.id))
            .where(TaskRun.tenant_id == tid, tw(TaskRun.started_at))
            .group_by(TaskRun.status)
        )
    ).all()
    runs_by_status = {row[0]: int(row[1]) for row in run_status_rows}
    recent_runs = [
        {
            "task_id": r.task_id,
            "thread_id": r.thread_id,
            "task_name": r.task_name,
            "status": r.status,
            "trigger": r.trigger,
            "summary": (r.summary or "")[:160],
            "error": (r.error or "")[:160],
            "duration_ms": r.duration_ms,
            "started_at": r.started_at,
        }
        for r in (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.tenant_id == tid, tw(TaskRun.started_at))
                .order_by(TaskRun.started_at.desc())
                .limit(8)
            )
        ).scalars().all()
    ]
    upcoming = [
        {"id": t.id, "name": t.name, "next_run_at": t.next_run_at}
        for t in (
            await db.execute(
                select(ScheduledTask)
                .where(
                    ScheduledTask.tenant_id == tid,
                    ScheduledTask.status == "on",
                    ScheduledTask.deleted_at.is_(None),
                    ScheduledTask.next_run_at.is_not(None),
                )
                .order_by(ScheduledTask.next_run_at.asc())
                .limit(5)
            )
        ).scalars().all()
    ]

    # ---- Recent activity feed (audit) --------------------------------------
    recent_activity = [
        {
            "id": a.id,
            "actor_id": a.actor_id,
            "action": a.action,
            "target": a.target,
            "provider": a.provider,
            "model": a.model,
            "chat_id": (a.metadata_json or {}).get("chat_id")
            if isinstance(a.metadata_json, dict)
            else None,
            "created_at": a.created_at,
        }
        for a in (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tid, tw(AuditLog.created_at))
                .order_by(AuditLog.created_at.desc())
                .limit(14)
            )
        ).scalars().all()
    ]

    # ---- Hourly activity (last 24h) — finer trend for the header sparkline --
    start_24h = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)

    async def per_hour(date_col, where_clause) -> dict[str, int]:
        bucket = _hour_bucket(date_col)
        rows = (
            await db.execute(
                select(bucket, func.count())
                .where(where_clause)
                .group_by(bucket)
            )
        ).all()
        return {r[0]: int(r[1]) for r in rows if r[0]}

    msgs_by_hour = await per_hour(
        Message.created_at,
        (Message.chat_id.in_(chat_ids)) & (Message.created_at >= start_24h),
    )
    tools_by_hour = await per_hour(
        ToolCall.created_at,
        (ToolCall.tenant_id == tid) & (ToolCall.created_at >= start_24h),
    )
    activity_24h = []
    for i in range(24):
        h = (start_24h + timedelta(hours=i)).strftime("%Y-%m-%d %H")
        activity_24h.append(
            {
                "hour": (start_24h + timedelta(hours=i)).isoformat(),
                "messages": msgs_by_hour.get(h, 0),
                "tool_calls": tools_by_hour.get(h, 0),
            }
        )

    # ---- Most active chats (deep-linkable) ---------------------------------
    msg_counts = (
        await db.execute(
            select(
                Message.chat_id,
                func.count(Message.id),
                func.max(Message.created_at),
            )
            .where(Message.chat_id.in_(chat_ids), tw(Message.created_at))
            .group_by(Message.chat_id)
        )
    ).all()
    tool_counts = dict(
        (
            await db.execute(
                select(ToolCall.chat_id, func.count(ToolCall.id))
                .where(ToolCall.tenant_id == tid, tw(ToolCall.created_at))
                .group_by(ToolCall.chat_id)
            )
        ).all()
    )
    chat_titles = dict(
        (
            await db.execute(
                select(Chat.id, Chat.title).where(Chat.tenant_id == tid)
            )
        ).all()
    )
    chat_rows = []
    for cid, mcount, last_at in msg_counts:
        chat_rows.append(
            {
                "id": cid,
                "title": chat_titles.get(cid, "Untitled"),
                "messages": int(mcount),
                "tool_calls": int(tool_counts.get(cid, 0)),
                "last_activity": last_at,
            }
        )
    top_chats = sorted(
        chat_rows, key=lambda c: c["messages"] + c["tool_calls"], reverse=True
    )[:6]

    # ---- Tool latency (avg ms for successful calls) ------------------------
    latency_rows = (
        await db.execute(
            select(
                ToolCall.tool_name,
                func.avg(ToolCall.duration_ms),
                func.count(ToolCall.id),
            )
            .where(
                ToolCall.tenant_id == tid,
                ToolCall.duration_ms.is_not(None),
                tw(ToolCall.created_at),
            )
            .group_by(ToolCall.tool_name)
            .order_by(func.avg(ToolCall.duration_ms).desc())
            .limit(6)
        )
    ).all()
    tool_latency = [
        {"name": r[0], "avg_ms": int(r[1] or 0), "count": int(r[2])}
        for r in latency_rows
    ]

    # ---- Provider mix (from assistant messages) ----------------------------
    provider_rows = (
        await db.execute(
            select(Message.provider, func.count(Message.id))
            .where(
                Message.chat_id.in_(chat_ids),
                Message.role == "assistant",
                Message.provider.is_not(None),
                tw(Message.created_at),
            )
            .group_by(Message.provider)
        )
    ).all()
    providers = sorted(
        ({"provider": r[0], "count": int(r[1])} for r in provider_rows if r[0]),
        key=lambda p: p["count"],
        reverse=True,
    )

    # ---- Deep investigations -----------------------------------------------
    deep_investigations = await scalar(
        select(func.count(Message.id)).where(
            Message.chat_id.in_(chat_ids), Message.investigation_json.is_not(None), tw(Message.created_at)
        )
    )

    # ---- Connectors detail (deep-linkable) ---------------------------------
    connectors_detail: list[dict[str, Any]] = []
    try:
        from app.connectors.registry import list_connectors as _lc

        for c in _lc():
            connectors_detail.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "status": c.get("status", "unknown"),
                }
            )
    except Exception:  # noqa: BLE001
        pass

    # ---- Azure posture (workloads + Well-Architected assessment health) -----
    # The latest succeeded, non-deleted assessment run per workload gives the current
    # posture: per-pillar scores, open findings by severity, and the worst controls.
    try:
        from app.workloads.registry import list_workloads as _list_workloads

        workload_total = len(_list_workloads())
    except Exception:  # noqa: BLE001
        workload_total = 0

    posture_runs = (
        await db.execute(
            select(AssessmentRun)
            .where(
                AssessmentRun.tenant_id == tid,
                AssessmentRun.status == "succeeded",
                AssessmentRun.deleted_at.is_(None),
            )
            .order_by(AssessmentRun.started_at.desc())
        )
    ).scalars().all()

    _SEV_ORDER = {"critical": 3, "error": 2, "warning": 1, "info": 0}
    latest_by_wl: dict[str, AssessmentRun] = {}
    for r in posture_runs:
        if r.workload_id not in latest_by_wl:
            latest_by_wl[r.workload_id] = r

    # Every assessed workload is offered as a filter option (before applying the filter), so
    # the Stats posture dropdown lists all workloads that have a run — even when one is selected.
    posture_workload_options = [
        {"workload_id": wid, "workload_name": run.workload_name or wid}
        for wid, run in latest_by_wl.items()
    ]
    posture_workload_options.sort(key=lambda o: (o["workload_name"] or "").lower())

    # Scope the posture aggregation to a single workload when requested.
    selected_workload_id = posture_workload_id if posture_workload_id in latest_by_wl else ""
    if selected_workload_id:
        latest_by_wl = {selected_workload_id: latest_by_wl[selected_workload_id]}

    pillar_scores: dict[str, list[int]] = {}
    findings_by_severity = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    failing_controls: dict[str, dict[str, Any]] = {}
    posture_workloads: list[dict[str, Any]] = []
    overall_scores: list[int] = []
    new_findings_total = 0
    last_assessed_at = None

    for r in latest_by_wl.values():
        if r.overall_score is not None:
            overall_scores.append(int(r.overall_score))
        for pillar, sc in (r.scores_json or {}).items():
            val = (sc or {}).get("score") if isinstance(sc, dict) else None
            if val is not None:
                pillar_scores.setdefault(pillar, []).append(int(val))
        by_sev = (r.totals_json or {}).get("by_severity", {}) if isinstance(r.totals_json, dict) else {}
        for sev, n in by_sev.items():
            if sev in findings_by_severity:
                findings_by_severity[sev] += int(n or 0)
        for f in (r.findings_json or []):
            if not isinstance(f, dict) or f.get("status") != "fail":
                continue
            key = f.get("check_id") or f.get("title") or "control"
            entry = failing_controls.setdefault(
                key,
                {
                    "title": f.get("title", key),
                    "pillar": f.get("pillar", ""),
                    "severity": f.get("severity", "warning"),
                    "count": 0,
                    "resources": 0,
                },
            )
            entry["count"] += 1
            entry["resources"] += int(f.get("flagged_count", 0) or 0)
        diff = r.diff_json if isinstance(r.diff_json, dict) else {}
        new_findings_total += len(diff.get("new_failures", []) or [])
        if last_assessed_at is None or (r.started_at and r.started_at > last_assessed_at):
            last_assessed_at = r.started_at
        posture_workloads.append(
            {
                "workload_id": r.workload_id,
                "workload_name": r.workload_name,
                "run_id": r.id,
                "overall_score": r.overall_score,
                "failed": (r.totals_json or {}).get("failed", 0) if isinstance(r.totals_json, dict) else 0,
                "severity": r.severity,
                "pillars": r.pillars or [],
                "at": r.started_at,
            }
        )

    posture_workloads.sort(
        key=lambda w: (w["overall_score"] if w["overall_score"] is not None else 999)
    )
    pillar_avgs = {
        p: round(sum(v) / len(v)) for p, v in pillar_scores.items() if v
    }
    top_failing = sorted(
        failing_controls.values(),
        key=lambda c: (_SEV_ORDER.get(c["severity"], 0), c["count"]),
        reverse=True,
    )[:6]

    azure_posture = {
        "workload_total": workload_total,
        "assessed_count": len(latest_by_wl),
        "avg_score": round(sum(overall_scores) / len(overall_scores)) if overall_scores else None,
        "pillar_avgs": pillar_avgs,
        "findings_by_severity": findings_by_severity,
        "open_findings": sum(findings_by_severity.values()),
        "new_findings": new_findings_total,
        "top_failing": top_failing,
        "workloads": posture_workloads[:8],
        "workload_options": posture_workload_options,
        "selected_workload_id": selected_workload_id,
        "last_assessed_at": last_assessed_at,
    }

    # ---- Live operations (in-flight agent turns) ---------------------------
    # The turn registry is the source of truth for what's running RIGHT NOW. Join
    # against this tenant's chats so we only surface (and title) turns the tenant owns.
    live_turns: list[dict[str, Any]] = []
    try:
        from app.agent.turn_runner import registry as _turn_registry

        snapshot = _turn_registry.live_snapshot()
        if snapshot:
            owned = (
                await db.execute(
                    select(Chat.id, Chat.title, Chat.user_id).where(
                        Chat.id.in_(list(snapshot.keys())),
                        Chat.tenant_id == tid,
                    )
                )
            ).all()
            for cid, title, uid in owned:
                meta = snapshot.get(cid) or {}
                live_turns.append(
                    {
                        "chat_id": cid,
                        "title": title or "Untitled",
                        "user_id": uid,
                        "kind": meta.get("kind", "chat"),
                        "elapsed_s": meta.get("elapsed_s", 0),
                        "current_tool": meta.get("current_tool"),
                        "tool_count": meta.get("tool_count", 0),
                        "started_at": meta.get("started_at"),
                    }
                )
            live_turns.sort(key=lambda t: t.get("elapsed_s", 0), reverse=True)
    except Exception:  # noqa: BLE001 - never let live ops break the dashboard
        live_turns = []

    return {
        "generated_at": now,
        "window": {"days": days, "since": since.isoformat() if since is not None else None},
        "totals": {
            "chats": total_chats,
            "messages": total_messages,
            "messages_24h": messages_24h,
            "tool_calls": total_tool_calls,
            "tool_calls_24h": tool_calls_24h,
            "pending_approvals": pending_approvals,
            "task_runs": total_runs,
            "active_schedules": active_schedules,
            "total_schedules": total_schedules,
            "custom_agents": agent_count,
            "connectors": connector_count,
            "connectors_ok": connector_ok,
            "deep_investigations": deep_investigations,
            "live_turns": len(live_turns),
        },
        "live_turns": live_turns,
        "tokens": tokens,
        "tool_calls": tool_calls,
        "tool_latency": tool_latency,
        "providers": providers,
        "activity_14d": activity_14d,
        "activity_24h": activity_24h,
        "activity_range": activity_range,
        "heatmap": heatmap,
        "top_chats": top_chats,
        "connectors_detail": connectors_detail,
        "azure_posture": azure_posture,
        "automations": {
            "active": active_schedules,
            "total": total_schedules,
            "runs_total": total_runs,
            "runs_by_status": runs_by_status,
            "recent_runs": recent_runs,
            "upcoming": upcoming,
        },
        "recent_activity": recent_activity,
    }


# ------------------------------------------------- Customizable Monitor dashboards
class TilePlacement(BaseModel):
    tileId: str
    x: int = 0
    y: int = 0
    w: int = 4
    h: int = 3


class MonitorDashboardUpsert(BaseModel):
    id: str | None = None
    name: str = Field(default="Untitled dashboard", max_length=200)
    description: str = Field(default="", max_length=2000)
    is_default: bool = False
    tiles: list[TilePlacement] = Field(default_factory=list)
    # Monitor 2.0: data-bound widgets + dashboard params + optional source workload.
    widgets: list[dict[str, Any]] | None = None
    params: list[dict[str, Any]] | None = None
    workload_id: str | None = None


@router.get("/monitor/dashboards")
async def list_monitor_dashboards(principal: Principal = Depends(require_admin)):
    """All saved Monitor dashboards for this tenant (default first)."""
    from app.monitor import registry as dash_registry

    return {"dashboards": dash_registry.list_dashboards(principal.tenant_id)}


@router.get("/monitor/dashboards/{dashboard_id}")
async def get_monitor_dashboard(dashboard_id: str, _: Principal = Depends(require_admin)):
    from app.monitor import registry as dash_registry

    dash = dash_registry.get_dashboard(dashboard_id)
    if dash is None:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return {"dashboard": dash}


@router.put("/monitor/dashboards")
async def upsert_monitor_dashboard(
    payload: MonitorDashboardUpsert, principal: Principal = Depends(require_admin)
):
    """Create or update a Monitor dashboard (tiles + layout)."""
    from app.monitor import registry as dash_registry

    data = payload.model_dump(exclude_none=True)
    data["tenant_id"] = principal.tenant_id
    actor = principal.display_name or principal.email or principal.subject
    if not payload.id:
        data["created_by"] = actor
    saved = dash_registry.upsert_dashboard(data, actor=actor)
    return {"dashboard": saved}


@router.delete("/monitor/dashboards/{dashboard_id}")
async def delete_monitor_dashboard(dashboard_id: str, _: Principal = Depends(require_admin)):
    from app.monitor import registry as dash_registry

    if not dash_registry.delete_dashboard(dashboard_id):
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return {"ok": True}


@router.post("/monitor/dashboards/{dashboard_id}/set-default")
async def set_default_monitor_dashboard(
    dashboard_id: str, principal: Principal = Depends(require_admin)
):
    from app.monitor import registry as dash_registry

    actor = principal.display_name or principal.email or principal.subject
    saved = dash_registry.set_default(dashboard_id, principal.tenant_id, actor)
    if saved is None:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return {"dashboard": saved}


# ---- Monitor 2.0: widget data engine + AI authoring ------------------------------
class WidgetRunRequest(BaseModel):
    dataSource: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    no_cache: bool = False


class AiWidgetRequest(BaseModel):
    prompt: str = Field(max_length=4000)


class AiDashboardRequest(BaseModel):
    workload_id: str
    selected: list[dict[str, Any]] | None = None
    save: bool = False
    archetype: str = "full_stack"


@router.get("/monitor/datasources")
async def monitor_datasources(_: Principal = Depends(require_admin)):
    """Catalog of data sources + widget types (powers the editor + grounds the AI)."""
    from app.monitor.catalog import DATASOURCE_CATALOG, WIDGET_CATALOG

    return {"datasources": DATASOURCE_CATALOG, "widgets": WIDGET_CATALOG}


@router.post("/monitor/widgets/run")
async def run_monitor_widget(
    payload: WidgetRunRequest, principal: Principal = Depends(require_admin)
):
    """Resolve one widget's data binding to a normalized table (cached). Live refresh
    and the editor's preview both call this."""
    from app.monitor.datasources.resolver import resolve_widget

    result = await resolve_widget(
        payload.dataSource,
        tenant_id=principal.tenant_id,
        params=payload.params,
        use_cache=not payload.no_cache,
    )
    return {"result": result}


@router.post("/monitor/ai/widget")
async def ai_build_widget(
    payload: AiWidgetRequest, principal: Principal = Depends(require_admin)
):
    """Natural language → a single widget config (validated, ready to place)."""
    from app.core.azure_connections import public_connections
    from app.monitor.ai_author import build_widget
    from app.workbooks import registry as wb_registry

    context = {
        "connections": {c["id"]: c["display_name"] for c in public_connections()},
        "workbooks": {w["id"]: w["name"] for w in wb_registry.list_workbooks()},
    }
    widget = await build_widget(payload.prompt, context=context)
    if widget.get("error"):
        raise HTTPException(status_code=422, detail=widget["error"])
    # Validate by round-tripping through the registry cleaner.
    from app.monitor.registry import _clean_widget

    cleaned = _clean_widget({**widget, "type": widget.get("type")})
    if cleaned is None:
        raise HTTPException(status_code=422, detail="The AI produced an invalid widget type.")
    return {"widget": cleaned}


@router.post("/monitor/ai/dashboard/suggest")
async def ai_suggest_dashboard(
    payload: AiDashboardRequest, principal: Principal = Depends(require_admin)
):
    """Suggest a list of widgets to monitor a workload (uses its Architecture Memory)."""
    from app.monitor.ai_author import suggest_dashboard

    result = await suggest_dashboard(
        payload.workload_id, tenant_id=principal.tenant_id, archetype=payload.archetype
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.post("/monitor/ai/dashboard")
async def ai_build_dashboard(
    payload: AiDashboardRequest, principal: Principal = Depends(require_admin)
):
    """Build (and optionally save) a full dashboard for a workload, named after it."""
    from app.monitor import registry as dash_registry
    from app.monitor.ai_author import build_dashboard

    built = await build_dashboard(
        payload.workload_id,
        tenant_id=principal.tenant_id,
        selected=payload.selected,
        archetype=payload.archetype,
    )
    if built.get("error"):
        raise HTTPException(status_code=422, detail=built["error"])
    if payload.save:
        actor = principal.display_name or principal.email or principal.subject
        data = dict(built["dashboard"])
        data["tenant_id"] = principal.tenant_id
        data["created_by"] = actor
        saved = dash_registry.upsert_dashboard(data, actor=actor)
        built["saved_dashboard"] = saved
    return built


@router.get("/monitor/dashboards/{dashboard_id}/versions")
async def monitor_dashboard_versions(
    dashboard_id: str, _: Principal = Depends(require_admin)
):
    from app.monitor import registry as dash_registry

    if dash_registry.get_dashboard(dashboard_id) is None:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return {"versions": dash_registry.list_revisions(dashboard_id)}


@router.post("/monitor/dashboards/{dashboard_id}/restore/{version}")
async def restore_monitor_dashboard(
    dashboard_id: str, version: int, principal: Principal = Depends(require_admin)
):
    from app.monitor import registry as dash_registry

    actor = principal.display_name or principal.email or principal.subject
    restored = dash_registry.restore_revision(dashboard_id, version, actor=actor)
    if restored is None:
        raise HTTPException(status_code=404, detail="Dashboard or version not found.")
    return {"dashboard": restored}


