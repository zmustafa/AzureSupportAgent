"""Federated people-picker for assigning owners.

Sources, richest first (all already present in the app):

1. **App users** provisioned via SSO — the ``users`` table records ``auth_source``
   (local/oidc/saml), ``external_idp`` and ``external_id`` (the OIDC ``sub``). These are
   real, authenticated identities. The signed-in user gets a one-click *"assign me"*.
2. **Live Entra search** — the vendored EntraID MCP ``search_users`` tool (displayName /
   UPN / mail / given/sur name). Finds people who never signed into this app. Requires an
   Azure connection that can drive Microsoft Graph (service principal / managed identity).
3. **Manual free-text** — always available; the API turns a typed name/email into an owner.

All search functions are READ-ONLY, defensive (a directory failure degrades to the other
sources), and tenant-scoped. Each result is normalised to a :class:`DirectoryHit` dict that
the API materialises into an owner record + ``OwnerRef`` linkage on selection."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User

log = logging.getLogger("app.ownership.directory")


def _hit(
    *,
    source: str,
    display_name: str,
    email: str = "",
    kind: str = "person",
    user_id: str = "",
    idp_id: str = "",
    external_id: str = "",
    entra_object_id: str = "",
    upn: str = "",
    group_id: str = "",
) -> dict[str, Any]:
    """One normalised picker result. ``link`` carries whatever directory coordinates the
    source could supply; the API stores them on the owner so notify / leaver-detection /
    group-expansion can use them later."""
    return {
        "source": source,                      # app_user | entra | oidc_group | rbac | manual
        "kind": kind,                          # person | team | service
        "display_name": display_name,
        "email": email or upn,
        "link": {
            k: v for k, v in {
                "user_id": user_id,
                "idp_id": idp_id,
                "external_id": external_id,
                "entra_object_id": entra_object_id,
                "upn": upn,
                "group_id": group_id,
            }.items() if v
        },
    }


# ----------------------------------------------------------------- app users (SSO + local)
async def search_app_users(
    db: AsyncSession, tenant_id: str, q: str, *, sso_only: bool = False, limit: int = 25
) -> list[dict[str, Any]]:
    """App ``User`` rows matching ``q`` on email/username/display_name. With ``sso_only``
    restricts to OIDC/SAML-provisioned accounts (the federated-identity ask)."""
    stmt = select(User).where(User.tenant_id == (tenant_id or "default"))
    if sso_only:
        stmt = stmt.where(User.auth_source.in_(("oidc", "saml")))
    term = (q or "").strip().lower()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(
            or_(
                User.email.ilike(like),
                User.username.ilike(like),
                User.display_name.ilike(like),
            )
        )
    stmt = stmt.limit(max(1, min(int(limit), 100)))
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for u in rows:
        out.append(
            _hit(
                source="app_user",
                display_name=u.display_name or u.username,
                email=u.email,
                user_id=u.id,
                idp_id=u.external_idp or "",
                external_id=u.external_id or "",
                upn=u.email,
            )
        )
    return out


# ----------------------------------------------------------------- live Entra directory
async def search_entra(connection: dict[str, Any] | None, q: str, *, limit: int = 15) -> tuple[list[dict[str, Any]], str]:
    """Live Microsoft Graph user search via the EntraID MCP ``search_users`` tool.

    Returns ``(hits, note)``. ``note`` is a non-empty, human-readable reason when the
    directory couldn't be searched (no/incompatible connection, Graph error) so the API can
    show it without failing the whole picker (the app-user + manual sources still work)."""
    term = (q or "").strip()
    if len(term) < 2:
        return [], ""
    from app.core.config import get_settings
    from app.mcp.client import build_entra_mcp_client, entra_graph_config_error, unwrap_exc_message

    cfg_err = entra_graph_config_error(connection)
    if cfg_err:
        return [], cfg_err

    from app.identity.collector import _tool_result_json

    settings = get_settings()
    client = build_entra_mcp_client(settings, connection=connection)
    try:
        raw = _tool_result_json(
            await client.call_tool("search_users", {"query": term, "limit": max(1, min(int(limit), 50))})
        )
    except Exception as exc:  # noqa: BLE001
        return [], unwrap_exc_message(exc)[:300]
    finally:
        client.close()

    hits: list[dict[str, Any]] = []
    for u in raw if isinstance(raw, list) else []:
        if not isinstance(u, dict):
            continue
        oid = str(u.get("id") or "")
        upn = u.get("userPrincipalName") or u.get("mail") or ""
        name = u.get("displayName") or upn or oid
        hits.append(
            _hit(
                source="entra",
                display_name=name,
                email=u.get("mail") or upn,
                entra_object_id=oid,
                upn=upn,
            )
        )
    return hits, ""


# ----------------------------------------------------------------- combined picker
async def search_directory(
    db: AsyncSession,
    connection: dict[str, Any] | None,
    tenant_id: str,
    q: str,
    *,
    include_entra: bool = True,
    limit: int = 25,
) -> dict[str, Any]:
    """Unified people-picker: app SSO/local users + live Entra results, de-duplicated by
    email/UPN (app-user entries win so the picker prefers an already-linked identity).
    Always returns 200 with whatever sources succeeded + per-source notes."""
    notes: dict[str, str] = {}
    app_hits = await search_app_users(db, tenant_id, q, limit=limit)
    entra_hits: list[dict[str, Any]] = []
    if include_entra:
        try:
            entra_hits, note = await search_entra(connection, q, limit=limit)
            if note:
                notes["entra"] = note
        except Exception as exc:  # noqa: BLE001
            notes["entra"] = str(exc)[:200]
            log.info("directory entra search failed: %s", exc)

    seen_emails = {h["email"].lower() for h in app_hits if h.get("email")}
    merged = list(app_hits)
    for h in entra_hits:
        em = (h.get("email") or "").lower()
        if em and em in seen_emails:
            continue
        if em:
            seen_emails.add(em)
        merged.append(h)
    return {
        "query": q,
        "results": merged,
        "counts": {"app_users": len(app_hits), "entra": len(entra_hits)},
        "notes": notes,
    }
