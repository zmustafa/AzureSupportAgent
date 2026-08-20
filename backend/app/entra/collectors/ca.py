"""Conditional Access collector — policies, named locations and authentication strengths.

I/O only. The interesting work (resolving include/exclude sets to concrete users, building
the coverage matrix, detecting conflicts and break-glass exposure) is a **pure** function in
:mod:`app.entra.ca_engine`, because it needs the people and roles domains and because a pure
function is the only way to golden-file test a coverage matrix.

The collector does expand the groups a policy references — a bounded set, usually a few
dozen — since that expansion is I/O and the engine must stay pure.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, clip, guarded
from app.entra.collectors.people import expand_groups
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "ca"

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_REPORT_ONLY = "enabledForReportingButNotEnforced"

# Well-known application ids used in policy conditions.
APP_ALL = "All"
APP_OFFICE365 = "Office365"
APP_ADMIN_PORTALS = "MicrosoftAdminPortals"
APP_AZURE_MANAGEMENT = "797f4846-ba00-4fd7-ba43-dac1f8f63013"

# Client app types that mean "legacy authentication" (no modern auth, so no MFA).
LEGACY_CLIENT_APPS = {"exchangeActiveSync", "other"}
MODERN_CLIENT_APPS = {"browser", "mobileAppsAndDesktopClients"}


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        truncated = False

        await ctx.say("info", "Conditional Access: collecting policies…")
        policies, _ = await client.get_all("/identity/conditionalAccess/policies", top=999)
        await ctx.say("ok", f"Conditional Access: {len(policies)} policy/policies")

        locations: list[dict[str, Any]] = []
        try:
            locations, _ = await client.get_all("/identity/conditionalAccess/namedLocations", top=999)
        except GraphPermissionError as exc:
            notes.append(f"Named locations not permitted ({clip(exc.message, 110)}).")
        except GraphError as exc:
            notes.append(f"Named locations: {clip(exc, 150)}")

        strengths: list[dict[str, Any]] = []
        try:
            # This collection rejects `$top` (400). See GraphClient.get_all's fallback.
            strengths, _ = await client.get_all(
                "/policies/authenticationStrengthPolicies",
                select=["id", "displayName", "policyType", "requirementsSatisfied", "allowedCombinations"],
                top=0,
            )
        except GraphPermissionError as exc:
            notes.append(f"Authentication strengths not permitted ({clip(exc.message, 110)}).")
        except GraphError as exc:
            notes.append(f"Authentication strengths: {clip(exc, 150)}")

        auth_contexts: list[dict[str, Any]] = []
        try:
            auth_contexts, _ = await client.get_all(
                "/identity/conditionalAccess/authenticationContextClassReferences", top=0
            )
        except GraphError as exc:
            notes.append(f"Authentication contexts: {clip(exc, 120)}")

        # --- expand the groups these policies actually reference --------------------
        referenced: set[str] = set()
        for p in policies:
            conditions = as_dict(as_dict(p).get("conditions"))
            users = as_dict(conditions.get("users"))
            for key in ("includeGroups", "excludeGroups"):
                referenced.update(str(g) for g in as_list(users.get(key)) if g)
        group_members: dict[str, list[str]] = {}
        if referenced:
            await ctx.say("info", f"Conditional Access: expanding {len(referenced)} referenced group(s)…")
            group_members, gtrunc, gnotes = await expand_groups(
                client, sorted(referenced), cap=ctx.max_group_expansions
            )
            truncated = truncated or gtrunc
            notes.extend(gnotes)

        enabled = sum(1 for p in policies if str(as_dict(p).get("state")) == STATE_ENABLED)
        report_only = sum(1 for p in policies if str(as_dict(p).get("state")) == STATE_REPORT_ONLY)
        data = {
            "policies": [_slim_policy(as_dict(p)) for p in policies],
            "named_locations": [_slim_location(as_dict(loc)) for loc in locations],
            "auth_strengths": [
                {
                    "id": str(as_dict(s).get("id") or ""),
                    "display_name": str(as_dict(s).get("displayName") or ""),
                    "policy_type": str(as_dict(s).get("policyType") or ""),
                    "requirements_satisfied": str(as_dict(s).get("requirementsSatisfied") or ""),
                    "combinations": [str(c) for c in as_list(as_dict(s).get("allowedCombinations"))],
                }
                for s in strengths
            ],
            "auth_contexts": [
                {
                    "id": str(as_dict(c).get("id") or ""),
                    "display_name": str(as_dict(c).get("displayName") or ""),
                    "is_available": bool(as_dict(c).get("isAvailable")),
                }
                for c in auth_contexts
            ],
            "group_members": group_members,
            "counts": {
                "policies": len(policies),
                "enabled": enabled,
                "report_only": report_only,
                "disabled": len(policies) - enabled - report_only,
                "named_locations": len(locations),
                "auth_strengths": len(strengths),
            },
        }
        status = model.STATUS_PARTIAL if (notes or truncated) else model.STATUS_OK
        return model.domain_payload(
            DOMAIN, data, status=status, item_count=len(policies), truncated=truncated, notes=notes
        )

    return await guarded(DOMAIN, ctx, _run)


def _slim_policy(p: dict[str, Any]) -> dict[str, Any]:
    """Keep the full semantic content of a policy, drop only Graph noise.

    Deliberately conservative: the CA engine and the policy-as-code export both need the
    complete condition/grant/session tree, so nothing meaningful is discarded here."""
    conditions = as_dict(p.get("conditions"))
    users = as_dict(conditions.get("users"))
    apps = as_dict(conditions.get("applications"))
    platforms = as_dict(conditions.get("platforms"))
    locations = as_dict(conditions.get("locations"))
    devices = as_dict(conditions.get("devices"))
    grant = as_dict(p.get("grantControls"))
    session = as_dict(p.get("sessionControls"))
    return {
        "id": str(p.get("id") or ""),
        "display_name": str(p.get("displayName") or ""),
        "state": str(p.get("state") or ""),
        "created_at": str(p.get("createdDateTime") or ""),
        "modified_at": str(p.get("modifiedDateTime") or ""),
        "conditions": {
            "include_users": [str(x) for x in as_list(users.get("includeUsers"))],
            "exclude_users": [str(x) for x in as_list(users.get("excludeUsers"))],
            "include_groups": [str(x) for x in as_list(users.get("includeGroups"))],
            "exclude_groups": [str(x) for x in as_list(users.get("excludeGroups"))],
            "include_roles": [str(x) for x in as_list(users.get("includeRoles"))],
            "exclude_roles": [str(x) for x in as_list(users.get("excludeRoles"))],
            "include_guests": _guest_kinds(users.get("includeGuestsOrExternalUsers")),
            "exclude_guests": _guest_kinds(users.get("excludeGuestsOrExternalUsers")),
            "include_apps": [str(x) for x in as_list(apps.get("includeApplications"))],
            "exclude_apps": [str(x) for x in as_list(apps.get("excludeApplications"))],
            "user_actions": [str(x) for x in as_list(apps.get("includeUserActions"))],
            "auth_contexts": [str(x) for x in as_list(apps.get("includeAuthenticationContextClassReferences"))],
            # Application FILTER (custom security attributes on the resource), distinct from the
            # device filter below. Collected because a policy scoped this way looks like it
            # targets nothing when you read `includeApplications` alone — which is how an
            # attribute-scoped policy silently reads as "no application in scope".
            "application_filter_mode": str(as_dict(apps.get("applicationFilter")).get("mode") or ""),
            "application_filter_rule": str(as_dict(apps.get("applicationFilter")).get("rule") or ""),
            "client_app_types": [str(x) for x in as_list(conditions.get("clientAppTypes"))],
            # Authentication FLOWS (device-code flow, authentication transfer). Microsoft's
            # recommended "block device code flow" policy targets all users and all apps and
            # narrows ONLY on this. Not collecting it made that policy read as an
            # unconditional block on everyone, so the simulator reported every sign-in in the
            # tenant as blocked.
            "auth_flows": [
                str(x) for x in as_list(
                    as_dict(conditions.get("authenticationFlows")).get("transferMethods")
                )
            ] or [
                # Graph returns the transfer methods as a comma-joined string on some API
                # versions; normalize both shapes to a list.
                s.strip() for s in str(
                    as_dict(conditions.get("authenticationFlows")).get("transferMethods") or ""
                ).split(",") if s.strip()
            ],
            "platforms_include": [str(x) for x in as_list(platforms.get("includePlatforms"))],
            "platforms_exclude": [str(x) for x in as_list(platforms.get("excludePlatforms"))],
            "locations_include": [str(x) for x in as_list(locations.get("includeLocations"))],
            "locations_exclude": [str(x) for x in as_list(locations.get("excludeLocations"))],
            "device_filter_mode": str(as_dict(devices.get("deviceFilter")).get("mode") or ""),
            "device_filter_rule": str(as_dict(devices.get("deviceFilter")).get("rule") or ""),
            "sign_in_risk": [str(x) for x in as_list(conditions.get("signInRiskLevels"))],
            "user_risk": [str(x) for x in as_list(conditions.get("userRiskLevels"))],
            "service_principal_risk": [str(x) for x in as_list(conditions.get("servicePrincipalRiskLevels"))],
            "client_applications": _client_applications(conditions.get("clientApplications")),
        },
        "grant": {
            "operator": str(grant.get("operator") or "OR"),
            "controls": [str(x) for x in as_list(grant.get("builtInControls"))],
            "custom_controls": [str(x) for x in as_list(grant.get("customAuthenticationFactors"))],
            "terms_of_use": [str(x) for x in as_list(grant.get("termsOfUse"))],
            "auth_strength_id": str(as_dict(grant.get("authenticationStrength")).get("id") or ""),
            "auth_strength_name": str(as_dict(grant.get("authenticationStrength")).get("displayName") or ""),
            "present": bool(grant),
        },
        "session": {
            "sign_in_frequency": bool(as_dict(session.get("signInFrequency")).get("isEnabled")),
            "sign_in_frequency_value": as_dict(session.get("signInFrequency")).get("value"),
            "sign_in_frequency_type": str(as_dict(session.get("signInFrequency")).get("type") or ""),
            "persistent_browser": bool(as_dict(session.get("persistentBrowser")).get("isEnabled")),
            "persistent_browser_mode": str(as_dict(session.get("persistentBrowser")).get("mode") or ""),
            "app_enforced_restrictions": bool(as_dict(session.get("applicationEnforcedRestrictions")).get("isEnabled")),
            "cloud_app_security": bool(as_dict(session.get("cloudAppSecurity")).get("isEnabled")),
            "continuous_access_evaluation": str(
                as_dict(session.get("continuousAccessEvaluation")).get("mode") or ""
            ),
            "present": bool(session),
        },
    }


def _guest_kinds(raw: Any) -> list[str]:
    """``guestOrExternalUserTypes`` is a comma-joined enum string in Graph."""
    d = as_dict(raw)
    types = str(d.get("guestOrExternalUserTypes") or "")
    return [t for t in types.split(",") if t]


def _client_applications(raw: Any) -> dict[str, Any]:
    d = as_dict(raw)
    return {
        "include_service_principals": [str(x) for x in as_list(d.get("includeServicePrincipals"))],
        "exclude_service_principals": [str(x) for x in as_list(d.get("excludeServicePrincipals"))],
    }


def _slim_location(loc: dict[str, Any]) -> dict[str, Any]:
    odata = str(loc.get("@odata.type") or "")
    ranges = []
    for r in as_list(loc.get("ipRanges")):
        r = as_dict(r)
        ranges.append(str(r.get("cidrAddress") or ""))
    return {
        "id": str(loc.get("id") or ""),
        "display_name": str(loc.get("displayName") or ""),
        "kind": "country" if "country" in odata.lower() else "ip",
        "is_trusted": bool(loc.get("isTrusted")),
        "ip_ranges": [r for r in ranges if r],
        "countries": [str(c) for c in as_list(loc.get("countriesAndRegions"))],
        "include_unknown_countries": bool(loc.get("includeUnknownCountriesAndRegions")),
    }
