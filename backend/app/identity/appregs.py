"""Entra ID **App Registrations** snapshot — a current-state inventory of application
registrations with their credentials (secrets + certs), API permissions (Application vs
Delegated, with high-risk flagging) and owners.

This powers the *Application Registrations* tab on the Identity screen: an inventory-style,
filterable grid that answers "what app registrations exist, how many secrets/certs does
each have, how many Application vs Delegated permissions, which are high-risk, and who owns
them".

Like the other proactive dashboards, the heavy data pull is **server-side cached** (see
``appregs_cache``) and only recomputed on an explicit refresh.

Two data paths:

* **Real** — when an Entra connection is configured, best-effort enumerate applications via
  the EntraID MCP server (``list_applications`` + ``get_application_by_id``) and normalise
  the Graph shapes (``passwordCredentials``/``keyCredentials``/``requiredResourceAccess``/
  owners). Any failure falls back to the demo dataset so the screen is never blank locally.
* **Demo** — a rich, deterministic dummy dataset (no Azure required) so the grid + filters
  can be exercised locally.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger("app.identity.appregs")

# A progress callback used while building the snapshot: progress(level, message).
# level ∈ {"info", "ok", "warn", "error"}.
ProgressFn = Callable[[str, str], Awaitable[None]]


# --------------------------------------------------------------------------- risk model
# Microsoft Graph permission values that grant broad, tenant-wide or write access. Used to
# flag an app registration as "high risk" and to drive the high-risk facet/filter.
HIGH_RISK_PERMISSIONS: set[str] = {
    "Directory.ReadWrite.All",
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "RoleManagement.ReadWrite.Directory",
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
    "GroupMember.ReadWrite.All",
    "Mail.ReadWrite",
    "Mail.Send",
    "Files.ReadWrite.All",
    "Sites.FullControl.All",
    "PrivilegedAccess.ReadWrite.AzureAD",
    "Policy.ReadWrite.ConditionalAccess",
    "DeviceManagementConfiguration.ReadWrite.All",
}

# Read-only-but-broad permissions worth surfacing as "medium" risk.
MEDIUM_RISK_PERMISSIONS: set[str] = {
    "Directory.Read.All",
    "Application.Read.All",
    "User.Read.All",
    "Group.Read.All",
    "Mail.Read",
    "AuditLog.Read.All",
    "Policy.Read.All",
    "Files.Read.All",
    "Sites.Read.All",
}


def permission_risk(value: str) -> str:
    """Risk tier for a permission value: ``high`` | ``medium`` | ``low``."""
    if value in HIGH_RISK_PERMISSIONS:
        return "high"
    if value in MEDIUM_RISK_PERMISSIONS:
        return "medium"
    return "low"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - _now()).total_seconds() // 86400)


# --------------------------------------------------------------------------- normalise
def _normalise_app(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a single app (demo or Graph-shaped) onto the grid row contract.

    Computes credential counts + soonest expiry, splits permissions into Application vs
    Delegated, flags high-risk, and derives the owner/ownerless state."""
    creds_in = raw.get("credentials") or []
    secrets = [c for c in creds_in if c.get("type") == "secret"]
    certs = [c for c in creds_in if c.get("type") == "certificate"]

    credentials: list[dict[str, Any]] = []
    expiry_days: list[int] = []
    expired = 0
    for c in creds_in:
        d = c.get("daysUntilExpiry")
        if d is None:
            d = _days_until(c.get("endDateTime"))
        if isinstance(d, int):
            expiry_days.append(d)
            if d < 0:
                expired += 1
        credentials.append(
            {
                "type": c.get("type", "secret"),
                "displayName": c.get("displayName") or "",
                "endDateTime": c.get("endDateTime"),
                "daysUntilExpiry": d,
            }
        )
    next_expiry = min(expiry_days) if expiry_days else None

    perms_in = raw.get("permissions") or []
    permissions: list[dict[str, Any]] = []
    app_perms = 0
    del_perms = 0
    high_risk = False
    for p in perms_in:
        ptype = p.get("type") or "Application"
        value = p.get("value") or ""
        risk = p.get("risk") or permission_risk(value)
        if risk == "high":
            high_risk = True
        if ptype == "Delegated":
            del_perms += 1
        else:
            app_perms += 1
        permissions.append(
            {"api": p.get("api") or "Microsoft Graph", "value": value, "type": ptype, "risk": risk}
        )

    owners = [o for o in (raw.get("owners") or []) if o]

    return {
        "id": raw.get("id") or "",
        "appId": raw.get("appId") or "",
        "displayName": raw.get("displayName") or "(unnamed)",
        "signInAudience": raw.get("signInAudience") or "AzureADMyOrg",
        "createdDateTime": raw.get("createdDateTime"),
        "publisherDomain": raw.get("publisherDomain") or "",
        "tags": list(raw.get("tags") or []),
        "secretsCount": len(secrets),
        "certsCount": len(certs),
        "credentials": credentials,
        "nextExpiryDays": next_expiry,
        "expiredCredentials": expired,
        "applicationPermissionsCount": app_perms,
        "delegatedPermissionsCount": del_perms,
        "permissions": permissions,
        "owners": owners,
        "ownerless": len(owners) == 0,
        "highRisk": high_risk,
    }


