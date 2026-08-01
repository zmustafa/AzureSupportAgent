"""Tenant-level profile and policy collector.

Covers the settings that are single objects rather than collections, and that carry
disproportionate risk: who may consent to applications, who may invite guests, what guests
can read, which authentication methods are enabled tenant-wide, and whether the directory
is synchronised from on-premises.

Every sub-call is independently fail-open: `/policies/authenticationMethodsPolicy` returning
403 must not lose the `/organization` profile we already read.
"""
from __future__ import annotations

from typing import Any

from app.entra import federation, model
from app.entra.collectors import CollectContext, as_dict, as_list, clip, guarded
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "tenant"

_ORG_SELECT = [
    "id", "displayName", "verifiedDomains", "onPremisesSyncEnabled",
    "onPremisesLastSyncDateTime", "technicalNotificationMails", "createdDateTime",
    "tenantType", "countryLetterCode",
]

# `/organization` carries verified domain NAMES and nothing about how they authenticate.
# `/domains` is where the authentication type lives, and it is the difference between a
# tenant that signs its own users in and one that delegates the job to somebody else.
_DOMAIN_SELECT = [
    "id", "authenticationType", "isVerified", "isDefault", "isInitial", "isRoot",
    "supportedServices", "passwordValidityPeriodInDays", "passwordNotificationWindowInDays",
]


async def _one(client: GraphClient, path: str, notes: list[str], label: str) -> dict[str, Any]:
    """GET a singleton policy object, tolerating both object and collection shapes."""
    try:
        body = await client.get(path)
    except GraphPermissionError as exc:
        notes.append(f"{label}: not permitted ({clip(exc.message, 120)})")
        return {}
    except GraphError as exc:
        notes.append(f"{label}: {clip(exc, 160)}")
        return {}
    if isinstance(body, dict) and isinstance(body.get("value"), list):
        vals = body["value"]
        return as_dict(vals[0]) if vals else {}
    return as_dict(body)


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        await ctx.say("info", "Tenant: reading organisation profile and tenant policies…")

        orgs, _ = await client.get_all("/organization", select=_ORG_SELECT, top=0)
        org = as_dict(orgs[0]) if orgs else {}

        authorization = await _one(client, "/policies/authorizationPolicy", notes, "authorizationPolicy")
        auth_methods = await _one(client, "/policies/authenticationMethodsPolicy", notes, "authenticationMethodsPolicy")
        admin_consent = await _one(client, "/policies/adminConsentRequestPolicy", notes, "adminConsentRequestPolicy")
        cross_tenant = await _one(client, "/policies/crossTenantAccessPolicy/default", notes, "crossTenantAccessPolicy")

        grant_policies: list[dict[str, Any]] = []
        try:
            grant_policies, _ = await client.get_all(
                "/policies/permissionGrantPolicies", select=["id", "displayName"], top=0
            )
        except GraphPermissionError as exc:
            notes.append(f"permissionGrantPolicies: not permitted ({clip(exc.message, 120)})")
        except GraphError as exc:
            notes.append(f"permissionGrantPolicies: {clip(exc, 160)}")

        domains = [
            {
                "name": d.get("name", ""),
                "is_default": bool(d.get("isDefault")),
                "is_initial": bool(d.get("isInitial")),
                "type": d.get("type", ""),
            }
            for d in as_list(org.get("verifiedDomains"))
        ]
        primary = next((d["name"] for d in domains if d["is_default"]), "")

        fabric = await _identity_fabric(client, ctx, notes)

        data = {
            "tenant": {
                "id": org.get("id", "") or ctx.tenant_id,
                "display_name": org.get("displayName", ""),
                "primary_domain": primary,
                "domains": domains,
                "country": org.get("countryLetterCode", ""),
                "created_at": org.get("createdDateTime", ""),
                "technical_contacts": as_list(org.get("technicalNotificationMails")),
            },
            "hybrid": {
                "sync_enabled": bool(org.get("onPremisesSyncEnabled")),
                "last_sync": org.get("onPremisesLastSyncDateTime", "") or "",
                **fabric["hybrid"],
            },
            "identity_fabric": fabric["fabric"],
            "authorization_policy": _authorization(authorization),
            "authentication_methods_policy": _auth_methods(auth_methods),
            "admin_consent_policy": {
                "is_enabled": bool(admin_consent.get("isEnabled")),
                "notify_reviewers": bool(admin_consent.get("notifyReviewers")),
                "reviewers": len(as_list(admin_consent.get("reviewers"))),
                "present": bool(admin_consent),
            },
            "cross_tenant_default": _cross_tenant(cross_tenant),
            "permission_grant_policies": [
                {"id": p.get("id", ""), "display_name": p.get("displayName", "")} for p in grant_policies
            ],
        }
        status = model.STATUS_PARTIAL if notes else model.STATUS_OK
        federated = data["identity_fabric"]["federated_count"]
        await ctx.say("ok", f"Tenant: {data['tenant']['display_name'] or ctx.tenant_id} "
                            f"({len(domains)} verified domain(s)"
                            + (f", {federated} federated" if federated else "") + ")")
        return model.domain_payload(
            DOMAIN, data, status=status, item_count=1 + len(domains), notes=notes
        )

    return await guarded(DOMAIN, ctx, _run)


