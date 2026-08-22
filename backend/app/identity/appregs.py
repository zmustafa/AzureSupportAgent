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

* **Real** — when an Entra connection is configured, enumerate Microsoft Graph one bounded
    page at a time using the shared retry-aware Graph client. Every completed page is eligible
    for a durable checkpoint, so navigation, throttling, process failure and restart never
    destroy the last completed snapshot or force a long scan to start over.
* **Demo** — a rich, deterministic dummy dataset (no Azure required) so the grid + filters
  can be exercised locally.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from app.core.app_settings import load_settings
from app.entra.graphclient import (
    GraphAuthError,
    GraphClient,
    GraphError,
    GraphPermissionError,
    GraphRequest,
    GraphResponse,
)

log = logging.getLogger("app.identity.appregs")

# A progress callback used while building the snapshot: progress(level, message, metadata).
# level ∈ {"info", "ok", "warn", "error"}.
ProgressFn = Callable[..., Awaitable[None]]
CheckpointFn = Callable[[dict[str, Any]], Awaitable[None]]

APPREGS_PAGE_SIZE = 250
APPREGS_FULL_SAFETY_LIMIT = 100_000
APPREGS_CHECKPOINT_SCHEMA = 2
ENTERPRISE_APP_STATES = ("active", "deactivated", "not_instantiated", "unknown")


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


