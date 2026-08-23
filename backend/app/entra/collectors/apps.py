"""Applications, service principals, consent grants and credentials collector.

Two things this does that the existing ``identity/appregs.py`` cannot:

1. **Granted, not merely requested.** ``requiredResourceAccess`` on an application is what
   the app *asks for*; ``appRoleAssignments`` is what it has actually *been granted*. The
   older collector reports the former as though it were the latter. Real risk lives in the
   granted set, so both are collected and never conflated.

2. **One call instead of N.** Rather than fanning ``/servicePrincipals/{id}/appRoleAssignments``
   out across thousands of principals, we query ``appRoleAssignedTo`` on the handful of
   *resource* service principals that matter (Microsoft Graph, Exchange, SharePoint, legacy
   AAD Graph). That returns every principal holding one of their application permissions in
   a single paged call each.

Permission names and risk tiers are resolved from the live Microsoft Graph service
principal (``appRoles`` + ``oauth2PermissionScopes``) rather than a hard-coded list, so new
permissions are named automatically and an unknown GUID is reported as unknown rather than
silently dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.core.signin_activity import failed_signin
from app.entra import model, signin_outcomes
from app.entra.collectors import CollectContext, as_dict, as_list, batch_collection, clip, guarded
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "apps"

MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
AAD_GRAPH_APP_ID = "00000002-0000-0000-c000-000000000000"
EXCHANGE_APP_ID = "00000002-0000-0ff1-ce00-000000000000"
SHAREPOINT_APP_ID = "00000003-0000-0ff1-ce00-000000000000"

RESOURCE_APP_IDS = {
    MS_GRAPH_APP_ID: "Microsoft Graph",
    AAD_GRAPH_APP_ID: "Azure AD Graph (legacy)",
    EXCHANGE_APP_ID: "Office 365 Exchange Online",
    SHAREPOINT_APP_ID: "Office 365 SharePoint Online",
}

# Microsoft's own publisher tenants — service principals owned by these are first-party.
MICROSOFT_TENANTS = {
    "f8cdef31-a31e-4b4a-93e4-5f571e91255a",
    "72f988bf-86f1-41af-91ab-2d7cd011db47",
}

_APP_SELECT = [
    "id", "appId", "displayName", "createdDateTime", "signInAudience", "identifierUris",
    "passwordCredentials", "keyCredentials", "web", "spa", "publicClient",
    "requiredResourceAccess", "appRoles", "tags", "notes", "api",
]
_SP_SELECT = [
    "id", "appId", "displayName", "servicePrincipalType", "accountEnabled",
    "appRoleAssignmentRequired", "publisherName", "verifiedPublisher", "homepage",
    "replyUrls", "tags", "appOwnerOrganizationId", "disabledByMicrosoftStatus",
    "passwordCredentials", "keyCredentials", "servicePrincipalNames", "preferredSingleSignOnMode",
    # A system-assigned managed identity carries the ARM id of the resource that owns it in
    # alternativeNames. It is the only link back from the principal to the thing it belongs to,
    # and without it "what is this managed identity?" has no answer but its display name.
    "alternativeNames",
]

# ---------------------------------------------------------------- permission tiering
# CRITICAL: can escalate to tenant control, or read/write all organizational content.
_CRITICAL_PERMS = {
    "RoleManagement.ReadWrite.Directory", "AppRoleAssignment.ReadWrite.All",
    "Application.ReadWrite.All", "Application.ReadWrite.OwnedBy", "Directory.ReadWrite.All",
    "PrivilegedAccess.ReadWrite.AzureAD", "PrivilegedAccess.ReadWrite.AzureADGroup",
    "PrivilegedAccess.ReadWrite.AzureResources", "PrivilegedEligibilitySchedule.ReadWrite.AzureADGroup",
    "RoleAssignmentSchedule.ReadWrite.Directory", "RoleEligibilitySchedule.ReadWrite.Directory",
    "RoleManagementPolicy.ReadWrite.Directory", "Policy.ReadWrite.ConditionalAccess",
    "Policy.ReadWrite.PermissionGrant", "Policy.ReadWrite.AuthenticationMethod",
    "Mail.ReadWrite", "Files.ReadWrite.All", "Sites.FullControl.All", "Sites.ReadWrite.All",
    "User.ReadWrite.All", "Group.ReadWrite.All", "GroupMember.ReadWrite.All",
    "UserAuthenticationMethod.ReadWrite.All", "User-PasswordProfile.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All", "Domain.ReadWrite.All",
    "Device.ReadWrite.All", "DeviceManagementConfiguration.ReadWrite.All",
    "Organization.ReadWrite.All", "Directory.AccessAsUser.All", "full_access_as_app",
}
# The tenant-takeover primitives: an app holding one of these can grant itself anything.
_CONSENT_GRANT_PERMS = {
    "AppRoleAssignment.ReadWrite.All", "RoleManagement.ReadWrite.Directory",
    "Application.ReadWrite.All", "Directory.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All", "Policy.ReadWrite.PermissionGrant",
}
# Tenant-wide reads of user *content* (as opposed to directory metadata).
_TENANT_WIDE_MAIL = {"Mail.Read", "Mail.ReadBasic", "Mail.ReadBasic.All", "Mail.ReadWrite", "MailboxSettings.Read",
                     "MailboxSettings.ReadWrite", "IMAP.AccessAsApp", "POP.AccessAsApp", "SMTP.SendAsApp"}
_TENANT_WIDE_FILES = {"Files.Read.All", "Files.ReadWrite.All", "Sites.Read.All", "Sites.ReadWrite.All",
                      "Sites.FullControl.All", "Sites.Manage.All"}
_TENANT_WIDE_CHAT = {"Chat.Read.All", "Chat.ReadWrite.All", "ChannelMessage.Read.All",
                     "Chat.ReadBasic.All", "TeamsActivity.Read.All", "OnlineMeetings.Read.All",
                     "CallRecords.Read.All"}
_HIGH_PERMS = (
    _TENANT_WIDE_MAIL | _TENANT_WIDE_FILES | _TENANT_WIDE_CHAT | {
        "Policy.Read.All", "Exchange.ManageAsApp", "SecurityEvents.ReadWrite.All",
        "IdentityRiskyUser.ReadWrite.All", "AuditLog.Read.All", "Notes.Read.All",
        "Calendars.ReadWrite", "Contacts.ReadWrite", "People.Read.All",
    }
)
_MEDIUM_PERMS = {
    "Directory.Read.All", "User.Read.All", "Group.Read.All", "GroupMember.Read.All",
    "Application.Read.All", "RoleManagement.Read.Directory", "Device.Read.All",
    "Organization.Read.All", "Reports.Read.All",
}

TIER_CRITICAL = "critical"
TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"


def permission_tier(name: str) -> str:
    if name in _CRITICAL_PERMS:
        return TIER_CRITICAL
    if name in _HIGH_PERMS:
        return TIER_HIGH
    if name in _MEDIUM_PERMS:
        return TIER_MEDIUM
    # Unrecognized ".ReadWrite.All" style permissions are treated as high, not ignored.
    if name.endswith(".ReadWrite.All") or name.endswith("ReadWrite.Directory"):
        return TIER_HIGH
    if name.endswith(".Read.All"):
        return TIER_MEDIUM
    return TIER_LOW


def is_consent_grant_capable(name: str) -> bool:
    return name in _CONSENT_GRANT_PERMS


def perm_flags(name: str) -> dict[str, bool]:
    return {
        "mail": name in _TENANT_WIDE_MAIL,
        "files": name in _TENANT_WIDE_FILES,
        "chat": name in _TENANT_WIDE_CHAT,
        "consent_grant": is_consent_grant_capable(name),
        "directory_write": name in {"Directory.ReadWrite.All", "Application.ReadWrite.All",
                                    "RoleManagement.ReadWrite.Directory"},
    }


# ------------------------------------------------------------------------ FIC issuers
# Compared as HOSTS, not as url prefixes. The previous form was a tuple of url prefixes
# fed to `str.startswith`, and only two of the nine carried a trailing "/" -- so
# `https://gitlab.com.evil.com/` and `https://token.actions.github.com.evil.com/` both
# classified as trusted. Storing bare hosts makes the trailing-delimiter mistake
# impossible to make again.
_TRUSTED_FIC_HOSTS = frozenset({
    "token.actions.githubusercontent.com",
    "vstoken.dev.azure.com",
    "login.microsoftonline.com",
    "sts.windows.net",
    "gitlab.com",
    "oidc.prod-aks.azure.com",
    "kubernetes.default.svc",
    "container.googleapis.com",
    "token.actions.github.com",
})

# AKS hands out per-region OIDC issuers as https://<region>.oic.prod-aks.azure.com/<...>,
# so this one is a genuine domain suffix rather than a fixed host. The leading dot is
# required: it is what stops `https://evil-oic.prod-aks.azure.com/` from matching.
_TRUSTED_FIC_HOST_SUFFIXES = (".oic.prod-aks.azure.com",)


def fic_trusted(issuer: str) -> bool:
    """True when a federated-identity-credential issuer is a known identity provider.

    Parses the url and compares the host. The previous implementation combined
    `startswith` on url prefixes with `".oic.prod-aks.azure.com" in iss`, so an issuer of
    `https://evil.com/.oic.prod-aks.azure.com/x` was reported as TRUSTED -- the substring
    appears anywhere in the string, including the path. That inverts the meaning of the
    only signal this collector publishes about FIC issuers, on exactly the input an
    attacker controls when they add a credential to an app registration. CodeQL
    `py/incomplete-url-substring-sanitization`.
    """
    try:
        parsed = urlparse((issuer or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in _TRUSTED_FIC_HOSTS or host.endswith(_TRUSTED_FIC_HOST_SUFFIXES)


# ------------------------------------------------------------------ redirect URI risk
def redirect_risk(uri: str) -> str:
    """Return a risk reason for a redirect URI, or "" when it looks fine."""
    u = (uri or "").strip()
    low = u.lower()
    if "*" in u:
        return "wildcard"
    if low.startswith("http://") and not (
        low.startswith("http://localhost") or low.startswith("http://127.0.0.1") or low.startswith("http://[::1]")
    ):
        return "plaintext-http"
    if low.startswith("http://localhost") or low.startswith("http://127.0.0.1"):
        return "localhost"
    return ""


# ------------------------------------------------------------------------ risk score
# Published with its components so the number is never a black box. Weights sum to 100.
RISK_COMPONENTS: list[dict[str, Any]] = [
    {"key": "permissions", "weight": 30, "label": "Granted permission tier"},
    {"key": "consent_grant", "weight": 15, "label": "Can grant itself permissions"},
    {"key": "credentials", "weight": 15, "label": "Credential hygiene"},
    {"key": "ownership", "weight": 10, "label": "Ownership"},
    {"key": "exposure", "weight": 10, "label": "Exposure (multi-tenant, publisher, redirect URIs)"},
    {"key": "azure_reach", "weight": 10, "label": "Azure control-plane reach"},
    {"key": "usage", "weight": 5, "label": "Usage and assignment breadth"},
    {"key": "ca_coverage", "weight": 5, "label": "Conditional Access coverage"},
]

_TIER_SCORE = {TIER_CRITICAL: 1.0, TIER_HIGH: 0.7, TIER_MEDIUM: 0.35, TIER_LOW: 0.0}

# Azure creates and owns these. They cannot be given an Entra owner, and their certificates
# are platform-rotated — an "expired" one is normal, not a finding. Scoring them on ownership
# and credential hygiene puts dozens of unactionable rows above the applications that matter.
_PLATFORM_MANAGED_SP_TYPES = frozenset({"ManagedIdentity"})


def is_platform_managed(sp: dict[str, Any]) -> bool:
    return str(sp.get("sp_type") or sp.get("service_principal_type") or "") in _PLATFORM_MANAGED_SP_TYPES


def risk_score(app: dict[str, Any], sp: dict[str, Any] | None, *, azure_roles: int = 0) -> dict[str, Any]:
    """0-100 risk for one application, with the contribution of every component.

    Sorting an inventory by this is the fastest route from "3,000 applications" to "look at
    these six first", which is the only way the screen is usable at production directory size.
    """
    sp = sp or {}
    platform = is_platform_managed(sp)
    perms = sp.get("granted_app_permissions") or []
    delegated = sp.get("granted_delegated") or []
    creds = (app.get("credentials") or []) + (sp.get("credentials") or [])

    tier_factor = max((_TIER_SCORE.get(p.get("tier", TIER_LOW), 0.0) for p in perms), default=0.0)
    tenant_wide = any(
        p.get("flags", {}).get(k) for p in perms for k in ("mail", "files", "chat")
    )
    scores: dict[str, float] = {
        "permissions": min(1.0, tier_factor * (1.25 if tenant_wide else 1.0)),
        "consent_grant": 1.0 if any(p.get("flags", {}).get("consent_grant") for p in perms) else 0.0,
        "credentials": 0.0 if platform else _credential_risk(creds),
        "ownership": 0.0 if platform else _ownership_risk(app, sp),
        "exposure": _exposure_risk(app, sp),
        "azure_reach": min(1.0, azure_roles / 3.0) if azure_roles else 0.0,
        "usage": _usage_risk(sp),
        "ca_coverage": 0.0,   # filled in by the API once the CA analysis is available
    }
    components = [
        {**c, "factor": round(scores.get(c["key"], 0.0), 3),
         "points": round(c["weight"] * scores.get(c["key"], 0.0), 1),
         **({"not_applicable": "Azure manages this identity's credentials and ownership."}
            if platform and c["key"] in ("credentials", "ownership") else {})}
        for c in RISK_COMPONENTS
    ]
    total = round(sum(c["points"] for c in components))
    if any(p.get("flags", {}).get("consent_grant") for p in perms):
        total = max(total, 80)   # a tenant-takeover primitive is never a low-risk application
    return {"score": min(100, total), "components": components, "platform_managed": platform,
            "delegated_all_principals": sum(1 for g in delegated if g.get("consent_type") == "AllPrincipals")}


def _credential_risk(creds: list[dict[str, Any]]) -> float:
    if not creds:
        return 0.0
    worst = 0.0
    for c in creds:
        if c.get("expired"):
            worst = max(worst, 0.8)
        days = c.get("days_left")
        if days is not None and 0 <= days <= 30:
            worst = max(worst, 0.5)
        lifetime = c.get("lifetime_days")
        if lifetime is not None and lifetime > 730:
            worst = max(worst, 0.6)
    active = [c for c in creds if not c.get("expired")]
    if len(active) > 2:
        worst = max(worst, 0.4)
    return min(1.0, worst)


def _ownership_risk(app: dict[str, Any], sp: dict[str, Any]) -> float:
    owners = (app.get("owner_ids") or []) + (sp.get("owner_ids") or [])
    known = app.get("owners_known") or sp.get("owners_known")
    if not known:
        return 0.3          # unknown ownership is not the same as no ownership
    return 0.0 if owners else 1.0


def _exposure_risk(app: dict[str, Any], sp: dict[str, Any]) -> float:
    score = 0.0
    if app.get("multi_tenant"):
        score += 0.4
    if app.get("multi_tenant") and not (app.get("verified_publisher") or sp.get("verified_publisher")):
        score += 0.3
    risky_uris = [r for r in app.get("redirect_uris") or [] if r.get("risk") in ("wildcard", "plaintext-http")]
    if risky_uris:
        score += 0.4
    if any(not f.get("trusted") or f.get("wildcard_subject") for f in app.get("federated_credentials") or []):
        score += 0.5
    return min(1.0, score)


def _usage_risk(sp: dict[str, Any]) -> float:
    assigned = int(sp.get("assigned_principals") or 0)
    if assigned >= 500:
        return 1.0
    if assigned >= 100:
        return 0.6
    if assigned >= 10:
        return 0.3
    return 0.0


def _azure_role_counts(tenant_id: str) -> dict[str, int]:
    """Azure control-plane roles per service principal, from the existing RBAC cache.

    Best-effort and read-only: an application that also holds Azure power is materially
    riskier, but a cold RBAC cache must never fail application collection.
    """
    try:
        from app.entra import azure_link

        link = azure_link.build(tenant_id)
    except Exception:  # noqa: BLE001 - enrichment only
        return {}
    if not link.get("available"):
        return {}
    return {pid: len(p.get("powerful_roles") or []) for pid, p in (link.get("principals") or {}).items()}


def _days_left(value: str, now: datetime) -> int | None:
    if not value:
        return None
    try:
        end = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return int((end - now).total_seconds() // 86400)


def _lifetime_days(start: str, end: str) -> int | None:
    try:
        s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int((e - s).total_seconds() // 86400)


def _credentials(raw: list[Any], kind: str, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw or []:
        c = as_dict(c)
        end = c.get("endDateTime", "") or ""
        start = c.get("startDateTime", "") or ""
        days = _days_left(end, now)
        out.append({
            "id": str(c.get("keyId") or c.get("customKeyIdentifier") or ""),
            "display_name": c.get("displayName", "") or "",
            "kind": kind,
            "start": start,
            "end": end,
            "days_left": days,
            "lifetime_days": _lifetime_days(start, end),
            "expired": days is not None and days < 0,
        })
    return out


SIGNIN_WINDOW_DAYS = 30


async def _signin_activity(client: GraphClient, ctx: CollectContext,
                           now: datetime) -> dict[str, Any]:
    """Which applications were actually signed into recently, and which were rejected.

    This exists to answer "is anyone USING the app nobody protects?". A gap on an application
    that has not been touched in a year is housekeeping; the same gap on one with daily traffic
    is live exposure, and the coverage matrix alone cannot tell them apart.

    Two sources, in order of authority:

    * ``/reports/servicePrincipalSignInActivities`` — one row per application, one call, but it
      does not separate a success from a rejected attempt and can serve a stale build.
    * ``/auditLogs/signIns`` scoped per application — carries ``status.errorCode``, so it is
      the only source that can say *failed*. One slow call per app, so it is read out of band
      and only its cached result is applied here.

    The return value ALWAYS carries ``measured``. When the tenant lacks the license or the
    permission for sign-in logs, the honest answer is "not measured", never an empty app list —
    an empty list renders as "nothing unattributed", which is the reassuring reading of missing
    data. The `ca.unattributed_apps` detector treats unmeasured as unavailable for the same
    reason.
    """
    since = (now - timedelta(days=SIGNIN_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await ctx.say("info", "Applications: sampling recent sign-in activity…")
    # Prefer the per-service-principal aggregate. Scanning /auditLogs/signIns instead means
    # paging every sign-in EVENT in the tenant — millions of rows for a large tenant — and then
    # truncating, which quietly turns "we stopped reading" into "this app is not used". The
    # aggregate is one row per application and cannot mislead that way.
    try:
        rows, truncated = await client.get_all(
            "/reports/servicePrincipalSignInActivities",
            top=999, max_items=ctx.max_apps, beta=True,
        )
        active: dict[str, str] = {}
        failed: dict[str, str] = {}
        for r in rows:
            r = as_dict(r)
            app_id = str(r.get("appId") or "").lower()
            if not app_id:
                continue
            activity = as_dict(r.get("lastSignInActivity"))
            # Graph does not emit `lastSuccessfulSignInDateTime` for service principals, so
            # the attempt stamp is the only signal this report offers. Prefer a real success
            # where one exists.
            last = str(activity.get("lastSuccessfulSignInDateTime")
                       or activity.get("lastSignInDateTime") or "")
            if last and last >= since:
                active[app_id] = last
            # Only set when Graph actually distinguishes the two. Without a premium licence
            # the sign-in logs that carry an error code are unreadable, so this stays empty
            # rather than branding every application as failing.
            rejected = failed_signin(
                str(activity.get("lastSignInDateTime") or ""),
                str(activity.get("lastSuccessfulSignInDateTime") or ""),
            ) if activity.get("lastSuccessfulSignInDateTime") else ""
            if rejected:
                failed[app_id] = rejected
        await ctx.say("ok", f"Applications: {len(active):,} app(s) with sign-in activity")
        block = {
            "measured": True,
            "source": "servicePrincipalSignInActivities",
            "window_days": SIGNIN_WINDOW_DAYS,
            "active_app_ids": sorted(active),
            "last_seen": active,
            "last_failed": failed,
            "complete": not truncated,
        }
    except Exception as exc:  # noqa: BLE001 - any Graph failure means "not measured"
        await ctx.say("warn", f"Applications: sign-in activity unavailable ({clip(str(exc), 120)})")
        block = {
            "measured": False,
            "reason": "Sign-in activity could not be read for this tenant. This usually means the "
                      "AuditLog.Read.All permission is missing, or the tenant has no Entra ID P1 "
                      "license. Applications with no policy coverage are still listed, but "
                      "whether anyone is signing into them is unknown.",
            "active_app_ids": [],
            "last_seen": {},
            "last_failed": {},
        }

    await _apply_cached_outcomes(ctx, block)
    return block


async def _apply_cached_outcomes(ctx: CollectContext, block: dict[str, Any]) -> None:
    """Overlay whatever per-event outcomes are already cached, in place.

    No Graph calls: the per-application reads are slow and mostly empty, so they run out of
    band after the refresh (`signin_outcomes.run_backfill`) and land here on the next one. The
    `outcomes` sub-block reports which state the failure column is actually in, so an empty
    cell is never rendered as a confident "no failures".
    """
    scope, _ttl, _cap, _budget = signin_outcomes.settings()
    if scope == signin_outcomes.SCOPE_OFF:
        block["outcomes"] = {
            "measured": False, "scope": scope,
            "reason": "Per-application sign-in outcomes are turned off, so a rejected sign-in "
                      "cannot be told apart from a successful one.",
        }
        return

    entries = signin_outcomes.read_cache(ctx.tenant_id)
    if not entries:
        block["outcomes"] = {
            "measured": False, "scope": scope, "pending": True,
            "reason": "Per-application sign-in outcomes have not been read yet. They are "
                      "collected in the background after a refresh.",
        }
        return

    by_app = signin_outcomes.cached_by_app(entries)
    signin_outcomes.merge_outcomes(block, by_app)
    block["measured"] = True
    block["source"] = "auditLogs/signIns + servicePrincipalSignInActivities"
    block["outcomes"] = {"measured": True, "reason": "", "scope": scope, "cached": len(entries)}
    await ctx.say("ok", f"Applications: {len(by_app):,} app(s) with a cached sign-in outcome")


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        truncated = False
        now = datetime.now(timezone.utc)

        # --- applications --------------------------------------------------------
        await ctx.say("info", "Applications: collecting app registrations…")
        apps_raw, app_trunc = await client.get_all(
            "/applications", select=_APP_SELECT, top=999, max_items=ctx.max_apps
        )
        truncated = truncated or app_trunc
        if app_trunc:
            notes.append(f"Application collection capped at {ctx.max_apps:,}.")
        await ctx.say("ok", f"Applications: {len(apps_raw):,} registration(s)")

        # --- service principals ---------------------------------------------------
        await ctx.say("info", "Applications: collecting service principals…")
        sps_raw, sp_trunc = await client.get_all(
            "/servicePrincipals", select=_SP_SELECT, top=999, max_items=ctx.max_apps
        )
        truncated = truncated or sp_trunc
        if sp_trunc:
            notes.append(f"Service-principal collection capped at {ctx.max_apps:,}.")
        await ctx.say("ok", f"Applications: {len(sps_raw):,} service principal(s)")

        sp_by_object: dict[str, dict[str, Any]] = {}
        sp_by_appid: dict[str, dict[str, Any]] = {}
        for s in sps_raw:
            s = as_dict(s)
            oid = str(s.get("id") or "")
            if not oid:
                continue
            app_id = str(s.get("appId") or "")
            owner_tenant = str(s.get("appOwnerOrganizationId") or "")
            vp = as_dict(s.get("verifiedPublisher"))
            reply = [str(r) for r in as_list(s.get("replyUrls"))]
            rec = {
                "object_id": oid,
                "app_id": app_id,
                "display_name": s.get("displayName", "") or "",
                "sp_type": s.get("servicePrincipalType", "") or "",
                "enabled": bool(s.get("accountEnabled")),
                "assignment_required": bool(s.get("appRoleAssignmentRequired")),
                "publisher_name": s.get("publisherName", "") or "",
                "verified_publisher": vp.get("displayName", "") or "",
                "app_owner_tenant_id": owner_tenant,
                "is_first_party": owner_tenant.lower() in MICROSOFT_TENANTS,
                "is_external": bool(owner_tenant) and owner_tenant.lower() != (ctx.tenant_id or "").lower()
                and owner_tenant.lower() not in MICROSOFT_TENANTS,
                "disabled_by_microsoft": s.get("disabledByMicrosoftStatus", "") or "",
                "sso_mode": s.get("preferredSingleSignOnMode", "") or "",
                "alternative_names": [str(n) for n in as_list(s.get("alternativeNames"))],
                "reply_urls": reply,
                "reply_url_risks": [
                    {"uri": u, "risk": redirect_risk(u)} for u in reply if redirect_risk(u)
                ],
                "credentials": _credentials(as_list(s.get("passwordCredentials")), "secret", now)
                + _credentials(as_list(s.get("keyCredentials")), "certificate", now),
                "owner_ids": [],
                "owners_known": False,
                "granted_app_permissions": [],
                "granted_delegated": [],
                "assigned_principals": 0,
                "assignment_known": False,
                "provisioning_jobs": [],
            }
            sp_by_object[oid] = rec
            if app_id:
                sp_by_appid[app_id] = rec

        # --- permission catalog (GUID -> name) ----------------------------------
        catalogue: dict[str, dict[str, str]] = {}
        for res_app_id, res_name in RESOURCE_APP_IDS.items():
            sp = sp_by_appid.get(res_app_id)
            if not sp:
                continue
            try:
                body = await client.get(
                    f"/servicePrincipals/{sp['object_id']}?$select=appRoles,oauth2PermissionScopes"
                )
            except GraphError as exc:
                notes.append(f"Permission catalogue for {res_name}: {clip(exc, 120)}")
                continue
            for role in as_list(body.get("appRoles")):
                role = as_dict(role)
                rid = str(role.get("id") or "")
                if rid:
                    catalogue[rid] = {"name": str(role.get("value") or ""), "resource": res_name,
                                      "kind": "application"}
            for scope in as_list(body.get("oauth2PermissionScopes")):
                scope = as_dict(scope)
                sid = str(scope.get("id") or "")
                if sid:
                    catalogue[sid] = {"name": str(scope.get("value") or ""), "resource": res_name,
                                      "kind": "delegated"}
        await ctx.say("ok", f"Applications: resolved {len(catalogue):,} permission name(s)")

        # --- granted application permissions (appRoleAssignedTo per resource) -----
        unknown_perm_ids = 0
        for res_app_id, res_name in RESOURCE_APP_IDS.items():
            sp = sp_by_appid.get(res_app_id)
            if not sp:
                continue
            try:
                grants, _ = await client.get_all(
                    f"/servicePrincipals/{sp['object_id']}/appRoleAssignedTo",
                    select=["id", "principalId", "principalType", "principalDisplayName", "appRoleId", "createdDateTime"],
                    top=999,
                )
            except GraphPermissionError as exc:
                notes.append(f"Granted permissions for {res_name}: not permitted ({clip(exc.message, 100)})")
                continue
            except GraphError as exc:
                notes.append(f"Granted permissions for {res_name}: {clip(exc, 120)}")
                continue
            for g in grants:
                g = as_dict(g)
                principal_id = str(g.get("principalId") or "")
                client_sp = sp_by_object.get(principal_id)
                if client_sp is None:
                    continue
                role_id = str(g.get("appRoleId") or "")
                meta = catalogue.get(role_id)
                name = meta["name"] if meta else ""
                if not name:
                    unknown_perm_ids += 1
                    name = f"unknown:{role_id[:8]}"
                client_sp["granted_app_permissions"].append({
                    "permission": name,
                    "permission_id": role_id,
                    "resource": res_name,
                    "resource_app_id": res_app_id,
                    "kind": "application",
                    "tier": permission_tier(name),
                    "flags": perm_flags(name),
                    "granted_at": g.get("createdDateTime", "") or "",
                    "known": bool(meta),
                })
        if unknown_perm_ids:
            notes.append(f"{unknown_perm_ids} granted permission id(s) could not be named — reported as unknown.")

        # --- delegated consent grants --------------------------------------------
        try:
            oauth_grants, _ = await client.get_all(
                "/oauth2PermissionGrants",
                select=["id", "clientId", "consentType", "principalId", "resourceId", "scope"],
                top=999,
            )
        except GraphPermissionError as exc:
            oauth_grants = []
            notes.append(f"Delegated consent grants not permitted ({clip(exc.message, 100)}).")
        except GraphError as exc:
            oauth_grants = []
            notes.append(f"Delegated consent grants: {clip(exc, 120)}")

        all_principals_grants = 0
        for g in oauth_grants:
            g = as_dict(g)
            client_sp = sp_by_object.get(str(g.get("clientId") or ""))
            if client_sp is None:
                continue
            resource_sp = sp_by_object.get(str(g.get("resourceId") or ""))
            consent_type = str(g.get("consentType") or "")
            scopes = [s for s in str(g.get("scope") or "").split(" ") if s]
            if consent_type == "AllPrincipals":
                all_principals_grants += 1
            client_sp["granted_delegated"].append({
                "id": str(g.get("id") or ""),
                "resource": (resource_sp or {}).get("display_name", "") or "",
                "resource_id": str(g.get("resourceId") or ""),
                "consent_type": consent_type,
                "principal_id": str(g.get("principalId") or ""),
                "scopes": scopes,
                "max_tier": max((permission_tier(s) for s in scopes),
                                key=lambda t: ["low", "medium", "high", "critical"].index(t), default="low"),
            })
        await ctx.say("ok", f"Applications: {len(oauth_grants):,} delegated grant(s), "
                            f"{all_principals_grants} tenant-wide")

        # --- application records ---------------------------------------------------
        apps: dict[str, dict[str, Any]] = {}
        for a in apps_raw:
            a = as_dict(a)
            oid = str(a.get("id") or "")
            if not oid:
                continue
            app_id = str(a.get("appId") or "")
            web = as_dict(a.get("web"))
            spa = as_dict(a.get("spa"))
            public = as_dict(a.get("publicClient"))
            redirects = (
                [{"uri": str(u), "type": "web"} for u in as_list(web.get("redirectUris"))]
                + [{"uri": str(u), "type": "spa"} for u in as_list(spa.get("redirectUris"))]
                + [{"uri": str(u), "type": "publicClient"} for u in as_list(public.get("redirectUris"))]
            )
            for r in redirects:
                r["risk"] = redirect_risk(r["uri"])
            requested: list[dict[str, Any]] = []
            for rra in as_list(a.get("requiredResourceAccess")):
                rra = as_dict(rra)
                res_app_id = str(rra.get("resourceAppId") or "")
                res_name = RESOURCE_APP_IDS.get(res_app_id, res_app_id)
                for acc in as_list(rra.get("resourceAccess")):
                    acc = as_dict(acc)
                    pid = str(acc.get("id") or "")
                    meta = catalogue.get(pid)
                    name = meta["name"] if meta else f"unknown:{pid[:8]}"
                    requested.append({
                        "permission": name,
                        "permission_id": pid,
                        "resource": res_name,
                        "kind": "application" if str(acc.get("type")) == "Role" else "delegated",
                        "tier": permission_tier(name),
                        "known": bool(meta),
                    })
            sp = sp_by_appid.get(app_id)
            apps[oid] = {
                "object_id": oid,
                "app_id": app_id,
                "display_name": a.get("displayName", "") or "",
                "created_at": a.get("createdDateTime", "") or "",
                "sign_in_audience": a.get("signInAudience", "") or "",
                "multi_tenant": str(a.get("signInAudience") or "").startswith("AzureADMultipleOrgs")
                or str(a.get("signInAudience") or "").startswith("AzureADandPersonalMicrosoftAccount"),
                "identifier_uris": [str(u) for u in as_list(a.get("identifierUris"))],
                "redirect_uris": redirects,
                "notes": a.get("notes", "") or "",
                "tags": [str(t) for t in as_list(a.get("tags"))],
                "credentials": _credentials(as_list(a.get("passwordCredentials")), "secret", now)
                + _credentials(as_list(a.get("keyCredentials")), "certificate", now),
                "requested_permissions": requested,
                "app_roles": len(as_list(a.get("appRoles"))),
                "owner_ids": [],
                "owners_known": False,
                "federated_credentials": [],
                "fic_known": False,
                "sp_object_id": (sp or {}).get("object_id", ""),
                "verified_publisher": (sp or {}).get("verified_publisher", ""),
            }

        # --- owners (batched) -------------------------------------------------------
        if apps:
            await ctx.say("info", f"Applications: resolving owners for {len(apps):,} app(s)…")
            owners, owner_trunc, forbidden = await batch_collection(
                client, list(apps),
                lambda oid: f"/applications/{oid}/owners?$select=id,displayName&$top=20",
                cap=ctx.max_owner_lookups or None, ctx=ctx,
            )
            if owner_trunc:
                truncated = True
                notes.append(f"Application owner lookups capped at {ctx.max_owner_lookups:,}.")
            if forbidden:
                notes.append(f"{forbidden} application owner lookup(s) were forbidden.")
            for oid, rows in owners.items():
                apps[oid]["owner_ids"] = [str(as_dict(r).get("id") or "") for r in rows if as_dict(r).get("id")]
                apps[oid]["owners_known"] = True

            # Federated identity credentials — a credential-less persistence path.
            fics, fic_trunc, fic_forbidden = await batch_collection(
                client, list(apps),
                lambda oid: f"/applications/{oid}/federatedIdentityCredentials",
                cap=ctx.max_owner_lookups or None, ctx=ctx,
            )
            if fic_trunc:
                notes.append("Federated identity credential lookups were capped.")
            if fic_forbidden:
                notes.append(f"{fic_forbidden} federated-credential lookup(s) were forbidden.")
            for oid, rows in fics.items():
                apps[oid]["fic_known"] = True
                for r in rows:
                    r = as_dict(r)
                    issuer = str(r.get("issuer") or "")
                    subject = str(r.get("subject") or "")
                    apps[oid]["federated_credentials"].append({
                        "id": str(r.get("id") or ""),
                        "name": str(r.get("name") or ""),
                        "issuer": issuer,
                        "subject": subject,
                        "audiences": [str(x) for x in as_list(r.get("audiences"))],
                        "trusted": fic_trusted(issuer),
                        "wildcard_subject": "*" in subject,
                    })

        # --- enterprise-app owners (non first-party only) ---------------------------
        third_party_sps = [oid for oid, s in sp_by_object.items() if not s["is_first_party"]]
        if third_party_sps:
            sp_owners, sp_trunc2, sp_forbidden = await batch_collection(
                client, third_party_sps,
                lambda oid: f"/servicePrincipals/{oid}/owners?$select=id,displayName&$top=20",
                cap=ctx.max_owner_lookups or None, ctx=ctx,
            )
            if sp_trunc2:
                notes.append("Enterprise-application owner lookups were capped.")
            if sp_forbidden:
                notes.append(f"{sp_forbidden} enterprise-application owner lookup(s) were forbidden.")
            for oid, rows in sp_owners.items():
                sp_by_object[oid]["owner_ids"] = [
                    str(as_dict(r).get("id") or "") for r in rows if as_dict(r).get("id")
                ]
                sp_by_object[oid]["owners_known"] = True

        # --- orphaned service principals -------------------------------------------
        local_app_ids = {a["app_id"] for a in apps.values() if a["app_id"]}
        our_tenant = (ctx.tenant_id or "").lower()
        for sp in sp_by_object.values():
            sp["orphaned"] = bool(
                sp["sp_type"] == "Application"
                and sp["app_id"]
                and sp["app_id"] not in local_app_ids
                and sp["app_owner_tenant_id"].lower() == our_tenant
                and our_tenant
            )

        # --- assignment breadth + provisioning (P4) -----------------------------------
        # Only for third-party enterprise applications: first-party principals are assigned
        # to everyone by construction and would swamp the signal.
        assignable = [
            oid for oid, s in sp_by_object.items()
            if not s["is_first_party"] and s["sp_type"] == "Application"
        ]
        if assignable:
            await ctx.say("info", f"Applications: reading assignments for {len(assignable):,} enterprise app(s)…")
            assigned, assign_trunc, assign_forbidden = await batch_collection(
                client, assignable,
                lambda oid: f"/servicePrincipals/{oid}/appRoleAssignedTo?$select=id,principalType&$top=999",
                cap=ctx.max_owner_lookups or None, ctx=ctx,
            )
            if assign_trunc:
                notes.append("App assignment lookups were capped.")
            if assign_forbidden:
                notes.append(f"{assign_forbidden} app assignment lookup(s) were forbidden.")
            for oid, rows in assigned.items():
                sp_by_object[oid]["assigned_principals"] = len(rows)
                sp_by_object[oid]["assignment_known"] = True

            sync_jobs, _sync_trunc, sync_forbidden = await batch_collection(
                client, assignable,
                lambda oid: f"/servicePrincipals/{oid}/synchronization/jobs",
                cap=ctx.max_owner_lookups or None, ctx=ctx,
            )
            if sync_forbidden:
                notes.append(f"{sync_forbidden} provisioning-job lookup(s) were forbidden "
                             "(needs Synchronization.Read.All).")
            for oid, rows in sync_jobs.items():
                jobs = []
                for raw in rows:
                    job = as_dict(raw)
                    status = as_dict(job.get("status"))
                    jobs.append({
                        "id": str(job.get("id") or ""),
                        "template": str(job.get("templateId") or ""),
                        "code": str(status.get("code") or ""),
                        "quarantine": bool(status.get("quarantine")),
                        "last_execution": str(as_dict(status.get("lastExecution")).get("timeEnded") or ""),
                    })
                sp_by_object[oid]["provisioning_jobs"] = jobs

        # --- composite risk score ------------------------------------------------------
        azure_roles_by_sp = _azure_role_counts(ctx.tenant_id)
        for app in apps.values():
            sp = sp_by_object.get(app["sp_object_id"]) or {}
            app["risk"] = risk_score(app, sp, azure_roles=azure_roles_by_sp.get(app["sp_object_id"], 0))
        for oid, sp in sp_by_object.items():
            if sp.get("is_first_party"):
                sp["risk"] = {"score": 0, "components": [], "delegated_all_principals": 0}
                continue
            owning_app = next((a for a in apps.values() if a["sp_object_id"] == oid), {})
            sp["risk"] = risk_score(owning_app, sp, azure_roles=azure_roles_by_sp.get(oid, 0))

        data = {
            "applications": list(apps.values()),
            "service_principals": list(sp_by_object.values()),
            "signin_activity": await _signin_activity(client, ctx, now),
            "permission_catalogue_size": len(catalogue),
            "capabilities": {
                "granted_permissions": any(s["granted_app_permissions"] for s in sp_by_object.values())
                or bool(catalogue),
                "delegated_grants": bool(oauth_grants),
                "owners": any(a["owners_known"] for a in apps.values()),
                "federated_credentials": any(a["fic_known"] for a in apps.values()),
                "assignments": any(s.get("assignment_known") for s in sp_by_object.values()),
                "provisioning": any(s.get("provisioning_jobs") for s in sp_by_object.values()),
            },
            "counts": {
                "applications": len(apps),
                "service_principals": len(sp_by_object),
                "managed_identities": sum(1 for s in sp_by_object.values() if s["sp_type"] == "ManagedIdentity"),
                "first_party": sum(1 for s in sp_by_object.values() if s["is_first_party"]),
                "delegated_grants": len(oauth_grants),
                "all_principals_grants": all_principals_grants,
                "high_risk_apps": sum(1 for a in apps.values() if (a.get("risk") or {}).get("score", 0) >= 60),
                "provisioning_quarantined": sum(
                    1 for s in sp_by_object.values()
                    for j in s.get("provisioning_jobs") or [] if j.get("quarantine")
                ),
            },
        }
        status = model.STATUS_PARTIAL if (notes or truncated) else model.STATUS_OK
        return model.domain_payload(
            DOMAIN, data, status=status,
            item_count=len(apps) + len(sp_by_object), truncated=truncated, notes=notes,
        )

    return await guarded(DOMAIN, ctx, _run)