async def _identity_fabric(
    client: GraphClient, ctx: CollectContext, notes: list[str]
) -> dict[str, Any]:
    """Domain authentication types, federation trusts, and on-premises sync features.

    Three reads on a normal tenant, because `/domains/{id}/federationConfiguration` is only
    fetched for domains that say they are federated — usually none or one.

    Each piece degrades on its own. A tenant that cannot read the sync object still gets its
    federation trusts, and vice versa; the payload records which piece is blind rather than
    reporting an empty perimeter as a clean one.
    """
    fabric: dict[str, Any] = {
        "domains": [], "federation": [], "federated_count": 0, "managed_count": 0,
        "readable": False, "blind_reason": "",
        # External identity providers guests sign in with (social, SAML, WS-Fed). Its own
        # readability flag: it needs a scope nothing else does, so it is blind on its own
        # long after the rest of the perimeter is legible.
        "external_idps": [], "external_idps_readable": False, "external_idps_reason": "",
    }
    hybrid: dict[str, Any] = {"features_readable": False}

    try:
        rows, _ = await client.get_all("/domains", select=_DOMAIN_SELECT, top=0)
        fabric["domains"] = [federation.normalise_domain(as_dict(d)) for d in rows]
        fabric["readable"] = True
    except GraphPermissionError as exc:
        fabric["blind_reason"] = f"Domain.Read.All or Directory.Read.All ({clip(exc.message, 100)})"
        notes.append(f"domains: not permitted ({clip(exc.message, 120)})")
    except GraphError as exc:
        fabric["blind_reason"] = clip(exc, 160)
        notes.append(f"domains: {clip(exc, 160)}")

    federated = [d for d in fabric["domains"] if d["federated"]]
    fabric["federated_count"] = len(federated)
    fabric["managed_count"] = len(fabric["domains"]) - len(federated)

    for dom in federated:
        name = dom["name"]
        try:
            body = await client.get(f"/domains/{name}/federationConfiguration")
        except GraphError as exc:
            notes.append(f"federationConfiguration({name}): {clip(exc, 140)}")
            continue
        for cfg in as_list(as_dict(body).get("value")) or [as_dict(body)]:
            if not cfg:
                continue
            fabric["federation"].append(federation.normalise_federation(name, as_dict(cfg)))

    try:
        idps, _ = await client.get_all("/identity/identityProviders", top=0)
        fabric["external_idps"] = [federation.normalise_idp(as_dict(i)) for i in idps]
        fabric["external_idps_readable"] = True
    except GraphPermissionError as exc:
        fabric["external_idps_reason"] = "IdentityProvider.Read.All"
        notes.append(f"identityProviders: not permitted ({clip(exc.message, 120)})")
    except GraphError as exc:
        fabric["external_idps_reason"] = clip(exc, 140)
        notes.append(f"identityProviders: {clip(exc, 160)}")

    try:
        body = await client.get("/directory/onPremisesSynchronization")
        rows = as_list(as_dict(body).get("value"))
        sync = as_dict(rows[0]) if rows else {}
        features = as_dict(sync.get("features"))
        deletion = as_dict(as_dict(sync.get("configuration")).get("accidentalDeletionPrevention"))
        hybrid = {
            "features_readable": bool(sync),
            "password_sync": bool(features.get("passwordSyncEnabled")),
            "password_writeback": bool(features.get("passwordWritebackEnabled")),
            "user_writeback": bool(features.get("userWritebackEnabled")),
            "group_writeback": bool(features.get("groupWriteBackEnabled")
                                    or features.get("unifiedGroupWritebackEnabled")),
            "device_writeback": bool(features.get("deviceWritebackEnabled")),
            "cloud_password_policy": bool(features.get("cloudPasswordPolicyForPasswordSyncedUsersEnabled")),
            "block_soft_match": bool(features.get("blockSoftMatchEnabled")),
            "block_cloud_object_takeover": bool(features.get("blockCloudObjectTakeoverThroughHardMatchEnabled")),
            "deletion_prevention": {
                "type": deletion.get("synchronizationPreventionType", "") or "",
                "threshold": deletion.get("alertThreshold"),
            },
        }
    except GraphPermissionError as exc:
        notes.append(f"onPremisesSynchronization: not permitted ({clip(exc.message, 120)})")
    except GraphError as exc:
        notes.append(f"onPremisesSynchronization: {clip(exc, 160)}")

    return {"fabric": fabric, "hybrid": hybrid}


