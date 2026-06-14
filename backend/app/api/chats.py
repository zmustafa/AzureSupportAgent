"""Chat + message endpoints, including the SSE streaming agent turn."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent.factory import active_model, build_provider
from app.agent.orchestrator import Orchestrator
from app.core.ai_prompts import get_full_prompt, get_list
from app.core.config import get_settings
from app.core.db import SessionLocal, get_db
from app.core.security import Principal, get_principal, require_admin
from app.core.utils import format_error, safe_json_parse
from app.mcp.client import build_mcp_client
from app.models import AuditLog, Chat, Message, ToolCall, Usage
from app.schemas import ChatCreate, ChatOut, MessageCreate, MessageOut

logger = logging.getLogger("app.chats")
router = APIRouter(prefix="/chats", tags=["chats"])
settings = get_settings()


def _active_provider() -> str:
    """The globally-active provider id (for new chats / fallback)."""
    from app.core.llm_config import get_active

    return get_active().get("provider", "")


def _default_connection_id() -> str | None:
    """The default Azure connection id (tenant) for new chats, if any are configured."""
    from app.core.azure_connections import get_default_connection

    conn = get_default_connection()
    return conn["id"] if conn else None


async def _summarize_title(first_message: str) -> str:
    """Summarize the user's first message into a short chat title via the LLM.

    Falls back to a trimmed version of the message if the model call fails or
    returns nothing usable.
    """
    fallback = first_message.strip().splitlines()[0][:60] if first_message.strip() else "New Chat"
    try:
        provider = build_provider(settings)
        messages = [
            {"role": "system", "content": get_full_prompt("chat_title")},
            {"role": "user", "content": first_message[:2000]},
        ]
        text = ""
        async for ev in provider.stream(messages, None):
            if ev.type == "token":
                text += ev.text
        title = " ".join(text.split()).strip().strip('"').strip()
        if title:
            return title[:60]
    except Exception as exc:  # noqa: BLE001 - title is best-effort; fall back gracefully
        logger.warning("Auto-title generation failed: %s", format_error(exc))
    return fallback


async def _get_owned_chat(db: AsyncSession, principal: Principal, chat_id: str) -> Chat:
    chat = await db.get(Chat, chat_id)
    if (
        chat is None
        or chat.tenant_id != principal.tenant_id
        or chat.user_id != principal.subject
    ):
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.get("", response_model=list[ChatOut])
async def list_chats(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    # Order by last activity (newest message, falling back to chat creation) so a chat
    # the user just interacted with bubbles to the top of "Recents". Pinned chats first.
    last_msg = (
        select(Message.chat_id, func.max(Message.created_at).label("last_at"))
        .group_by(Message.chat_id)
        .subquery()
    )
    activity = func.coalesce(last_msg.c.last_at, Chat.created_at)
    result = await db.execute(
        select(Chat)
        .outerjoin(last_msg, last_msg.c.chat_id == Chat.id)
        .where(
            Chat.tenant_id == principal.tenant_id,
            Chat.user_id == principal.subject,
            Chat.archived.is_(False),
        )
        .order_by(Chat.pinned.desc(), activity.desc())
    )
    return list(result.scalars().all())


@router.get("/active")
async def active_turns(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Chat ids with an in-flight turn, scoped to the caller's own chats.

    This is the server's source of truth for "which chats are working right now", so
    the sidebar shows live spinners in EVERY browser tab/window — not just the one that
    started the turn. Returns only ids the caller owns (no cross-tenant leakage).
    """
    from app.agent.turn_runner import registry

    active = registry.active_chat_ids()
    if not active:
        return {"active": []}
    rows = (
        await db.execute(
            select(Chat.id).where(
                Chat.id.in_(active),
                Chat.tenant_id == principal.tenant_id,
                Chat.user_id == principal.subject,
            )
        )
    ).scalars().all()
    return {"active": list(rows)}


@router.get("/investigations")
async def list_investigations(
    limit: int = 30,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Recent deep investigations across the caller's chats, with a derived confidence
    score and hypothesis tallies — a single place to browse past root-cause analyses
    instead of reopening each chat. Tenant + owner scoped."""
    from app.agent.investigation_summary import investigation_digest

    limit = max(1, min(limit, 100))
    rows = (
        await db.execute(
            select(Message, Chat.title)
            .join(Chat, Chat.id == Message.chat_id)
            .where(
                Chat.tenant_id == principal.tenant_id,
                Message.investigation_json.is_not(None),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()

    out: list[dict[str, Any]] = []
    for msg, title in rows:
        digest = investigation_digest(msg.investigation_json)
        out.append(
            {
                "chat_id": msg.chat_id,
                "message_id": msg.id,
                "title": title or "Untitled",
                "created_at": msg.created_at,
                "provider": msg.provider,
                "model": msg.model,
                "duration_ms": msg.duration_ms,
                **digest,
            }
        )
    return {"investigations": out}


@router.post("", response_model=ChatOut)
async def create_chat(
    payload: ChatCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    chat = Chat(
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
        title=payload.title or "New Chat",
        system_prompt=payload.system_prompt,
        provider=payload.provider or _active_provider(),
        model=payload.model or active_model() or settings.llm_model,
        connection_id=payload.connection_id or _default_connection_id(),
    )
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return chat


@router.get("/{chat_id}/messages", response_model=list[MessageOut])
async def list_messages(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_chat(db, principal, chat_id)
    result = await db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
    )
    return [MessageOut.from_model(m) for m in result.scalars().all()]


class BreakoutRequest(BaseModel):
    up_to_message_id: str | None = None


@router.post("/{chat_id}/breakout", response_model=ChatOut)
async def breakout_chat(
    chat_id: str,
    payload: BreakoutRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Fork a chat into a new thread, copying the conversation up to (and including) a
    given message. The new chat inherits the source's model/tenant/system prompt."""
    src = await _get_owned_chat(db, principal, chat_id)
    rows = (
        await db.execute(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
        )
    ).scalars().all()

    # Copy up to and including the selected message (or everything if unspecified).
    cutoff = len(rows)
    if payload.up_to_message_id:
        for i, m in enumerate(rows):
            if m.id == payload.up_to_message_id:
                cutoff = i + 1
                break
    to_copy = rows[:cutoff]

    base_title = (src.title or "Chat").removeprefix("Breakout: ")
    new_chat = Chat(
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
        title=f"Breakout: {base_title}"[:256],
        system_prompt=src.system_prompt,
        provider=src.provider,
        model=src.model,
        connection_id=src.connection_id,
    )
    db.add(new_chat)
    await db.flush()  # assign new_chat.id

    for m in to_copy:
        db.add(
            Message(
                chat_id=new_chat.id,
                role=m.role,
                content=m.content,
                token_count=m.token_count,
                activity_json=m.activity_json,
                images_json=m.images_json,
                provider=m.provider,
                model=m.model,
                duration_ms=m.duration_ms,
                created_at=m.created_at,
            )
        )
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="chat.breakout",
            target=new_chat.id,
            metadata_json={"source_chat": chat_id, "messages": len(to_copy)},
        )
    )
    await db.commit()
    await db.refresh(new_chat)
    return new_chat