# --------------------------------------------------------------------------- sign-in activity
# Microsoft's per-application sign-in report covers a rolling window. Three facts have to stay
# distinct here, because collapsing them is how a live application gets deleted:
#   * a date            — it was signed into then
#   * no row, measured  — nothing signed into it inside the window (NOT "never")
#   * not measured      — we could not read the report at all (NOT "never" either)
SIGNIN_WINDOW_DAYS = 30
SIGNIN_BUCKET_RECENT = "Last 7 days"
SIGNIN_BUCKET_WINDOW = "8-30 days"
SIGNIN_BUCKET_OLD = "Over 30 days"
SIGNIN_BUCKET_NONE = "No sign-in in 30 days"
SIGNIN_BUCKET_UNKNOWN = "Not measured"
SIGNIN_BUCKETS = (
    SIGNIN_BUCKET_RECENT,
    SIGNIN_BUCKET_WINDOW,
    SIGNIN_BUCKET_OLD,
    SIGNIN_BUCKET_NONE,
    SIGNIN_BUCKET_UNKNOWN,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _newest_signin(*blocks: Any) -> str:
    """Newest ``lastSignInDateTime`` across Graph activity blocks, or "".

    Graph emits ISO-8601 UTC, which sorts lexically, so ``max`` is exact without parsing.
    """
    stamps = [str(_as_dict(b).get("lastSignInDateTime") or "") for b in blocks]
    return max((s for s in stamps if s), default="")


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((_now() - dt).total_seconds() // 86400))


def signin_bucket(app: dict[str, Any]) -> str:
    """Facet bucket for one normalized app row."""
    if not app.get("lastSignInKnown"):
        return SIGNIN_BUCKET_UNKNOWN
    days = app.get("lastSignInDays")
    if days is None:
        return SIGNIN_BUCKET_NONE
    if days <= 7:
        return SIGNIN_BUCKET_RECENT
    if days <= SIGNIN_WINDOW_DAYS:
        return SIGNIN_BUCKET_WINDOW
    return SIGNIN_BUCKET_OLD


def unmeasured_signin_activity(reason: str) -> dict[str, Any]:
    """Snapshot-level block stating that sign-in activity could not be read, and why."""
    return {
        "measured": False,
        "reason": reason,
        "window_days": SIGNIN_WINDOW_DAYS,
        "source": "servicePrincipalSignInActivities",
        "complete": False,
        "apps_with_activity": 0,
        "credentials": {"measured": False, "reason": reason, "count": 0},
    }


# --------------------------------------------------------------------------- normalize
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
        last_used = str(c.get("lastUsed") or "") or None
        credentials.append(
            {
                "keyId": str(c.get("keyId") or ""),
                "type": c.get("type", "secret"),
                "displayName": c.get("displayName") or "",
                "endDateTime": c.get("endDateTime"),
                "daysUntilExpiry": d,
                "lastUsed": last_used,
                "lastUsedKnown": bool(c.get("lastUsedKnown")),
                "lastUsedDays": _days_since(last_used),
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
    enterprise_state = str(raw.get("enterpriseAppState") or "unknown")
    if enterprise_state not in ENTERPRISE_APP_STATES:
        enterprise_state = "unknown"

    activity = _as_dict(raw.get("signInActivity"))
    last_signin = str(activity.get("last") or "") or None

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
        # `lastSignInKnown` is the tenant-level fact "the report was read". Without it a
        # missing date is indistinguishable from an unreadable report.
        "lastSignIn": last_signin,
        "lastSignInKnown": bool(activity.get("known")),
        "lastSignInDays": _days_since(last_signin),
        "lastSignInDelegated": str(activity.get("delegated") or "") or None,
        "lastSignInApplication": str(activity.get("application") or "") or None,
        "enterpriseAppState": enterprise_state,
        "servicePrincipalId": raw.get("servicePrincipalId") or None,
        "servicePrincipalType": raw.get("servicePrincipalType") or "",
        "disabledByMicrosoftStatus": raw.get("disabledByMicrosoftStatus") or "",
        "enterpriseAppStateReadStatus": raw.get("enterpriseAppStateReadStatus") or "unreadable",
        "enterpriseAppStateSource": raw.get("enterpriseAppStateSource") or "microsoft_graph",
    }


# --------------------------------------------------------------------------- aggregate
def aggregate(apps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build facet option counts + the summary KPIs from normalized app rows."""
    audiences: dict[str, int] = {}
    perms: dict[str, int] = {}
    owners: dict[str, int] = {}
    states: dict[str, int] = {state: 0 for state in ENTERPRISE_APP_STATES}
    signins: dict[str, int] = {bucket: 0 for bucket in SIGNIN_BUCKETS}
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
        "active": 0,
        "deactivated": 0,
        "notInstantiated": 0,
        "stateUnknown": 0,
        "signedIn7d": 0,
        "signedIn30d": 0,
        "noRecentSignIn": 0,
        "signInNotMeasured": 0,
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
        state = str(a.get("enterpriseAppState") or "unknown")
        if state not in states:
            state = "unknown"
        states[state] += 1
        if state == "active":
            summary["active"] += 1
        elif state == "deactivated":
            summary["deactivated"] += 1
        elif state == "not_instantiated":
            summary["notInstantiated"] += 1
        else:
            summary["stateUnknown"] += 1
        summary["applicationPerms"] += a["applicationPermissionsCount"]
        summary["delegatedPerms"] += a["delegatedPermissionsCount"]
        bucket = signin_bucket(a)
        signins[bucket] += 1
        if bucket == SIGNIN_BUCKET_UNKNOWN:
            summary["signInNotMeasured"] += 1
        elif bucket in (SIGNIN_BUCKET_NONE, SIGNIN_BUCKET_OLD):
            summary["noRecentSignIn"] += 1
        else:
            summary["signedIn30d"] += 1
            if bucket == SIGNIN_BUCKET_RECENT:
                summary["signedIn7d"] += 1
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
        "enterpriseAppStates": _facet(states),
        # Ordered newest-first rather than by count, so the buckets read as a timeline.
        "signInActivity": [{"value": b, "count": signins[b]} for b in SIGNIN_BUCKETS],
        "summary": summary,
    }


# --------------------------------------------------------------------------- demo data
def _iso_in(days: int) -> str:
    return (_now() + timedelta(days=days)).isoformat()


def _created(days_ago: int) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat()


def _signed_in(days_ago: int | None) -> str:
    return "" if days_ago is None else (_now() - timedelta(days=days_ago, hours=3)).isoformat()


def demo_signin_activity(apps: list[dict[str, Any]]) -> dict[str, Any]:
    """The snapshot-level sign-in block for the demo dataset (measured, clearly labeled)."""
    return {
        "measured": True,
        "reason": "",
        "window_days": SIGNIN_WINDOW_DAYS,
        "source": "demo",
        "complete": True,
        "apps_with_activity": sum(1 for a in apps if a.get("lastSignIn")),
        "credentials": {
            "measured": True,
            "reason": "",
            "count": sum(1 for a in apps for c in a["credentials"] if c.get("lastUsed")),
        },
    }


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
    demo_states = (
        "active", "active", "deactivated", "active", "active", "active",
        "not_instantiated", "active", "unknown", "active", "active", "active",
    )
    # Days since the last sign-in; None = measured but nothing signed into it in the window.
    # Index 2 (Legacy Migration Tool) and 8 (Partner B2B Gateway) are the dormant-but-dangerous
    # pair, and index 9 is deliberately older than the window so that bucket is reachable too.
    demo_signins: tuple[int | None, ...] = (0, 3, None, 12, 0, 21, 5, 2, None, 45, 1, 9)
    for index, app in enumerate(raw):
        state = demo_states[index]
        app["enterpriseAppState"] = state
        app["servicePrincipalId"] = (
            f"d0000000-0000-0000-0000-{index + 1:012d}"
            if state in ("active", "deactivated")
            else None
        )
        app["servicePrincipalType"] = "Application" if app["servicePrincipalId"] else ""
        app["disabledByMicrosoftStatus"] = ""
        app["enterpriseAppStateReadStatus"] = (
            "demo" if state != "not_instantiated" else "not_found"
        )
        app["enterpriseAppStateSource"] = "demo"

        days_ago = demo_signins[index]
        last = _signed_in(days_ago)
        kinds = {p["type"] for p in app["permissions"]}
        app["signInActivity"] = {
            "known": True,
            "last": last,
            "delegated": last if "Delegated" in kinds else "",
            "application": last if "Application" in kinds else "",
        }
        # Only the first credential is shown as used, so the demo carries the question the
        # per-credential report exists to answer: which of these can be retired?
        for position, credential in enumerate(app["credentials"]):
            credential["keyId"] = f"c0000000-0000-0000-{index + 1:04d}-{position + 1:012d}"
            credential["lastUsed"] = last if position == 0 else ""
            credential["lastUsedKnown"] = True
    return [_normalise_app(r) for r in raw]


# --------------------------------------------------------------------------- real Graph projection
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


def _creds_from_graph_app(
    detail: dict[str, Any],
    *,
    last_used: dict[str, str] | None = None,
    used_known: bool = False,
) -> list[dict[str, Any]]:
    used = last_used or {}
    out: list[dict[str, Any]] = []
    for kind, key in (("secret", "passwordCredentials"), ("certificate", "keyCredentials")):
        for cred in detail.get(key) or []:
            key_id = str(cred.get("keyId") or "")
            out.append({
                "keyId": key_id,
                "type": kind,
                "displayName": cred.get("displayName") or "",
                "endDateTime": cred.get("endDateTime"),
                "lastUsed": used.get(key_id.lower(), ""),
                "lastUsedKnown": used_known,
            })
    return out


async def _signin_activity(
    client: GraphClient,
    say: ProgressFn,
    *,
    max_items: int,
) -> dict[str, Any]:
    """Last sign-in per application, from the beta service-principal activity report.

    Returns ``{"block": <snapshot-level meta>, "by_app": {appId(lower): {...}}}``. Any failure
    degrades to ``measured: False`` with an operator-actionable reason — never to an empty map,
    which would render as "nothing has signed into any of these".
    """
    empty: dict[str, dict[str, str]] = {}
    if not client.beta_available(True):
        return {
            "block": unmeasured_signin_activity(
                "Sign-in activity comes from a Microsoft Graph beta report, and beta endpoints "
                "are turned off for this deployment. Enable them in General settings to show "
                "when each application was last signed into."
            ),
            "by_app": empty,
        }
    await say("info", "Reading per-application sign-in activity…", phase="signin_activity")
    try:
        rows, truncated = await client.get_all(
            "/reports/servicePrincipalSignInActivities", top=999, max_items=max_items, beta=True,
        )
    except GraphPermissionError:
        log.info("app-registrations sign-in activity denied")
        return {
            "block": unmeasured_signin_activity(
                "Microsoft Graph denied the sign-in activity report. Grant AuditLog.Read.All as an "
                "application permission to this connection, then refresh."
            ),
            "by_app": empty,
        }
    except GraphError as exc:
        log.info("app-registrations sign-in activity unavailable: status=%d", exc.status)
        licence = (
            " This report also requires a Microsoft Entra ID P1 or P2 license."
            if exc.status in (400, 403, 404)
            else ""
        )
        return {
            "block": unmeasured_signin_activity(
                f"The sign-in activity report could not be read (HTTP {exc.status or 'network'}).{licence}"
            ),
            "by_app": empty,
        }

    by_app: dict[str, dict[str, str]] = {}
    for row in rows:
        row = _as_dict(row)
        app_id = str(row.get("appId") or "").lower()
        if not app_id:
            continue
        delegated = _newest_signin(
            row.get("delegatedClientSignInActivity"), row.get("delegatedResourceSignInActivity")
        )
        application = _newest_signin(
            row.get("applicationAuthenticationClientSignInActivity"),
            row.get("applicationAuthenticationResourceSignInActivity"),
        )
        last = _newest_signin(row.get("lastSignInActivity")) or max(
            (s for s in (delegated, application) if s), default=""
        )
        if last or delegated or application:
            by_app[app_id] = {"last": last, "delegated": delegated, "application": application}
    await say("ok", f"Sign-in activity read — {len(by_app):,} application(s) with recent sign-ins.",
              phase="signin_activity", current=len(by_app))
    return {
        "block": {
            "measured": True,
            "reason": "",
            "window_days": SIGNIN_WINDOW_DAYS,
            "source": "servicePrincipalSignInActivities",
            "complete": not truncated,
            "apps_with_activity": len(by_app),
            "credentials": {"measured": False, "reason": "", "count": 0},
        },
        "by_app": by_app,
    }


async def _credential_activity(
    client: GraphClient,
    say: ProgressFn,
    *,
    max_items: int,
) -> dict[str, Any]:
    """Last use per credential (secret/certificate keyId), from the beta credential report.

    This is what turns "this app has three secrets" into "two of these three can be retired".
    """
    empty: dict[str, str] = {}
    if not client.beta_available(True):
        return {"measured": False, "reason": "Beta Graph endpoints are turned off.", "by_key": empty}
    try:
        rows, truncated = await client.get_all(
            "/reports/appCredentialSignInActivities", top=999, max_items=max_items, beta=True,
        )
    except GraphError as exc:
        log.info("app-registrations credential activity unavailable: status=%d", exc.status)
        return {
            "measured": False,
            "reason": f"Per-credential usage could not be read (HTTP {exc.status or 'network'}).",
            "by_key": empty,
        }
    by_key: dict[str, str] = {}
    for row in rows:
        row = _as_dict(row)
        key_id = str(row.get("keyId") or "").lower()
        last = _newest_signin(row.get("signInActivity"))
        if key_id and last and last > by_key.get(key_id, ""):
            by_key[key_id] = last
    await say("ok", f"Credential usage read — {len(by_key):,} credential(s) used recently.",
              phase="signin_activity", current=len(by_key))
    return {"measured": True, "reason": "", "by_key": by_key, "complete": not truncated}



async def _graph_permission_resolver(client: GraphClient) -> dict[str, str]:
    """Build a Graph permission GUID→friendly-name map with one read-only call."""
    try:
        data = await client.get(
            "/servicePrincipals",
            params={
                "$filter": "appId eq '00000003-0000-0000-c000-000000000000'",
                "$select": "appRoles,oauth2PermissionScopes",
                "$top": 1,
            },
        )
    except GraphError as exc:
        log.info("Microsoft Graph permission catalog unavailable: status=%d", exc.status)
        return {}
    rows = data.get("value") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return {}
    out: dict[str, str] = {}
    for key in ("oauth2PermissionScopes", "appRoles"):
        for permission in rows[0].get(key) or []:
            if not isinstance(permission, dict):
                continue
            gid, value = permission.get("id"), permission.get("value")
            if gid and value:
                out[str(gid)] = str(value)
    return out


def _enterprise_state_projection(raw: dict[str, Any]) -> dict[str, Any]:
    """Return only the persisted service-principal state fields from an app row."""
    state = str(raw.get("enterpriseAppState") or "unknown")
    if state not in ENTERPRISE_APP_STATES:
        state = "unknown"
    return {
        "enterpriseAppState": state,
        "servicePrincipalId": raw.get("servicePrincipalId") or None,
        "servicePrincipalType": raw.get("servicePrincipalType") or "",
        "disabledByMicrosoftStatus": raw.get("disabledByMicrosoftStatus") or "",
        "enterpriseAppStateReadStatus": raw.get("enterpriseAppStateReadStatus") or "unreadable",
        "enterpriseAppStateSource": raw.get("enterpriseAppStateSource") or "microsoft_graph",
    }


def _unknown_enterprise_state(read_status: str) -> dict[str, Any]:
    return {
        "enterpriseAppState": "unknown",
        "servicePrincipalId": None,
        "servicePrincipalType": "",
        "disabledByMicrosoftStatus": "",
        "enterpriseAppStateReadStatus": read_status,
        "enterpriseAppStateSource": "microsoft_graph",
    }


def _enterprise_state_from_response(response: GraphResponse) -> dict[str, Any]:
    if response.status == 404:
        return {
            "enterpriseAppState": "not_instantiated",
            "servicePrincipalId": None,
            "servicePrincipalType": "",
            "disabledByMicrosoftStatus": "",
            "enterpriseAppStateReadStatus": "not_found",
            "enterpriseAppStateSource": "microsoft_graph",
        }
    if not response.ok or not isinstance(response.body, dict):
        return _unknown_enterprise_state("unreadable")
    enabled = response.body.get("accountEnabled")
    state = "active" if enabled is True else "deactivated" if enabled is False else "unknown"
    return {
        "enterpriseAppState": state,
        "servicePrincipalId": response.body.get("id") or None,
        "servicePrincipalType": response.body.get("servicePrincipalType") or "",
        "disabledByMicrosoftStatus": response.body.get("disabledByMicrosoftStatus") or "",
        "enterpriseAppStateReadStatus": "read" if isinstance(enabled, bool) else "incomplete",
        "enterpriseAppStateSource": "microsoft_graph",
    }


async def _attach_enterprise_app_states(
    client: GraphClient,
    apps: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
    *,
    on_retry: Callable[[int, int, float], Awaitable[None]] | None = None,
) -> None:
    """Join local service-principal state to one completed application page by ``appId``.

    Alternate-key GETs are sent through Graph JSON batching (20 per request). Every result,
    including absence and unreadability, is attached before the page is checkpointed so a
    resumed refresh never repeats completed state lookups.
    """
    pending: list[str] = []
    for app in apps:
        raw_id = str(app.get("appId") or "")
        try:
            app_id = str(UUID(raw_id))
        except (ValueError, TypeError, AttributeError):
            app.update(_unknown_enterprise_state("invalid_app_id"))
            continue
        if app_id in known:
            app.update(known[app_id])
        elif app_id not in pending:
            pending.append(app_id)

    if pending:
        requests = [
            GraphRequest(
                id=str(index),
                url=(
                    f"/servicePrincipals(appId='{app_id}')"
                    "?$select=id,appId,accountEnabled,servicePrincipalType,disabledByMicrosoftStatus"
                ),
            )
            for index, app_id in enumerate(pending)
        ]
        try:
            responses = await client.batch(requests, on_retry=on_retry)
        except GraphError:
            responses = [GraphResponse(id=request.id, status=0, body=None) for request in requests]
        for request, response in zip(requests, responses):
            app_id = pending[int(request.id)]
            known[app_id] = _enterprise_state_from_response(response)

    for app in apps:
        try:
            app_id = str(UUID(str(app.get("appId") or "")))
        except (ValueError, TypeError, AttributeError):
            continue
        app.update(known.get(app_id) or _unknown_enterprise_state("unreadable"))


async def _collect_real(
    connection: dict[str, Any],
    *,
    limit: int,
    full: bool = False,
    page_size: int = APPREGS_PAGE_SIZE,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint: CheckpointFn | None = None,
    progress: "ProgressFn | None" = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enumerate Graph applications one page at a time with durable checkpoints."""
    started = time.monotonic()
    mode = "full" if full else "capped"
    target_limit = APPREGS_FULL_SAFETY_LIMIT if full else max(50, min(5000, int(limit)))
    page_size = max(50, min(999, int(page_size)))

    async def _say(level: str, message: str, **metadata: Any) -> None:
        if progress is not None:
            await progress(level, message, metadata)

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    checkpoint_matches = bool(
        checkpoint
        and checkpoint.get("schema") == APPREGS_CHECKPOINT_SCHEMA
        and checkpoint.get("mode") == mode
        and int(checkpoint.get("target_limit") or 0) == target_limit
        and int(checkpoint.get("page_size") or 0) == page_size
        and isinstance(checkpoint.get("apps_raw"), list)
    )
    apps_raw = list(checkpoint.get("apps_raw") or []) if checkpoint_matches else []
    next_link = str(checkpoint.get("next_link") or "") if checkpoint_matches else ""
    pages = int(checkpoint.get("pages") or 0) if checkpoint_matches else 0
    graph_total = checkpoint.get("graph_total") if checkpoint_matches else None
    resumed = bool(checkpoint_matches and (apps_raw or next_link))
    enumeration_complete = bool(checkpoint.get("enumeration_complete")) if checkpoint_matches else False
    enterprise_state_by_app_id: dict[str, dict[str, Any]] = {}
    for saved in apps_raw:
        if not isinstance(saved, dict) or "enterpriseAppState" not in saved:
            continue
        try:
            saved_app_id = str(UUID(str(saved.get("appId") or "")))
        except (ValueError, TypeError, AttributeError):
            continue
        enterprise_state_by_app_id[saved_app_id] = _enterprise_state_projection(saved)

    await _say("info", "Connecting to Microsoft Entra (Graph)…", phase="connect", mode=mode)
    # The sign-in activity reports are beta-only, and GraphClient silently falls back to v1.0
    # when beta is off — which would 400 on a URL that does not exist there.
    async with GraphClient(
        connection,
        concurrency=2,
        beta=bool(load_settings().get("entra_enable_beta_endpoints", True)),
    ) as client:
        await _say("info", "Loading the Microsoft Graph permission catalog (appRoles + delegated scopes)…")
        resolver = await _graph_permission_resolver(client)
        await _say("info", f"Permission catalog loaded — {len(resolver)} permission id(s) resolvable to friendly names.")

        if resumed:
            await _say(
                "info",
                f"Resuming from checkpoint — {len(apps_raw)} registration(s) across {pages} page(s) already fetched.",
                phase="resume", current=len(apps_raw), total=graph_total, page=pages, resumed=True,
            )
        else:
            scope = "the full tenant" if full else f"up to {target_limit}"
            await _say("info", f"Listing application registrations ({scope})…", phase="enumerate", current=0)

        if graph_total is None and not resumed:
            try:
                graph_total = await client.get_count("applications")
            except GraphError as exc:
                log.info("Microsoft Graph application count unavailable: status=%d", exc.status)
            if graph_total is not None:
                await _say(
                    "info", f"Tenant reports {graph_total} application registration(s).",
                    phase="count", current=len(apps_raw), total=graph_total,
                    percent=round((len(apps_raw) / graph_total) * 100, 1) if graph_total else 100.0,
                )

        restarted_after_expired_checkpoint = False
        while not enumeration_complete and len(apps_raw) < target_limit:
            if _cancelled():
                raise asyncio.CancelledError()

            async def _retry(status: int, attempt: int, delay: float) -> None:
                await _say(
                    "warn",
                    f"Microsoft Graph returned {status}; retry {attempt} in {delay:.1f}s…",
                    phase="throttle", status=status, retry=attempt,
                    delay_seconds=round(delay, 1), current=len(apps_raw), total=graph_total,
                    throttles=client.stats.throttled, retries=client.stats.retries,
                )

            remaining = target_limit - len(apps_raw)
            try:
                page = await client.get_page(
                    "applications",
                    select=(
                        "id", "appId", "displayName", "createdDateTime", "signInAudience",
                        "publisherDomain", "tags", "passwordCredentials", "keyCredentials",
                        "requiredResourceAccess",
                    ),
                    expand="owners($select=id,displayName,userPrincipalName)",
                    top=min(page_size, remaining),
                    next_link=next_link,
                    include_count=not apps_raw and not next_link,
                    on_retry=_retry,
                )
            except GraphError as exc:
                if resumed and next_link and exc.status in (400, 410) and not restarted_after_expired_checkpoint:
                    restarted_after_expired_checkpoint = True
                    resumed = False
                    apps_raw, next_link, pages, graph_total = [], "", 0, None
                    enterprise_state_by_app_id = {}
                    await _say("warn", "Saved Graph continuation expired; restarting enumeration from page 1.", phase="restart")
                    continue
                raise

            if graph_total is None and page.total is not None:
                graph_total = max(0, int(page.total))
            page_items = page.items[:remaining]

            async def _state_retry(status: int, attempt: int, delay: float) -> None:
                await _say(
                    "warn",
                    f"Microsoft Graph returned {status} while checking enterprise-app state; retry {attempt} in {delay:.1f}s…",
                    phase="enterprise_state", status=status, retry=attempt,
                    delay_seconds=round(delay, 1), current=len(apps_raw), total=graph_total,
                    throttles=client.stats.throttled, retries=client.stats.retries,
                )

            await _say(
                "info",
                f"Checking enterprise-application state for page {pages + 1}…",
                phase="enterprise_state", current=len(apps_raw), total=graph_total, page=pages + 1,
            )
            await _attach_enterprise_app_states(
                client,
                page_items,
                enterprise_state_by_app_id,
                on_retry=_state_retry,
            )
            apps_raw.extend(page_items)
            pages += 1
            next_link = page.next_link
            enumeration_complete = not bool(next_link)
            state = {
                "schema": APPREGS_CHECKPOINT_SCHEMA,
                "mode": mode,
                "configured_limit": limit,
                "target_limit": target_limit,
                "page_size": page_size,
                "pages": pages,
                "graph_total": graph_total,
                "next_link": next_link,
                "enumeration_complete": enumeration_complete,
                "apps_raw": apps_raw,
            }
            if on_checkpoint is not None:
                await on_checkpoint(state)
            pct = round((len(apps_raw) / graph_total) * 100, 1) if graph_total else None
            total_text = f" of {graph_total}" if graph_total is not None else ""
            await _say(
                "ok",
                f"Page {pages} fetched — {len(apps_raw)}{total_text} application registration(s){f' ({pct:g}%)' if pct is not None else ''}.",
                phase="enumerate", current=len(apps_raw), total=graph_total, percent=pct,
                page=pages, pages=pages, page_size=page_size, retries=client.stats.retries,
                throttles=client.stats.throttled, resumed=resumed,
            )

        truncated = bool(next_link)
        stop_reason = "complete" if not truncated else ("full_safety_limit" if full else "configured_limit")

        # One tenant-wide read each, after enumeration: both reports are one row per object,
        # so doing them per page would multiply the cost for no extra information.
        signin = await _signin_activity(client, _say, max_items=APPREGS_FULL_SAFETY_LIMIT)
        signin_block, signin_by_app = signin["block"], signin["by_app"]
        creds = (
            await _credential_activity(client, _say, max_items=APPREGS_FULL_SAFETY_LIMIT)
            if signin_block["measured"]
            else {"measured": False, "reason": signin_block["reason"], "by_key": {}}
        )
        signin_block["credentials"] = {
            "measured": bool(creds["measured"]),
            "reason": str(creds.get("reason") or ""),
            "count": len(creds["by_key"]),
        }

        await _say(
            "info", f"Processing {len(apps_raw)} fetched application registration(s)…",
            phase="process", current=0, total=len(apps_raw), pages=pages,
        )

        out: list[dict[str, Any]] = []
        for index, app in enumerate(apps_raw, start=1):
            if _cancelled():
                raise asyncio.CancelledError()
            if not isinstance(app, dict):
                continue
            owners = []
            for owner in app.get("owners") or []:
                if isinstance(owner, dict):
                    owners.append(owner.get("displayName") or owner.get("userPrincipalName") or "")
                elif isinstance(owner, str):
                    owners.append(owner)
            activity = signin_by_app.get(str(app.get("appId") or "").lower()) or {}
            out.append(_normalise_app({
                "id": app.get("id"), "appId": app.get("appId"),
                "displayName": app.get("displayName"), "signInAudience": app.get("signInAudience"),
                "createdDateTime": app.get("createdDateTime"), "publisherDomain": app.get("publisherDomain"),
                "tags": app.get("tags") or [],
                "credentials": _creds_from_graph_app(
                    app, last_used=creds["by_key"], used_known=bool(creds["measured"]),
                ),
                "permissions": _perms_from_graph_app(app, resolver), "owners": [o for o in owners if o],
                "signInActivity": {"known": signin_block["measured"], **activity},
                **_enterprise_state_projection(app),
            }))
            if index == len(apps_raw) or index % 100 == 0:
                pct = round((index / len(apps_raw)) * 100, 1) if apps_raw else 100.0
                await _say(
                    "info", f"Processed {index} of {len(apps_raw)} registration(s) ({pct:g}%).",
                    phase="process", current=index, total=len(apps_raw), percent=pct, pages=pages,
                )

        return out, {
            "mode": mode, "configured_limit": limit, "applied_limit": target_limit,
            "full_safety_limit": APPREGS_FULL_SAFETY_LIMIT, "page_size": page_size,
            "pages": pages, "graph_total": graph_total, "fetched": len(out),
            "complete": not truncated, "truncated": truncated, "stop_reason": stop_reason,
            "resumed": resumed, "retries": client.stats.retries,
            "throttles": client.stats.throttled,
            "duration_seconds": round(time.monotonic() - started, 1),
            "signin_activity": signin_block,
        }


# --------------------------------------------------------------------------- orchestrator
async def collect_app_registrations(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
    limit: int = 200,
    full: bool = False,
    page_size: int = APPREGS_PAGE_SIZE,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint: CheckpointFn | None = None,
    progress: "ProgressFn | None" = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build the full app-registrations snapshot. Never raises — falls back to demo data.
    Emits granular ``progress(level, message)`` lines (the live enumeration can take 10–30
    minutes on a large tenant)."""
    async def _say(level: str, message: str, **metadata: Any) -> None:
        if progress is not None:
            await progress(level, message, metadata)

    source = "demo_dummy_data"
    note = ""
    apps: list[dict[str, Any]] = []
    enumeration = {
        "mode": "full" if full else "capped",
        "configured_limit": limit,
        "applied_limit": APPREGS_FULL_SAFETY_LIMIT if full else limit,
        "full_safety_limit": APPREGS_FULL_SAFETY_LIMIT,
        "page_size": page_size,
        "pages": 0,
        "graph_total": None,
        "fetched": 0,
        "complete": False,
        "truncated": False,
        "stop_reason": "not_started",
        "resumed": False,
        "retries": 0,
        "throttles": 0,
        "duration_seconds": 0.0,
    }
    # When a connection IS configured the user wants their REAL tenant — never silently
    # substitute demo data (it looks deceptively real). A Graph auth/config error or a live
    # enumeration failure yields an empty snapshot + an actionable note instead.
    connection_failed = False
    if connection is not None:
        try:
            apps, enumeration = await _collect_real(
                connection,
                limit=limit,
                full=full,
                page_size=page_size,
                checkpoint=checkpoint,
                on_checkpoint=on_checkpoint,
                progress=progress,
                should_cancel=should_cancel,
            )
            source = "microsoft_graph"
        except asyncio.CancelledError:
            raise
        except GraphPermissionError:
            note = "Microsoft Graph denied the application inventory. Grant Application.Read.All or Directory.Read.All, then refresh."
            source, connection_failed = "unavailable", True
            log.info("app-registrations Graph permission denied")
            await _say("error", note, phase="error")
        except GraphAuthError:
            note = "This connection could not acquire a Microsoft Graph token. Check its tenant credentials and Graph application permissions."
            source, connection_failed = "unavailable", True
            log.info("app-registrations Graph authentication unavailable")
            await _say("error", note, phase="error")
        except GraphError as exc:
            note = f"Microsoft Graph application enumeration failed (HTTP {exc.status or 'network'}). The previous completed snapshot was preserved."
            source, connection_failed = "unavailable", True
            log.info("app-registrations Graph enumeration failed: status=%d", exc.status)
            await _say("error", note, phase="error")
        except Exception as exc:  # noqa: BLE001 - collapse unexpected provider details
            note = "Application registration refresh failed. The previous completed snapshot was preserved."
            source, connection_failed = "unavailable", True
            log.info("app-registrations live collect failed: %s", type(exc).__name__)
            await _say("error", note, phase="error")
    if not apps and connection is None:
        # No Azure connection at all (fresh product exploration) — seed the illustrative demo set.
        apps = build_demo_app_registrations()
        source = "demo_dummy_data"
        note = note or "No Entra connection configured — showing demo data."
        await _say("warn", note)
    await _say("info", "Aggregating facets (audiences, permissions, owners, enterprise-app state) and summary KPIs…", phase="aggregate")
    apps.sort(key=lambda a: a["displayName"].lower())
    agg = aggregate(apps)
    if source == "microsoft_graph":
        signin_activity = enumeration.pop("signin_activity", None) or unmeasured_signin_activity(
            "Sign-in activity was not read during this refresh."
        )
    elif source == "demo_dummy_data":
        signin_activity = demo_signin_activity(apps)
    else:
        enumeration.pop("signin_activity", None)
        signin_activity = unmeasured_signin_activity(
            "Sign-in activity was not read because the application inventory could not be enumerated."
        )
    truncated = bool(enumeration.get("truncated")) if source == "microsoft_graph" else False
    await _say("ok", f"Snapshot complete — {len(apps)} app registration(s).", phase="complete", current=len(apps), total=enumeration.get("graph_total"), percent=100 if enumeration.get("complete") else None)
    return {
        "generated_at": _now_iso(),
        "tenant_id": tenant_id,
        "connection_configured": connection is not None,
        "source": source,
        "note": note,
        "connection_failed": connection_failed,
        "apps": apps,
        "facets": {
            "audiences": agg["audiences"],
            "permissions": agg["permissions"],
            "owners": agg["owners"],
            "enterpriseAppStates": agg["enterpriseAppStates"],
            "signInActivity": agg["signInActivity"],
        },
        "summary": agg["summary"],
        "signin_activity": signin_activity,
        "truncated": truncated,
        "limit": limit,
        "graph_total": enumeration.get("graph_total"),
        "enumeration": enumeration,
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
        "facets": {
            "audiences": agg["audiences"],
            "permissions": agg["permissions"],
            "owners": agg["owners"],
            "enterpriseAppStates": agg["enterpriseAppStates"],
            "signInActivity": agg["signInActivity"],
        },
        "summary": agg["summary"],
        "signin_activity": demo_signin_activity(apps),
    }


def seed_demo(tenant_id: str = "default") -> dict[str, Any]:
    """Seed the App Registrations demo snapshot into the cache (keyed by tenant + empty
    connection id, matching the no-connection read path). Returns the stored payload."""
    from app.identity import appregs_cache

    payload = build_demo_snapshot(tenant_id)
    appregs_cache.set_(tenant_id, "", payload)
    return payload