# --------------------------------------------------------------------------- aggregate
def aggregate(apps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build facet option counts + the summary KPIs from normalised app rows."""
    audiences: dict[str, int] = {}
    perms: dict[str, int] = {}
    owners: dict[str, int] = {}
    summary = {
        "total": len(apps),
        "withSecrets": 0,
        "withCerts": 0,
        "expiringSoon": 0,  # any credential within 30 days (not yet expired)
        "expired": 0,
        "highRisk": 0,
        "ownerless": 0,
        "applicationPerms": 0,
        "delegatedPerms": 0,
    }
    for a in apps:
        audiences[a["signInAudience"]] = audiences.get(a["signInAudience"], 0) + 1
        if a["secretsCount"]:
            summary["withSecrets"] += 1
        if a["certsCount"]:
            summary["withCerts"] += 1
        nx = a.get("nextExpiryDays")
        if isinstance(nx, int) and 0 <= nx <= 30:
            summary["expiringSoon"] += 1
        if a.get("expiredCredentials"):
            summary["expired"] += 1
        if a["highRisk"]:
            summary["highRisk"] += 1
        if a["ownerless"]:
            summary["ownerless"] += 1
            owners["(ownerless)"] = owners.get("(ownerless)", 0) + 1
        summary["applicationPerms"] += a["applicationPermissionsCount"]
        summary["delegatedPerms"] += a["delegatedPermissionsCount"]
        for p in a["permissions"]:
            if p["value"]:
                perms[p["value"]] = perms.get(p["value"], 0) + 1
        for o in a["owners"]:
            owners[o] = owners.get(o, 0) + 1

    def _facet(d: dict[str, int]) -> list[dict[str, Any]]:
        return [{"value": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "audiences": _facet(audiences),
        "permissions": _facet(perms),
        "owners": _facet(owners),
        "summary": summary,
    }


# --------------------------------------------------------------------------- demo data
def _iso_in(days: int) -> str:
    return (_now() + timedelta(days=days)).isoformat()


def _created(days_ago: int) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


def build_demo_app_registrations() -> list[dict[str, Any]]:
    """A deterministic, varied dummy set of app registrations for local review."""
    raw: list[dict[str, Any]] = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "appId": "a0000001-0000-0000-0000-000000000001",
            "displayName": "Contoso Payments API",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(420),
            "publisherDomain": "contoso.com",
            "tags": ["production", "pci"],
            "credentials": [
                {"type": "secret", "displayName": "rotated-2025", "endDateTime": _iso_in(12)},
                {"type": "certificate", "displayName": "signing-cert", "endDateTime": _iso_in(210)},
            ],
            "permissions": [
                {"value": "Directory.ReadWrite.All", "type": "Application"},
                {"value": "User.Read.All", "type": "Application"},
                {"value": "Mail.Send", "type": "Application"},
                {"value": "openid", "type": "Delegated"},
                {"value": "profile", "type": "Delegated"},
            ],
            "owners": ["Aisha Khan", "Diego Alvarez"],
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "appId": "a0000002-0000-0000-0000-000000000002",
            "displayName": "HR Self-Service Portal",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(900),
            "publisherDomain": "contoso.com",
            "tags": ["production"],
            "credentials": [
                {"type": "secret", "displayName": "portal-secret", "endDateTime": _iso_in(-5)},
            ],
            "permissions": [
                {"value": "User.Read", "type": "Delegated"},
                {"value": "User.ReadBasic.All", "type": "Delegated"},
                {"value": "Group.Read.All", "type": "Delegated"},
            ],
            "owners": ["Priya Nair"],
        },
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "appId": "a0000003-0000-0000-0000-000000000003",
            "displayName": "Legacy Migration Tool",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(1500),
            "publisherDomain": "contoso.com",
            "tags": ["legacy"],
            "credentials": [
                {"type": "secret", "displayName": "old-secret-1", "endDateTime": _iso_in(-120)},
                {"type": "secret", "displayName": "old-secret-2", "endDateTime": _iso_in(-30)},
            ],
            "permissions": [
                {"value": "Application.ReadWrite.All", "type": "Application"},
                {"value": "RoleManagement.ReadWrite.Directory", "type": "Application"},
                {"value": "Group.ReadWrite.All", "type": "Application"},
            ],
            "owners": [],  # ownerless + high-risk + expired → worst case
        },
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "appId": "a0000004-0000-0000-0000-000000000004",
            "displayName": "Marketing Analytics Connector",
            "signInAudience": "AzureADMultipleOrgs",
            "createdDateTime": _created(220),
            "publisherDomain": "contoso.com",
            "tags": ["multi-tenant", "saas"],
            "credentials": [
                {"type": "certificate", "displayName": "ml-cert", "endDateTime": _iso_in(25)},
            ],
            "permissions": [
                {"value": "Reports.Read.All", "type": "Application"},
                {"value": "User.Read", "type": "Delegated"},
                {"value": "offline_access", "type": "Delegated"},
            ],
            "owners": ["Tom Becker"],
        },
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "appId": "a0000005-0000-0000-0000-000000000005",
            "displayName": "DevOps Automation Runner",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(75),
            "publisherDomain": "contoso.com",
            "tags": ["automation", "production"],
            "credentials": [
                {"type": "secret", "displayName": "ci-secret", "endDateTime": _iso_in(48)},
                {"type": "certificate", "displayName": "deploy-cert", "endDateTime": _iso_in(330)},
            ],
            "permissions": [
                {"value": "Application.ReadWrite.All", "type": "Application"},
                {"value": "AppRoleAssignment.ReadWrite.All", "type": "Application"},
                {"value": "Directory.Read.All", "type": "Application"},
            ],
            "owners": ["Aisha Khan"],
        },
        {
            "id": "66666666-6666-6666-6666-666666666666",
            "appId": "a0000006-0000-0000-0000-000000000006",
            "displayName": "Customer Support Bot",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(140),
            "publisherDomain": "contoso.com",
            "tags": ["bot"],
            "credentials": [
                {"type": "secret", "displayName": "bot-secret", "endDateTime": _iso_in(180)},
            ],
            "permissions": [
                {"value": "Chat.ReadWrite", "type": "Delegated"},
                {"value": "User.Read", "type": "Delegated"},
                {"value": "Mail.ReadWrite", "type": "Application"},
            ],
            "owners": ["Sven Olsen", "Priya Nair"],
        },
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "appId": "a0000007-0000-0000-0000-000000000007",
            "displayName": "Field Service Mobile",
            "signInAudience": "AzureADandPersonalMicrosoftAccount",
            "createdDateTime": _created(60),
            "publisherDomain": "contoso.com",
            "tags": ["mobile", "public-client"],
            "credentials": [],  # public client — no creds
            "permissions": [
                {"value": "User.Read", "type": "Delegated"},
                {"value": "Calendars.ReadWrite", "type": "Delegated"},
                {"value": "offline_access", "type": "Delegated"},
            ],
            "owners": ["Diego Alvarez"],
        },
        {
            "id": "88888888-8888-8888-8888-888888888888",
            "appId": "a0000008-0000-0000-0000-000000000008",
            "displayName": "Security Audit Scanner",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(310),
            "publisherDomain": "contoso.com",
            "tags": ["security", "production"],
            "credentials": [
                {"type": "certificate", "displayName": "audit-cert", "endDateTime": _iso_in(9)},
            ],
            "permissions": [
                {"value": "AuditLog.Read.All", "type": "Application"},
                {"value": "Directory.Read.All", "type": "Application"},
                {"value": "Policy.Read.All", "type": "Application"},
                {"value": "SecurityEvents.Read.All", "type": "Application"},
            ],
            "owners": ["Security Team"],
        },
        {
            "id": "99999999-9999-9999-9999-999999999999",
            "appId": "a0000009-0000-0000-0000-000000000009",
            "displayName": "Partner B2B Gateway",
            "signInAudience": "AzureADMultipleOrgs",
            "createdDateTime": _created(540),
            "publisherDomain": "partner.example",
            "tags": ["multi-tenant", "b2b"],
            "credentials": [
                {"type": "secret", "displayName": "gw-secret", "endDateTime": _iso_in(70)},
                {"type": "secret", "displayName": "gw-secret-backup", "endDateTime": _iso_in(70)},
            ],
            "permissions": [
                {"value": "User.Read.All", "type": "Application"},
                {"value": "Group.Read.All", "type": "Application"},
            ],
            "owners": [],
        },
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "appId": "a0000010-0000-0000-0000-000000000010",
            "displayName": "Internal Wiki SSO",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(800),
            "publisherDomain": "contoso.com",
            "tags": ["sso"],
            "credentials": [
                {"type": "certificate", "displayName": "saml-cert", "endDateTime": _iso_in(400)},
            ],
            "permissions": [
                {"value": "openid", "type": "Delegated"},
                {"value": "profile", "type": "Delegated"},
                {"value": "email", "type": "Delegated"},
            ],
            "owners": ["Tom Becker", "Sven Olsen"],
        },
        {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "appId": "a0000011-0000-0000-0000-000000000011",
            "displayName": "Finance Data Exporter",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(95),
            "publisherDomain": "contoso.com",
            "tags": ["finance", "production"],
            "credentials": [
                {"type": "secret", "displayName": "export-secret", "endDateTime": _iso_in(3)},
            ],
            "permissions": [
                {"value": "Files.ReadWrite.All", "type": "Application"},
                {"value": "Sites.Read.All", "type": "Application"},
            ],
            "owners": ["Priya Nair"],
        },
        {
            "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "appId": "a0000012-0000-0000-0000-000000000012",
            "displayName": "Conditional Access Manager",
            "signInAudience": "AzureADMyOrg",
            "createdDateTime": _created(33),
            "publisherDomain": "contoso.com",
            "tags": ["security", "production"],
            "credentials": [
                {"type": "certificate", "displayName": "ca-cert", "endDateTime": _iso_in(150)},
            ],
            "permissions": [
                {"value": "Policy.ReadWrite.ConditionalAccess", "type": "Application"},
                {"value": "Application.Read.All", "type": "Application"},
                {"value": "Directory.Read.All", "type": "Delegated"},
            ],
            "owners": ["Security Team"],
        },
    ]
    return [_normalise_app(r) for r in raw]


# --------------------------------------------------------------------------- real (MCP)
def _tool_result_json(result: dict[str, Any]) -> Any:
    content = result.get("content") or []
    if result.get("isError"):
        msg = "\n".join(str(p) for p in content).strip()
        raise RuntimeError(msg[:500] or "EntraID tool returned an error.")
    joined = "".join(p for p in content if isinstance(p, str)).strip()
    if joined:
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            pass
    for part in content:
        if isinstance(part, str):
            try:
                return json.loads(part)
            except (ValueError, TypeError):
                continue
    return []


def _perms_from_graph_app(detail: dict[str, Any], resolver: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Best-effort permission extraction from a Graph application's
    ``requiredResourceAccess`` block. ``type == 'Role'`` → Application, ``'Scope'`` →
    Delegated. Resolves the permission *id* (a GUID) to its friendly value via ``resolver``
    (a GUID→value map from the Microsoft Graph service principal) when available."""
    resolver = resolver or {}
    out: list[dict[str, Any]] = []
    for rra in detail.get("requiredResourceAccess") or []:
        for ra in rra.get("resourceAccess") or []:
            ptype = "Application" if ra.get("type") == "Role" else "Delegated"
            gid = ra.get("id") or ""
            value = resolver.get(gid) or ra.get("value") or gid
            out.append({"value": value, "type": ptype})
    return out


def _creds_from_graph_app(detail: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pw in detail.get("passwordCredentials") or []:
        out.append(
            {"type": "secret", "displayName": pw.get("displayName") or "", "endDateTime": pw.get("endDateTime")}
        )
    for kc in detail.get("keyCredentials") or []:
        out.append(
            {"type": "certificate", "displayName": kc.get("displayName") or "", "endDateTime": kc.get("endDateTime")}
        )
    return out


async def _graph_permission_resolver(client) -> dict[str, str]:
    """Build a GUID→friendly-value map from the Microsoft Graph service principal's
    appRoles + oauth2PermissionScopes (so ``Directory.ReadWrite.All`` shows instead of a
    GUID). Best-effort: returns an empty map on any failure."""
    try:
        data = _tool_result_json(await client.call_tool("get_all_graph_permissions", {}))
    except Exception as exc:  # noqa: BLE001 - resolver is best-effort
        log.info("get_all_graph_permissions failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("delegated_permissions", "application_permissions"):
        for p in data.get(key) or []:
            gid = p.get("id")
            value = p.get("value")
            if gid and value:
                out[str(gid)] = str(value)
    return out


async def _collect_real(
    connection: dict[str, Any], *, limit: int, progress: "ProgressFn | None" = None
) -> list[dict[str, Any]]:
    """Best-effort real enumeration via the EntraID MCP server. Raises on any hard failure
    so the caller can fall back to demo data. Emits granular ``progress`` lines (this can take
    10–30 minutes against a large tenant, so the UI streams every step)."""
    from app.core.config import get_settings
    from app.mcp.client import build_entra_mcp_client

    async def _say(level: str, message: str) -> None:
        if progress is not None:
            await progress(level, message)

    settings = get_settings()
    await _say("info", "Connecting to Microsoft Entra (Graph) via the EntraID MCP server…")
    client = build_entra_mcp_client(settings, connection=connection)
    try:
        await _say("info", "Loading the Microsoft Graph permission catalog (appRoles + delegated scopes)…")
        resolver = await _graph_permission_resolver(client)
        await _say("info", f"Permission catalog loaded — {len(resolver)} permission id(s) resolvable to friendly names.")

        await _say("info", f"Listing application registrations (up to {limit})… this is the slow step on large tenants.")
        listing = _tool_result_json(await client.call_tool("list_applications", {"limit": limit}))
        apps_raw = listing if isinstance(listing, list) else []
        total = min(len(apps_raw), limit)
        await _say("info", f"Fetched {total} application registration(s). Processing each one…")

        out: list[dict[str, Any]] = []
        for i, app in enumerate(apps_raw[:limit], start=1):
            owners = []
            for o in app.get("owners") or []:
                if isinstance(o, dict):
                    owners.append(o.get("displayName") or o.get("userPrincipalName") or "")
                elif isinstance(o, str):
                    owners.append(o)
            norm = _normalise_app(
                {
                    "id": app.get("id"),
                    "appId": app.get("appId"),
                    "displayName": app.get("displayName"),
                    "signInAudience": app.get("signInAudience"),
                    "createdDateTime": app.get("createdDateTime"),
                    "publisherDomain": app.get("publisherDomain"),
                    "tags": app.get("tags") or [],
                    "credentials": _creds_from_graph_app(app),
                    "permissions": _perms_from_graph_app(app, resolver),
                    "owners": [o for o in owners if o],
                }
            )
            out.append(norm)
            risk = " · HIGH RISK" if norm["highRisk"] else ""
            owner_txt = f"{len(norm['owners'])} owner(s)" if norm["owners"] else "ownerless"
            await _say(
                "ok" if not norm["highRisk"] else "warn",
                f"[{i}/{total}] {norm['displayName']} — {norm['secretsCount']} secret(s), "
                f"{norm['certsCount']} cert(s), {norm['applicationPermissionsCount']} app + "
                f"{norm['delegatedPermissionsCount']} delegated perm(s), {owner_txt}{risk}",
            )
        return out
    finally:
        client.close()


# --------------------------------------------------------------------------- orchestrator
async def collect_app_registrations(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    limit: int = 200,
    progress: "ProgressFn | None" = None,
) -> dict[str, Any]:
    """Build the full app-registrations snapshot. Never raises — falls back to demo data.
    Emits granular ``progress(level, message)`` lines (the live enumeration can take 10–30
    minutes on a large tenant)."""
    async def _say(level: str, message: str) -> None:
        if progress is not None:
            await progress(level, message)

    source = "demo_dummy_data"
    note = ""
    apps: list[dict[str, Any]] = []
    if connection is not None:
        from app.mcp.client import entra_graph_config_error, unwrap_exc_message

        cfg_err = entra_graph_config_error(connection)
        if cfg_err:
            # The Graph MCP can't authenticate with this connection — don't spawn a doomed
            # server; show the clear, actionable reason and fall back to demo data.
            note = cfg_err
            log.info("app-registrations: Graph MCP not usable with this connection: %s", cfg_err)
            await _say("error", cfg_err)
        else:
            try:
                apps = await _collect_real(connection, limit=limit, progress=progress)
                source = "microsoft_graph"
            except Exception as exc:  # noqa: BLE001 - graceful fallback to demo
                note = f"Live enumeration failed, showing demo data: {unwrap_exc_message(exc)[:200]}"
                log.info("app-registrations live collect failed: %s", exc)
                await _say("error", note)
                apps = []
    if not apps:
        apps = build_demo_app_registrations()
        if connection is None:
            note = note or "No Entra connection configured — showing demo data."
            await _say("warn", note)
    await _say("info", "Aggregating facets (audiences, permissions, owners) and summary KPIs…")
    apps.sort(key=lambda a: a["displayName"].lower())
    agg = aggregate(apps)
    await _say("ok", f"Snapshot complete — {len(apps)} app registration(s).")
    return {
        "generated_at": _now_iso(),
        "tenant_id": tenant_id,
        "connection_configured": connection is not None,
        "source": source,
        "note": note,
        "apps": apps,
        "facets": {"audiences": agg["audiences"], "permissions": agg["permissions"], "owners": agg["owners"]},
        "summary": agg["summary"],
    }


def build_demo_snapshot(tenant_id: str = "default") -> dict[str, Any]:
    """Build the App Registrations demo snapshot synchronously (no Graph/connection).

    Mirrors the demo fallback inside ``build_snapshot`` so the admin 'Load demo data' button
    can pre-seed the cache without spinning up the Graph MCP."""
    apps = build_demo_app_registrations()
    apps.sort(key=lambda a: a["displayName"].lower())
    agg = aggregate(apps)
    return {
        "generated_at": _now_iso(),
        "tenant_id": tenant_id,
        "connection_configured": False,
        "source": "demo_dummy_data",
        "note": "Demo data — not a live Entra enumeration.",
        "apps": apps,
        "facets": {"audiences": agg["audiences"], "permissions": agg["permissions"], "owners": agg["owners"]},
        "summary": agg["summary"],
    }


def seed_demo(tenant_id: str = "default") -> dict[str, Any]:
    """Seed the App Registrations demo snapshot into the cache (keyed by tenant + empty
    connection id, matching the no-connection read path). Returns the stored payload."""
    from app.identity import appregs_cache

    payload = build_demo_snapshot(tenant_id)
    appregs_cache.set_(tenant_id, "", payload)
    return payload