@router.delete("/{chat_id}/messages/from/{message_id}")
async def delete_messages_from(
    chat_id: str,
    message_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Delete the given message and every message after it (used to retry an earlier
    answer: truncate the conversation back to the preceding user turn)."""
    await _get_owned_chat(db, principal, chat_id)
    rows = (
        await db.execute(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
        )
    ).scalars().all()
    start = next((i for i, m in enumerate(rows) if m.id == message_id), None)
    if start is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    for m in rows[start:]:
        await db.delete(m)
    await db.commit()
    return {"ok": True, "deleted": len(rows) - start}


@router.delete("/{chat_id}")
async def delete_chat(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a chat: move it to Trash (archived). It can be restored or purged
    from the Trash view. Messages are kept until the chat is permanently deleted."""
    chat = await _get_owned_chat(db, principal, chat_id)
    chat.archived = True
    await db.commit()
    return {"ok": True}


@router.get("/trash", response_model=list[ChatOut])
async def list_trash(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """List the current user's trashed (archived) chats, most-recently-active first."""
    last_msg = (
        select(Message.chat_id, func.max(Message.created_at).label("last_at"))
        .group_by(Message.chat_id)
        .subquery()
    )
    activity = func.coalesce(last_msg.c.last_at, Chat.created_at)
    result = await db.execute(
        select(Chat)
        .outerjoin(last_msg, last_msg.c.chat_id == Chat.id)
        .where(
            Chat.tenant_id == principal.tenant_id,
            Chat.user_id == principal.subject,
            Chat.archived.is_(True),
        )
        .order_by(activity.desc())
    )
    return list(result.scalars().all())


@router.post("/{chat_id}/restore", response_model=ChatOut)
async def restore_chat(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Restore a trashed chat back into the active list."""
    chat = await _get_owned_chat(db, principal, chat_id)
    chat.archived = False
    await db.commit()
    await db.refresh(chat)
    return chat


@router.delete("/{chat_id}/purge")
async def purge_chat(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a single trashed chat (and its messages). Irreversible."""
    chat = await _get_owned_chat(db, principal, chat_id)
    await db.delete(chat)
    await db.commit()
    return {"ok": True}


@router.post("/trash/empty")
async def empty_trash(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete ALL trashed chats for the current user. Irreversible."""
    result = await db.execute(
        select(Chat).where(
            Chat.tenant_id == principal.tenant_id,
            Chat.user_id == principal.subject,
            Chat.archived.is_(True),
        )
    )
    chats = list(result.scalars().all())
    for chat in chats:
        await db.delete(chat)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="chats.empty_trash",
            target=None,
            metadata_json={"count": len(chats)},
        )
    )
    await db.commit()
    return {"ok": True, "deleted": len(chats)}


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    provider: str | None = None
    model: str | None = None
    connection_id: str | None = None
    thinking_level: str | None = None
    agent_id: str | None = None


@router.patch("/{chat_id}", response_model=ChatOut)
async def update_chat(
    chat_id: str,
    payload: ChatPatch,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Rename, pin/unpin, or change the AI provider/model/tenant for a chat."""
    chat = await _get_owned_chat(db, principal, chat_id)
    if payload.title is not None and payload.title.strip():
        chat.title = payload.title.strip()[:256]
    if payload.pinned is not None:
        chat.pinned = payload.pinned
    if payload.provider is not None:
        chat.provider = payload.provider
    if payload.model is not None:
        chat.model = payload.model
    if payload.connection_id is not None:
        chat.connection_id = payload.connection_id or None
    if payload.thinking_level is not None and payload.thinking_level in ("normal", "deep"):
        chat.thinking_level = payload.thinking_level
    if payload.agent_id is not None:
        # Empty string clears the agent (back to the default assistant).
        chat.agent_id = payload.agent_id or None
    await db.commit()
    await db.refresh(chat)
    return chat


@router.delete("")
async def delete_all_chats(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete all of the current user's UNPINNED chats: move them to Trash, where
    they can be restored or permanently purged. Pinned chats are kept. Matches the
    single-chat Delete behavior (archive, not hard-delete) so bulk delete never loses
    data irrecoverably."""
    result = await db.execute(
        select(Chat).where(
            Chat.tenant_id == principal.tenant_id,
            Chat.user_id == principal.subject,
            Chat.pinned.isnot(True),
            Chat.archived.is_(False),
        )
    )
    chats = list(result.scalars().all())
    for chat in chats:
        chat.archived = True
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="chats.delete_all",
            target=None,
            metadata_json={"count": len(chats)},
        )
    )
    await db.commit()
    return {"ok": True, "deleted": len(chats)}


async def _history_for(db: AsyncSession, chat_id: str) -> list[dict[str, Any]]:
    result = await db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
    )
    msgs = result.scalars().all()
    history: list[dict[str, Any]] = []
    for m in msgs:
        images = (m.images_json or []) if m.role == "user" else []
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
            for url in images[:6]:
                if isinstance(url, str) and url.startswith("data:"):
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            history.append({"role": m.role, "content": parts})
        else:
            history.append({"role": m.role, "content": m.content})
    return history


@router.get("/{chat_id}/suggestions")
async def get_suggestions(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Return short, clickable follow-up suggestions for this chat.

    Empty chat -> static starter prompts. Otherwise a quick LLM call proposes
    context-aware next actions based on the recent conversation.
    """
    await _get_owned_chat(db, principal, chat_id)
    history = await _history_for(db, chat_id)
    if not history:
        return {"suggestions": get_list("chat_starter_suggestions")}

    from app.core.app_settings import load_settings

    if not load_settings().get("suggestions", True):
        return {"suggestions": []}

    provider = build_provider(settings)
    messages = [
        {"role": "system", "content": get_full_prompt("chat_suggestions")},
        *history[-6:],
        {"role": "user", "content": "Suggest 4 follow-up actions."},
    ]
    text = ""
    try:
        async for ev in provider.stream(messages, None):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001 - suggestions are best-effort
        logger.warning("Suggestion generation failed: %s", format_error(exc))
        return {"suggestions": []}

    suggestions = [
        line.strip(" -*0123456789.\t").strip()
        for line in text.splitlines()
        if line.strip()
    ]
    return {"suggestions": suggestions[:4]}


async def _classify_scope(content: str, prompt: str) -> str:
    """Run a single-word scope classifier prompt; return the raw decision text."""
    provider = build_provider(settings)
    decision = ""
    try:
        async for ev in provider.stream(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            None,
        ):
            if ev.type == "token":
                decision += ev.text
    except Exception as exc:  # noqa: BLE001 - scope hint is best-effort
        logger.warning("Scope classification failed: %s", format_error(exc))
        return ""
    return decision


async def _list_subscriptions(connection: dict | None = None) -> list[dict]:
    """Enumerate subscriptions for the active tenant.

    Prefers the selected connection's ARM token (works for any tenant, incl. pasted-
    token connections); falls back to the Azure MCP server's ``subscription_list``."""
    if connection:
        from app.azure.arm import list_subscriptions as arm_list_subs
        from app.azure.credentials import get_arm_token

        token, err = await get_arm_token(connection)
        if token and not err:
            subs, sub_err = await arm_list_subs(token)
            if not sub_err and subs:
                default_sub = connection.get("default_subscription", "")
                for s in subs:
                    s["is_default"] = s["id"] == default_sub
                return subs
    options: list[dict] = []
    client = None
    try:
        client = build_mcp_client(settings, connection=connection)
        result = await client.call_tool("subscription_list", {})
        for block in result.get("content", []):
            parsed = safe_json_parse(block)
            if parsed is None:
                continue
            subs = (parsed or {}).get("results", {}).get("subscriptions", [])
            for s in subs:
                options.append(
                    {
                        "id": s.get("subscriptionId", ""),
                        "name": s.get("displayName", s.get("subscriptionId", "")),
                        "is_default": bool(s.get("isDefault")),
                    }
                )
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        logger.warning("Subscription discovery failed: %s", format_error(exc))
        return []
    finally:
        if client is not None:
            client.close()
    return options


async def _list_management_groups(connection: dict | None = None) -> list[dict]:
    """Enumerate management groups for the active tenant.

    Prefers the selected connection's ARM token (Microsoft.Management REST); falls back
    to shelling out to ``az account management-group list`` using the host identity.
    Returns [] on any failure so clarification never blocks the user.
    """
    if connection:
        from app.azure.arm import list_management_groups as arm_list_mgs
        from app.azure.credentials import get_arm_token

        token, err = await get_arm_token(connection)
        if token and not err:
            mgs, mg_err = await arm_list_mgs(token)
            if not mg_err and mgs:
                return mgs

    import asyncio
    import shutil
    import subprocess

    az = shutil.which("az")
    if not az:
        return []
    try:
        # Blocking subprocess in a worker thread so it works on any event loop
        # (the Windows SelectorEventLoop can't spawn asyncio subprocesses).
        result = await asyncio.to_thread(
            subprocess.run,
            [az, "account", "management-group", "list", "--output", "json"],
            capture_output=True,
            timeout=30,
        )
        out = result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("Management-group discovery timed out")
        return []
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        logger.warning("Management-group discovery failed: %s", format_error(exc))
        return []
    data = safe_json_parse(out.decode("utf-8", "ignore") or "[]", default=[])
    groups: list[dict] = []
    for g in data if isinstance(data, list) else []:
        gid = g.get("name", "")
        if not gid:
            continue
        groups.append({"id": gid, "name": g.get("displayName", gid)})
    return groups


def _build_workload_scope_hint(workload: dict) -> str:
    """Turn a workload's node membership into a precise, bounded scope constraint.

    Lists the concrete subscriptions / resource groups / resource ids the agent may
    consider, with the appropriate Resource Graph filters. Large memberships are
    summarized (counts + filters) to keep the prompt bounded."""
    subs: list[str] = []
    rgs: list[str] = []
    resources: list[str] = []
    mgs: list[str] = []
    excludes: list[str] = []
    for n in workload.get("nodes", []) or []:
        kind = n.get("kind")
        nid = n.get("id", "")
        if kind == "mg":
            mgs.append(n.get("name") or nid)
        elif kind == "subscription":
            subs.append(nid)
        elif kind == "resource_group":
            rgs.append(n.get("name") or nid)
        elif kind == "resource":
            resources.append(nid)
        for ex in n.get("excludes", []) or []:
            excludes.append(ex)

    # Collect EVERY in-scope subscription id — from subscription nodes and parsed out of
    # resource / RG node ids — so we can tell the agent the exact subscription to target.
    # The connection often has no default subscription, so a direct Azure tool (Logic Apps,
    # Monitor, App Service diagnostics, …) called WITHOUT an explicit subscription resolves
    # to the wrong/ambiguous one and 404s on the resource group. Naming the subscription and
    # requiring it on every tool call prevents that.
    import re as _re

    all_sub_ids: list[str] = []
    for _val in list(subs) + list(rgs) + list(resources):
        _m = _re.search(r"/subscriptions/([0-9a-fA-F-]{36})", _val)
        if _m and _m.group(1).lower() not in [s.lower() for s in all_sub_ids]:
            all_sub_ids.append(_m.group(1))
    for _s in subs:
        # Subscription nodes may store the bare guid as the id.
        if "/" not in _s and _s and _s.lower() not in [x.lower() for x in all_sub_ids]:
            all_sub_ids.append(_s)

    parts: list[str] = [
        "Scope constraint: the user scoped this conversation to the Azure workload "
        f"'{workload.get('name', 'workload')}'. Only consider the resources that belong "
        "to this workload; do not query anything outside it unless explicitly asked."
    ]
    if all_sub_ids:
        shown_subs = all_sub_ids[:10]
        sub_more = f" (+{len(all_sub_ids) - len(shown_subs)} more)" if len(all_sub_ids) > len(shown_subs) else ""
        parts.append(
            "IMPORTANT — pass the subscription id explicitly on EVERY Azure tool call "
            "(the `--subscription` / `subscription` parameter), not just Resource Graph "
            f"queries: {', '.join(repr(s) for s in shown_subs)}{sub_more}. There is no "
            "default subscription configured, so a tool called without it will resolve to "
            "the wrong subscription and fail with a 404 for the resource group."
        )
    if mgs:
        parts.append(
            "Management groups in scope (consider all subscriptions/resources beneath "
            f"them): {', '.join(mgs[:20])}."
        )
    if subs:
        shown = subs[:30]
        more = f" (+{len(subs) - len(shown)} more)" if len(subs) > len(shown) else ""
        parts.append(
            "Subscriptions in scope — filter Resource Graph with "
            f"`where subscriptionId in ({', '.join(repr(s) for s in shown)})`{more}."
        )
    if rgs:
        shown = rgs[:30]
        more = f" (+{len(rgs) - len(shown)} more)" if len(rgs) > len(shown) else ""
        parts.append(
            "Resource groups in scope — filter with "
            f"`where resourceGroup in~ ({', '.join(repr(r) for r in shown)})`{more}."
        )
    if resources:
        shown = resources[:50]
        if len(resources) > len(shown):
            # Too many ids to inline. Derive the concrete subscription + resource-group
            # filters FROM the resource ids so the model has an actionable Resource Graph
            # scope (it can't see the raw id list, so referencing "the workload's ids"
            # would leave it stuck). Falls back to listing ids when parsing yields nothing.
            derived_subs: list[str] = []
            derived_rgs: list[str] = []
            for rid in resources:
                ms = _re.search(r"/subscriptions/([^/]+)", rid, _re.IGNORECASE)
                if ms and ms.group(1) not in derived_subs:
                    derived_subs.append(ms.group(1))
                mr = _re.search(r"/resourcegroups/([^/]+)", rid, _re.IGNORECASE)
                if mr and mr.group(1) not in derived_rgs:
                    derived_rgs.append(mr.group(1))
            if derived_subs or derived_rgs:
                line = (
                    f"Individual resources in scope: {len(resources)} resource ids (too "
                    "many to list inline). They live in"
                )
                if derived_subs:
                    line += (
                        " subscription(s) "
                        f"`{', '.join(repr(s) for s in derived_subs[:30])}`"
                    )
                if derived_rgs:
                    rgs_shown = derived_rgs[:40]
                    rg_more = f" (+{len(derived_rgs) - len(rgs_shown)} more)" if len(derived_rgs) > len(rgs_shown) else ""
                    line += (
                        (" and" if derived_subs else "")
                        + " resource group(s) "
                        + f"`{', '.join(repr(r) for r in rgs_shown)}`{rg_more}"
                    )
                line += (
                    ". Scope Resource Graph queries with "
                    "`where subscriptionId in (...)"
                    + (" and resourceGroup in~ (...)" if derived_rgs else "")
                    + "` using those values, then post-filter to the workload's members. "
                    "For non-Resource-Graph tools, pass the subscription + resource group "
                    "above as explicit parameters."
                )
                parts.append(line)
            else:
                parts.append(
                    f"Individual resources in scope: {len(resources)} resource ids "
                    "(too many to list inline); filter Resource Graph with a `where id in (...)` "
                    "clause using the workload's resource ids."
                )
        else:
            parts.append(
                "Individual resources in scope — filter with "
                f"`where id in ({', '.join(repr(r) for r in shown)})`."
            )
    if excludes:
        parts.append(
            f"Explicitly EXCLUDE these {len(excludes)} resource id(s) even if they fall "
            "under an in-scope parent."
        )
    return "\n".join(parts)


@router.post("/{chat_id}/clarify")
async def clarify_scope(
    chat_id: str,
    payload: MessageCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Pre-flight check: does this question need the user to pick a scope first?

    Returns {needs_subscription, options, needs_management_group, mg_options} WITHOUT
    persisting anything. The frontend shows the options; once the user picks (or skips),
    it calls the stream endpoint with the chosen scope. Subscription clarification takes
    precedence (narrower, most common scope); management-group clarification covers
    governance/policy/org-wide questions and is opt-in via settings.
    """
    await _get_owned_chat(db, principal, chat_id)
    content = payload.content.strip()
    empty = {
        "needs_subscription": False,
        "options": [],
        "needs_management_group": False,
        "mg_options": [],
    }
    if not content:
        return empty

    from app.core.app_settings import load_settings
    from app.core.azure_connections import resolve_connection

    cfg = load_settings()
    sub_on = bool(cfg.get("scope_clarification", False))
    mg_on = bool(cfg.get("mgmt_group_clarification", False))
    if not sub_on and not mg_on:
        return empty

    # Discover within the selected tenant (connection) when one is configured.
    chat = await _get_owned_chat(db, principal, chat_id)
    conn = resolve_connection(payload.connection_id or chat.connection_id)

    # 1) Subscription scope (most common) takes precedence.
    if sub_on:
        decision = await _classify_scope(content, get_full_prompt("chat_scope_subscription"))
        if "NEEDS_SUBSCRIPTION" in decision.upper():
            options = await _list_subscriptions(conn)
            if options:
                return {**empty, "needs_subscription": True, "options": options}

    # 2) Management-group scope for governance/policy/org-wide questions.
    if mg_on:
        decision = await _classify_scope(content, get_full_prompt("chat_scope_mgmt"))
        if "NEEDS_MANAGEMENT_GROUP" in decision.upper():
            mg_options = await _list_management_groups(conn)
            if mg_options:
                # When subscription clarification is also enabled, include the
                # subscription list so the frontend can chain a subscription pick
                # after the management-group pick (or skip) — drilling down from the
                # governance scope into a specific subscription.
                sub_options = await _list_subscriptions(conn) if sub_on else []
                return {
                    **empty,
                    "needs_management_group": True,
                    "mg_options": mg_options,
                    "options": sub_options,
                }

    return empty


class ProposeRequest(BaseModel):
    content: str
    candidates: list[str] = []


@router.post("/{chat_id}/propose")
async def propose_problems(
    chat_id: str,
    payload: ProposeRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Propose up to 5 sharper problem statements for a vague first message.

    Matches the user's input against a catalog of common Azure problems (sent by the
    client from the built-in problem tree) and returns refined, first-person problem
    statements for the user to pick from. Returns an empty list (no LLM call) when the
    feature is disabled, so the client can always call this safely.
    """
    await _get_owned_chat(db, principal, chat_id)
    content = payload.content.strip()
    if not content:
        return {"suggestions": []}

    from app.core.app_settings import load_settings

    if not load_settings().get("propose_problems", False):
        return {"suggestions": []}

    # Bound the catalog so the prompt stays reasonable; the tree is already ordered
    # by relevance/volume so the head is the most useful slice.
    candidates = [c.strip() for c in payload.candidates if c and c.strip()][:600]
    if not candidates:
        return {"suggestions": []}

    catalog = "\n".join(candidates)
    user_block = f"Catalog:\n{catalog}\n\nUser message:\n{content}"
    provider = build_provider(settings)
    text = ""
    try:
        async for ev in provider.stream(
            [
                {"role": "system", "content": get_full_prompt("chat_propose_problems")},
                {"role": "user", "content": user_block},
            ],
            None,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001 - proposal is best-effort
        logger.warning("Problem proposal failed: %s", format_error(exc))
        return {"suggestions": []}

    suggestions = [
        line.strip(" -*0123456789.\t").strip()
        for line in text.splitlines()
        if line.strip()
    ]
    # De-duplicate while preserving order, drop empties, cap at 5.
    seen: set[str] = set()
    out: list[str] = []
    for s in suggestions:
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return {"suggestions": out[:5]}


class DeepAgentsRequest(BaseModel):
    content: str


@router.post("/{chat_id}/deep/agents")
async def deep_suggest_agents(
    chat_id: str,
    payload: DeepAgentsRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Suggest which specialist investigation agents to dispatch for a deep investigation.

    Returns the full agent catalog, each annotated with ``recommended`` + ``reason`` so
    the launch popup can pre-select the relevant specialists.
    """
    chat = await _get_owned_chat(db, principal, chat_id)
    from app.agent.deep_agents import suggest_agents

    agents = await suggest_agents(
        payload.content.strip(), provider=chat.provider, model=chat.model
    )
    return {"agents": agents}


@router.post("/{chat_id}/messages/stream")
async def stream_message(
    chat_id: str,
    payload: MessageCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Append a user message and stream the agent's response over SSE.

    The agent work runs in a background task (see turn_runner) that is NOT tied to
    this SSE connection, so navigating away / disconnecting never stops the turn —
    it runs to completion and persists. Reconnect via GET /{chat_id}/stream.
    """
    from app.agent.turn_runner import TurnRun, registry

    chat = await _get_owned_chat(db, principal, chat_id)

    # Don't start a second turn while one is already running for this chat; just
    # attach to the in-flight one (defensive — the UI also guards against this).
    if registry.is_active(chat_id):
        run = registry.get(chat_id)
        assert run is not None
        return EventSourceResponse(_sse_from_run(run))

    if payload.regenerate:
        # Regenerate in place: drop the trailing assistant message (and any trailing
        # tool messages) so we re-run the turn from the existing last user message,
        # rather than appending a duplicate user turn.
        result = await db.execute(
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.created_at.desc())
        )
        msgs = list(result.scalars().all())
        for m in msgs:
            if m.role in ("assistant", "tool"):
                await db.delete(m)
            elif m.role == "user":
                break
        await db.commit()
    else:
        user_msg = Message(
            chat_id=chat_id,
            role="user",
            content=payload.content,
            images_json=payload.images or None,
        )
        db.add(user_msg)
    # Auto-title from the first user message (if enabled): summarize the ask into a
    # short title rather than using the raw message text.
    from app.core.app_settings import load_settings

    if chat.title == "New Chat" and load_settings().get("auto_title", True):
        chat.title = await _summarize_title(payload.content)
    await db.commit()

    # Build a scope hint from the user's clarification choice, if any.
    scope_hint: str | None = None
    if payload.subscription_id and payload.management_group_id:
        sname = payload.subscription_name or payload.subscription_id
        mgname = payload.management_group_name or payload.management_group_id
        scope_hint = (
            "Scope constraint: the user chose to investigate ONLY the Azure "
            f"subscription '{sname}' (id: {payload.subscription_id}), which belongs to "
            f"the management group '{mgname}' (id: {payload.management_group_id}). Pass "
            "this subscription id to every Azure tool call, and scope any governance, "
            "policy, compliance or RBAC queries to this management group. Do not query "
            "other subscriptions or scopes unless the user explicitly asks."
        )
    elif payload.subscription_id:
        name = payload.subscription_name or payload.subscription_id
        scope_hint = (
            "Scope constraint: the user chose to investigate ONLY the Azure "
            f"subscription '{name}' (id: {payload.subscription_id}). Pass this "
            "subscription id to every Azure tool call and do not query other "
            "subscriptions unless the user explicitly asks."
        )
    elif payload.management_group_id:
        name = payload.management_group_name or payload.management_group_id
        scope_hint = (
            "Scope constraint: the user chose to investigate ONLY the Azure "
            f"management group '{name}' (id: {payload.management_group_id}). Scope "
            "governance, policy, compliance and RBAC queries to this management group, "
            "and when you need resource detail, enumerate the subscriptions under it "
            "and investigate those. Do not query scopes outside this management group "
            "unless the user explicitly asks."
        )
    elif payload.scope_all:
        scope_hint = (
            "Scope: the user chose to search ALL accessible subscriptions. "
            "Enumerate subscriptions and investigate across all of them."
        )

    # Resolve an optional custom agent for this turn (persona + tools + model + tenant).
    # Explicit payload.agent_id wins (empty string clears it), else the chat's saved
    # agent. Persisted on the chat so it sticks for subsequent messages.
    from app.automations import agents as _agents_registry

    turn_agent_id = chat.agent_id
    if payload.agent_id is not None:
        turn_agent_id = payload.agent_id or None  # "" explicitly clears the agent
    if chat.agent_id != turn_agent_id:
        chat.agent_id = turn_agent_id
        await db.commit()
    turn_agent = _agents_registry.get_agent(turn_agent_id) if turn_agent_id else None

    # Resolve the Azure connection (tenant) for this turn: explicit choice in the
    # payload wins, else the agent's tenant, else the chat's saved tenant, else default.
    from app.core.azure_connections import resolve_connection

    chosen_connection_id = (
        payload.connection_id or (turn_agent or {}).get("connection_id") or chat.connection_id
    )
    turn_connection = resolve_connection(chosen_connection_id)
    if turn_connection and not turn_agent and chat.connection_id != turn_connection["id"]:
        # Only persist tenant changes when NOT running as an agent (the agent's tenant
        # shouldn't overwrite the chat's own saved tenant once the agent is cleared).
        chat.connection_id = turn_connection["id"]
        await db.commit()
    if turn_connection:
        tenant_label = (
            payload.tenant_name
            or turn_connection.get("display_name")
            or turn_connection.get("tenant_id")
        )
        tenant_line = (
            f"Azure tenant context: you are operating in the '{tenant_label}' tenant "
            f"(tenant id: {turn_connection.get('tenant_id')}). All Azure tool calls use "
            "this tenant's identity; never assume resources from other tenants."
        )
        scope_hint = f"{tenant_line}\n\n{scope_hint}" if scope_hint else tenant_line

    # Resolve an optional Azure Workload (hand-picked resource scope) for this turn.
    # Explicit payload.workload_id wins ("" clears it), else the chat's saved workload.
    turn_workload_id = chat.workload_id
    if payload.workload_id is not None:
        turn_workload_id = payload.workload_id or None
    if chat.workload_id != turn_workload_id:
        chat.workload_id = turn_workload_id
        await db.commit()
    if turn_workload_id:
        from app.workloads.registry import get_workload

        workload = get_workload(turn_workload_id)
        if workload and workload.get("nodes"):
            workload_hint = _build_workload_scope_hint(workload)
            scope_hint = f"{scope_hint}\n\n{workload_hint}" if scope_hint else workload_hint
            # Backstop: if the workload lives in exactly one subscription and the connection
            # has no default subscription, pin the MCP server's default to it for this turn.
            # Otherwise a direct Azure tool called without an explicit subscription resolves
            # to the wrong one and 404s on the resource group.
            if turn_connection and not (turn_connection.get("default_subscription") or "").strip():
                import re as _re

                _wl_subs: set[str] = set()
                for _n in workload.get("nodes", []) or []:
                    if _n.get("kind") == "subscription" and _n.get("id"):
                        _wl_subs.add(str(_n["id"]).lower())
                    _ms = _re.search(r"/subscriptions/([0-9a-fA-F-]{36})", str(_n.get("id", "")))
                    if _ms:
                        _wl_subs.add(_ms.group(1).lower())
                if len(_wl_subs) == 1:
                    turn_connection = {**turn_connection, "default_subscription": next(iter(_wl_subs))}

    # Resolve sandbox troubleshooting VMs linked to this turn's workload. Their tools
    # (vm_exec/vm_list/vm_read_file) are registered below; here we tell the model they
    # exist and what OS/toolkit each box has.
    turn_sandbox_vms: list[dict] = []
    if turn_workload_id:
        from app.core.sandbox_vms import resolve_for_workload as _resolve_vms

        turn_sandbox_vms = _resolve_vms(turn_workload_id)
        if turn_sandbox_vms:
            from app.agent.vm_tools import vm_context_hint as _vm_hint

            _hint = _vm_hint(turn_sandbox_vms)
            scope_hint = f"{scope_hint}\n\n{_hint}" if scope_hint else _hint

    history = await _history_for(db, chat_id)

    # Resolve the thinking level for this turn: explicit payload wins, else the chat's
    # saved level. Persist it on the chat so it stays on for subsequent messages.
    turn_thinking = (payload.thinking_level or chat.thinking_level or "normal").lower()
    if turn_thinking not in ("normal", "deep"):
        turn_thinking = "normal"
    if chat.thinking_level != turn_thinking:
        chat.thinking_level = turn_thinking
        await db.commit()
    # Specialist agents the user chose for a deep-investigation war room (ids).
    turn_deep_agents = [a for a in (payload.deep_agents or []) if isinstance(a, str)]
    # Resolve the architecture Memory to inject into a deep investigation (intended
    # design + known gaps + diagnostic hints). Explicit pick wins; else, if exactly one
    # architecture with an enabled memory is linked to this turn's workload, use it.
    turn_arch_memory = ""
    if turn_thinking == "deep":
        try:
            from app.architectures import memory as _mem
            from app.architectures import registry as _arch_reg

            chosen_arch_id = (payload.architecture_memory_id or "").strip()
            if chosen_arch_id == "__none__":
                pass  # user explicitly suppressed memory for this investigation
            elif chosen_arch_id:
                _m = _mem.get_memory(chosen_arch_id)
                if _m is not None:
                    _a = _arch_reg.get_architecture(chosen_arch_id) or {}
                    turn_arch_memory = _mem.render_for_investigation(
                        _m, _a.get("name", ""), _a.get("workload_name", "")
                    )
            elif turn_workload_id:
                _candidates = [
                    a for a in _arch_reg.list_architectures(principal.tenant_id)
                    if a.get("workload_id") == turn_workload_id
                ]
                _with_mem = []
                for _a in _candidates:
                    _m = _mem.get_memory(_a["id"])
                    if _m is not None and _m.get("enabled_for_investigations", True):
                        _with_mem.append((_a, _m))
                if len(_with_mem) == 1:
                    _a, _m = _with_mem[0]
                    turn_arch_memory = _mem.render_for_investigation(
                        _m, _a.get("name", ""), _a.get("workload_name", "")
                    )
        except Exception:  # noqa: BLE001 - memory is best-effort context, never fatal
            turn_arch_memory = ""
    # Resolve the AI provider + model for this turn: the chat's own selection takes
    # precedence (so each chat keeps its model), falling back to the global active.
    from app.core.llm_config import get_active

    # IMPORTANT: provider and model must be resolved as a PAIR. Only honour the chat's
    # model when the chat also has a provider — otherwise an orphan model would be run
    # against (and persisted with) the global provider, producing a mismatched pair
    # like "github_copilot" + "<an OpenRouter model>".
    agent_forced_model = bool(turn_agent and turn_agent.get("provider") and turn_agent.get("model"))
    if agent_forced_model:
        # Running as a custom agent: the agent's provider+model win for this turn.
        _active = get_active(turn_agent["provider"], turn_agent["model"])
    elif chat.provider and chat.model:
        _active = get_active(chat.provider, chat.model)
    else:
        _active = get_active()
    turn_provider = _active.get("provider", "")
    turn_model = _active.get("model", "") or active_model()

    # Persist the resolved provider/model on the chat so it sticks for next time — but
    # NOT when an agent forced the model (that would clobber the chat's own picker).
    if not agent_forced_model and (chat.provider != turn_provider or chat.model != turn_model):
        chat.provider = turn_provider
        chat.model = turn_model
        await db.commit()

    # Build the custom-agent runtime extras (persona instructions, connector toolset,
    # write policy) applied to the Orchestrator when an agent is selected for this chat.
    turn_extra_instructions: str | None = None
    turn_connector_toolset = None
    turn_write_override: str | None = None
    if turn_agent:
        from app.connectors.registry import build_toolset

        if turn_agent.get("instructions"):
            turn_extra_instructions = (
                f"You are running as the custom agent '{turn_agent.get('name', 'agent')}'. "
                f"Follow these instructions:\n{turn_agent['instructions']}"
            )
        turn_connector_toolset = build_toolset(turn_agent.get("connector_tools"))
        turn_write_override = "off" if turn_agent.get("run_mode") == "autonomous" else "gated"
    else:
        # Default assistant: no external connectors, but it still gets the first-party
        # built-in utility tools (web fetch + network diagnostics), if enabled by admin.
        from app.connectors.registry import build_toolset

        turn_connector_toolset = build_toolset(include_connectors=False)

    # Merge in sandbox-VM tools (vm_exec/vm_list/vm_read_file) when the workload has
    # onboarded sandbox boxes. Deep mode runs them read-only (see worker below).
    if turn_sandbox_vms and turn_connector_toolset is not None:
        from app.agent.vm_tools import register_vm_tools

        register_vm_tools(
            turn_connector_toolset,
            turn_sandbox_vms,
            tenant_id=principal.tenant_id,
            chat_id=chat_id,
            actor=principal.display_name or principal.email or principal.subject,
            trigger="chat",
            read_only=(turn_thinking == "deep"),
        )

    # Performance Profiler tool: lets the investigation launch an AMBA-threshold performance
    # profile for the in-scope workload and use its scorecard + bottlenecks as evidence.
    if turn_workload_id and turn_connector_toolset is not None:
        from app.agent.perfprofile_tool import register_profiler_tool

        register_profiler_tool(
            turn_connector_toolset,
            workload_id=turn_workload_id,
            connection=turn_connection,
            tenant_id=principal.tenant_id,
            actor=principal.display_name or principal.email or principal.subject,
        )

    # EntraID (Microsoft Graph) tools: a custom agent opts in via allow_all_entra; the
    # default assistant gets them when the admin has enabled the global toggle.
    if turn_agent is not None:
        turn_entra_enabled = bool(turn_agent.get("allow_all_entra", False))
    else:
        from app.core.app_settings import load_settings as _load_app_settings

        turn_entra_enabled = bool(_load_app_settings().get("entra_mcp_enabled", False))

    # Create the assistant message up-front (in the request session) so we have a
    # stable id; the background task updates it via its own session.
    assistant_msg = Message(
        chat_id=chat_id,
        role="assistant",
        content="",
        provider=turn_provider,
        model=turn_model,
    )
    db.add(assistant_msg)
    await db.commit()
    await db.refresh(assistant_msg)
    assistant_id = assistant_msg.id

    tenant_id = principal.tenant_id
    actor_id = principal.subject
    fallback_model = chat.model or active_model() or settings.llm_model

    async def worker(run: "TurnRun") -> None:
        """Runs the whole turn with its OWN DB session; survives client disconnect."""
        import time as _time

        turn_started = _time.perf_counter()
        deep = turn_thinking == "deep"
        if deep:
            from app.agent.deep_investigation import DeepInvestigator

            runner: Any = DeepInvestigator(
                settings,
                provider=turn_provider,
                model=turn_model,
                connection=turn_connection,
                connector_toolset=turn_connector_toolset,
                focus=turn_deep_agents,
                architecture_memory=turn_arch_memory or None,
            )
        else:
            runner = Orchestrator(
                settings,
                provider=turn_provider,
                model=turn_model,
                connection=turn_connection,
                connector_toolset=turn_connector_toolset,
                extra_instructions=turn_extra_instructions,
                write_policy_override=turn_write_override,
                entra_enabled=turn_entra_enabled,
            )
        orchestrator = runner
        async with SessionLocal() as task_db:
            assistant = await task_db.get(Message, assistant_id)
            if assistant is None:  # pragma: no cover - shouldn't happen
                return

            assistant_text = ""
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            activity: list[dict[str, Any]] = []
            reasoning_buf = ""
            tokens_since_save = 0
            # Deep-investigation tree accumulated from phase/hypothesis events.
            investigation: dict[str, Any] = {"phases": [], "hypotheses": [], "conclusion": None, "agents": []}

            async def checkpoint() -> None:
                assistant.content = assistant_text
                # Assign fresh copies so SQLAlchemy detects the change on the JSON
                # columns. Re-assigning the SAME mutable object after the first commit
                # is identity-equal and silently dropped (no mutation tracking), which
                # would truncate the activity feed and the deep-investigation tree.
                assistant.activity_json = [dict(s) for s in activity] if activity else None
                if deep:
                    assistant.investigation_json = {
                        "phases": [dict(p) for p in investigation.get("phases", [])],
                        "hypotheses": [dict(h) for h in investigation.get("hypotheses", [])],
                        "conclusion": investigation.get("conclusion"),
                        "agents": [dict(a) for a in investigation.get("agents", [])],
                        **({"research": investigation["research"]} if "research" in investigation else {}),
                    }
                await task_db.commit()

            try:
                async for ev in orchestrator.run(history, scope_hint=scope_hint):
                    if ev.type == "token":
                        assistant_text += ev.data["text"]
                        reasoning_buf += ev.data["text"]
                        tokens_since_save += 1
                        if tokens_since_save >= 40:
                            tokens_since_save = 0
                            await checkpoint()
                    elif ev.type == "done":
                        usage["prompt_tokens"] = ev.data.get("prompt_tokens", 0)
                        usage["completion_tokens"] = ev.data.get("completion_tokens", 0)
                        if ev.data.get("content"):
                            assistant_text = ev.data["content"] or assistant_text
                        if ev.data.get("investigation"):
                            # Keep the live event-accumulated tree; just fold in the
                            # research summary the runner reports at the end.
                            investigation["research"] = ev.data["investigation"].get("research")

                    # Accumulate the deep-investigation structure for persistence.
                    if ev.type == "phase":
                        investigation.setdefault("phases", []).append(ev.data)
                    elif ev.type == "agents":
                        investigation["agents"] = ev.data.get("agents", [])
                    elif ev.type == "hypothesis":
                        investigation.setdefault("hypotheses", []).append(dict(ev.data))
                    elif ev.type == "hypothesis_status":
                        for h in investigation.get("hypotheses", []):
                            if h.get("id") == ev.data.get("id"):
                                h["status"] = ev.data.get("status")
                                h["evidence"] = ev.data.get("evidence", "")
                                break
                    elif ev.type == "conclusion":
                        investigation["conclusion"] = ev.data

                    if ev.type in ("tool_start", "approval_required"):
                        if reasoning_buf.strip():
                            activity.append({"kind": "reasoning", "text": reasoning_buf.strip()})
                            reasoning_buf = ""
                        # The text streamed before a tool call is pre-work narration
                        # (the model's understanding/plan), captured above as a reasoning
                        # step. It must NOT remain in the final answer, so reset the
                        # accumulator — only the post-final-tool answer should persist.
                        assistant_text = ""
                        activity.append(
                            {
                                "kind": "tool",
                                "name": ev.data["tool_name"],
                                "args": ev.data.get("arguments", {}),
                                "status": "awaiting_approval"
                                if ev.type == "approval_required"
                                else "running",
                            }
                        )
                        kind = "write" if ev.type == "approval_required" else "read"
                        status = (
                            "awaiting_approval" if ev.type == "approval_required" else "running"
                        )
                        task_db.add(
                            ToolCall(
                                chat_id=chat_id,
                                tenant_id=tenant_id,
                                tool_name=ev.data["tool_name"],
                                arguments_json=ev.data.get("arguments", {}),
                                subscription_id=settings.azure_subscription_id or None,
                                kind=kind,
                                status=status,
                            )
                        )
                        task_db.add(
                            AuditLog(
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                                action=f"tool.{ev.type}",
                                target=ev.data["tool_name"],
                                provider=turn_provider,
                                model=turn_model,
                                metadata_json={"chat_id": chat_id},
                            )
                        )
                        await checkpoint()

                    if ev.type == "tool_result":
                        for step in reversed(activity):
                            if step.get("kind") == "tool" and step.get("status") == "running":
                                step["status"] = "done"
                                step["summary"] = ev.data.get("summary")
                                step["duration"] = ev.data.get("duration_ms")
                                break
                        reasoning_buf = ""
                        await checkpoint()

                    run.emit(ev.type, ev.data)

                # Final, defensive strip of any ReAct protocol leakage and the
                # pre-work understanding/plan preamble before saving.
                from app.agent.tool_protocol import (
                    strip_plan_preamble,
                    strip_react_artifacts,
                )

                cleaned = strip_plan_preamble(strip_react_artifacts(assistant_text))
                if cleaned:
                    assistant_text = cleaned
                # Record how long the whole turn took to process.
                duration_ms = int((_time.perf_counter() - turn_started) * 1000)
                assistant.duration_ms = duration_ms
                await checkpoint()
                task_db.add(
                    Usage(
                        tenant_id=tenant_id,
                        user_id=actor_id,
                        chat_id=chat_id,
                        model=fallback_model,
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                    )
                )
                task_db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action="chat.turn",
                        target=chat_id,
                        provider=turn_provider,
                        model=turn_model,
                        metadata_json={
                            "chat_id": chat_id,
                            "prompt_tokens": usage["prompt_tokens"],
                            "completion_tokens": usage["completion_tokens"],
                        },
                    )
                )
                await task_db.commit()
                run.emit("saved", {"id": assistant_id, "duration_ms": duration_ms})
            except asyncio.CancelledError:
                # The user stopped the turn (POST /chats/{id}/stop). Persist whatever
                # was produced so far and emit a final `done` so the client closes the
                # stream cleanly with the partial answer, then re-raise so the task is
                # properly marked cancelled.
                logger.info("Turn stopped by user (chat=%s)", chat_id)
                try:
                    from app.agent.tool_protocol import (
                        strip_plan_preamble,
                        strip_react_artifacts,
                    )

                    cleaned = strip_plan_preamble(strip_react_artifacts(assistant_text))
                    if cleaned:
                        assistant_text = cleaned
                    # Close out any tool step still marked "running" so the persisted
                    # activity feed doesn't show a stuck spinner.
                    for step in reversed(activity):
                        if step.get("kind") == "tool" and step.get("status") == "running":
                            step["status"] = "done"
                            step["summary"] = "Stopped by user"
                            break
                    assistant.duration_ms = int((_time.perf_counter() - turn_started) * 1000)
                    # Shield the final persistence from the in-flight cancellation so the
                    # partial answer is committed before the task unwinds.
                    await asyncio.shield(checkpoint())
                except Exception:  # noqa: BLE001 - best-effort save on stop
                    pass
                run.emit("done", {"content": assistant_text, "stopped": True})
                raise
            except Exception as exc:  # noqa: BLE001 - surface + persist the error
                detail = format_error(exc, max_len=1500)
                logger.warning(
                    "Turn failed (provider=%s model=%s): %s",
                    turn_provider,
                    turn_model,
                    detail,
                )
                friendly = (
                    f"⚠️ The model could not complete this response.\n\n**{turn_provider} · "
                    f"{turn_model}** returned an error:\n\n```\n{detail}\n```\n\n"
                    "Try again, or switch models using the picker below."
                )
                try:
                    assistant.content = (
                        (assistant_text + "\n\n" + friendly)
                        if assistant_text.strip()
                        else friendly
                    )
                    await task_db.commit()
                except Exception:  # noqa: BLE001
                    pass
                run.emit("error", {"message": detail})
            finally:
                # Always release the per-turn MCP client (temp cert file, etc.).
                orchestrator.close()

    run = registry.start(chat_id, assistant_id, worker)
    return EventSourceResponse(_sse_from_run(run))


async def _sse_from_run(run) -> Any:
    """Adapt a TurnRun's event stream to sse-starlette frames."""
    async for frame in run.subscribe():
        yield {"event": frame["event"], "data": json.dumps(frame["data"])}


@router.get("/{chat_id}/active")
async def turn_active(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Whether an agent turn is currently running for this chat (for reconnect)."""
    from app.agent.turn_runner import registry

    await _get_owned_chat(db, principal, chat_id)
    return {"active": registry.is_active(chat_id)}


@router.post("/{chat_id}/stop")
async def stop_turn(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Stop the in-flight agent turn for this chat.

    The turn runs in a background task decoupled from the SSE connection, so simply
    closing the client stream never stops the work. This cancels that task; the worker
    persists whatever it produced so far and emits a final `done` event. Owner-scoped.
    """
    from app.agent.turn_runner import registry

    await _get_owned_chat(db, principal, chat_id)
    stopped = registry.cancel(chat_id)
    return {"stopped": stopped}


@router.get("/{chat_id}/stream")
async def reconnect_stream(
    chat_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Reconnect to an in-flight turn's SSE stream (replays buffered events first).

    Returns an empty, immediately-closed stream if no turn is running.
    """
    from app.agent.turn_runner import registry

    await _get_owned_chat(db, principal, chat_id)
    run = registry.get(chat_id)
    if run is None:
        async def _empty():
            if False:  # pragma: no cover - generator with no yields
                yield {}
            return

        return EventSourceResponse(_empty())
    return EventSourceResponse(_sse_from_run(run))


class CommandExec(BaseModel):
    command: str
    confirm: bool = False
    mode: str = "command"  # "command" (shell CLI) | "kql" (Azure Resource Graph query)


@router.post("/{chat_id}/exec/stream")
async def exec_command_stream(
    chat_id: str,
    payload: CommandExec,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Execute an allowlisted CLI command (or a KQL/Resource-Graph query) on the host,
    bound to the chat's Azure connection identity, streaming output live over SSE.
    Admin only.
    """
    from app.core.app_settings import load_settings
    from app.core.azure_connections import resolve_connection
    from app.exec.command_runner import run_command_stream, run_kql_stream

    chat = await _get_owned_chat(db, principal, chat_id)

    app_settings = load_settings()
    if not app_settings.get("command_execution_enabled", False):
        raise HTTPException(status_code=403, detail="Command execution is disabled.")

    conn = resolve_connection(chat.connection_id)
    mcp_read_only = bool(app_settings.get("mcp_read_only", True))
    read_only = bool(conn.get("read_only", mcp_read_only)) if conn else mcp_read_only

    tenant_id = principal.tenant_id
    actor_id = principal.subject
    command = payload.command
    confirm = payload.confirm
    is_kql = payload.mode == "kql"

    async def _gen():
        exit_code: int | None = None
        had_error = False
        if is_kql:
            runner = run_kql_stream(command, conn)
        else:
            runner = run_command_stream(command, conn, read_only=read_only, confirm=confirm)
        async for ev in runner:
            ev_type = ev.pop("type")
            if ev_type == "exit":
                exit_code = ev.get("code")
            elif ev_type == "error":
                had_error = True
            yield {"event": ev_type, "data": json.dumps(ev)}
        yield {"event": "done", "data": "{}"}
        # Audit the run (best-effort; a fresh session since the request one is closed).
        try:
            async with SessionLocal() as audit_db:
                audit_db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action="command.kql" if is_kql else "command.exec",
                        target=chat_id,
                        metadata_json={
                            "command": command[:500],
                            "exit_code": exit_code,
                            "error": had_error,
                            "connection_id": conn.get("id") if conn else None,
                        },
                    )
                )
                await audit_db.commit()
        except Exception:  # noqa: BLE001 - auditing must never break the response
            logger.exception("Failed to audit command execution")

    return EventSourceResponse(_gen())