def _authorization(policy: dict[str, Any]) -> dict[str, Any]:
    """Decode the consent / guest-invite / guest-access settings.

    ``defaultUserRolePermissions.permissionGrantPoliciesAssigned`` is the actual user-consent
    control: an empty list means users cannot consent at all; a policy id ending
    ``-low`` limits them to low-risk permissions from verified publishers; the
    ``ManagePermissionGrantsForSelf.microsoft-user-default-legacy`` value is the
    unrestricted "users can consent to any app" setting.
    """
    perms = as_dict(policy.get("defaultUserRolePermissions"))
    assigned = [str(p) for p in as_list(perms.get("permissionGrantPoliciesAssigned"))]
    unrestricted = any("legacy" in a for a in assigned)
    restricted_low = any(a.endswith("-low") for a in assigned)
    return {
        "present": bool(policy),
        "guest_user_role_id": str(policy.get("guestUserRoleId") or ""),
        "allow_invites_from": str(policy.get("allowInvitesFrom") or ""),
        "allow_email_verified_users_to_join": bool(policy.get("allowEmailVerifiedUsersToJoinOrganization")),
        "block_msol_powershell": bool(policy.get("blockMsolPowerShell")),
        "user_consent_policies": assigned,
        "user_consent_unrestricted": unrestricted,
        "user_consent_restricted_low_risk": restricted_low,
        "user_consent_disabled": len(assigned) == 0,
        "allowed_to_create_apps": bool(perms.get("allowedToCreateApps", True)),
        "allowed_to_create_security_groups": bool(perms.get("allowedToCreateSecurityGroups", True)),
        "allowed_to_read_other_users": bool(perms.get("allowedToReadOtherUsers", True)),
    }


# Guest access levels (guestUserRoleId well-known GUIDs).
GUEST_ROLE_SAME_AS_MEMBER = "a0b1b346-4d3e-4e8b-98f8-753987be4970"
GUEST_ROLE_LIMITED = "10dae51f-b6af-4016-8d66-8c2a99b929b3"
GUEST_ROLE_RESTRICTED = "2af84b1e-32c8-42b7-82bc-daa82404023b"

_GUEST_ROLE_LABEL = {
    GUEST_ROLE_SAME_AS_MEMBER: "Same as member users",
    GUEST_ROLE_LIMITED: "Limited access (default)",
    GUEST_ROLE_RESTRICTED: "Restricted access (most restrictive)",
}


def guest_access_label(role_id: str) -> str:
    return _GUEST_ROLE_LABEL.get(role_id, "Unknown")


def _auth_methods(policy: dict[str, Any]) -> dict[str, Any]:
    configs = as_list(policy.get("authenticationMethodConfigurations"))
    enabled: dict[str, bool] = {}
    for cfg in configs:
        cfg = as_dict(cfg)
        cid = str(cfg.get("id") or "")
        if cid:
            enabled[cid] = str(cfg.get("state") or "").lower() == "enabled"
    return {
        "present": bool(policy),
        "registration_campaign": bool(as_dict(policy.get("registrationEnforcement")).get("authenticationMethodsRegistrationCampaign")),
        "methods": enabled,
        "sms_enabled": bool(enabled.get("Sms")),
        "voice_enabled": bool(enabled.get("Voice")),
        "fido2_enabled": bool(enabled.get("Fido2")),
        "authenticator_enabled": bool(enabled.get("MicrosoftAuthenticator")),
        "tap_enabled": bool(enabled.get("TemporaryAccessPass")),
        "email_otp_enabled": bool(enabled.get("Email")),
    }


def _cross_tenant(policy: dict[str, Any]) -> dict[str, Any]:
    inbound = as_dict(policy.get("b2bCollaborationInbound"))
    users = as_dict(inbound.get("usersAndGroups"))
    apps = as_dict(inbound.get("applications"))
    return {
        "present": bool(policy),
        "inbound_trust_mfa": bool(as_dict(policy.get("inboundTrust")).get("isMfaAccepted")),
        "inbound_trust_compliant_device": bool(as_dict(policy.get("inboundTrust")).get("isCompliantDeviceAccepted")),
        "b2b_inbound_users": str(users.get("accessType") or ""),
        "b2b_inbound_apps": str(apps.get("accessType") or ""),
        "automatic_redemption_inbound": bool(
            as_dict(as_dict(policy.get("automaticUserConsentSettings")).get("inboundAllowed") or {}) or
            policy.get("automaticUserConsentSettings", {}).get("inboundAllowed")
        ),
    }
