"""Which Microsoft Graph permissions this connection actually holds.

Two mechanisms, used together:

1. **Token claim inspection** (primary, zero API calls) — an app-only Graph token carries
   its granted application permissions in the ``roles`` claim. Decoding the JWT payload
   gives the exact list without touching the network.
2. **Live probe** (fallback / confirmation) — one ``$batch`` of ``$top=1`` GETs, one per
   domain, recording 200 vs 403. Used when the token has no ``roles`` claim (delegated
   tokens, pasted tokens) or when the caller asks for verification.

The output is a per-domain blindness map that flows into the snapshot, the score's coverage
calculation and every screen's coverage banner. A missing permission must degrade exactly
one pillar — never the page.

No secret ever leaves this module: the JWT is decoded, not validated (we are reading our
own token's claims, not authenticating anyone), and only claim names are retained.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from app.entra.graphclient import GraphClient, GraphError, GraphRequest

log = logging.getLogger("app.entra.permissions")

# The three consent tiers documented in docs/ENTRA_SETUP.md. Every scope is read-only.
TIER_1 = (
    "Directory.Read.All",
    "Application.Read.All",
    "Policy.Read.All",
    "RoleManagement.Read.Directory",
    "Organization.Read.All",
)
TIER_2 = (
    "AuditLog.Read.All",
    "Reports.Read.All",
    "UserAuthenticationMethod.Read.All",
    "Group.Read.All",
    "GroupMember.Read.All",
    "Policy.Read.PermissionGrant",
    "Device.Read.All",
    "Synchronization.Read.All",
    "SecurityEvents.Read.All",
)
TIER_3 = (
    "RoleManagementPolicy.Read.Directory",
    # PIM activation HISTORY (who elevated, when) needs its own scope. Without it listed
    # here the setup screen reported "11/11 granted · measured" while the coverage banner
    # still declared PIM activity a blind spot — two screens contradicting each other.
    "RoleAssignmentSchedule.Read.Directory",
    "PrivilegedAccess.Read.AzureAD",
    "PrivilegedAccess.Read.AzureADGroup",
    "IdentityRiskyUser.Read.All",
    "IdentityRiskEvent.Read.All",
    "IdentityRiskyServicePrincipal.Read.All",
    "AccessReview.Read.All",
    "EntitlementManagement.Read.All",
    "LifecycleWorkflows.Read.All",
    "OnPremDirectorySynchronization.Read.All",
    "DirectoryRecommendations.Read.All",
    # External identity providers for guests (social and SAML/WS-Fed). Nothing else grants
    # it: `/identity/identityProviders` answers 403 even with Directory.Read.All, so without
    # this the guest sign-in perimeter is simply invisible.
    "IdentityProvider.Read.All",
)
ALL_SCOPES = TIER_1 + TIER_2 + TIER_3

TIERS: list[dict[str, Any]] = [
    {"tier": 1, "name": "Minimum viable", "scopes": list(TIER_1),
     "unlocks": "Posture score, applications, directory roles and the Conditional Access inventory."},
    {"tier": 2, "name": "Recommended", "scopes": list(TIER_2),
     "unlocks": "Dormancy, MFA registration truth, consent posture, change history and sign-in analysis."},
    {"tier": 3, "name": "Complete", "scopes": list(TIER_3),
     "unlocks": "PIM depth, Identity Protection risk, access reviews, entitlement, lifecycle "
                "workflows and the external identity providers guests sign in with."},
]

# Which permissions each collector domain needs. A domain is blind when NONE of the
# alternatives in a requirement group are held. ``Directory.Read.All`` is a superset of
# several narrower scopes, which is why the groups are alternatives rather than a flat list.
DOMAIN_REQUIREMENTS: dict[str, list[tuple[str, ...]]] = {
    "tenant": [("Organization.Read.All", "Directory.Read.All"), ("Policy.Read.All",)],
    "people": [("User.Read.All", "Directory.Read.All"), ("Group.Read.All", "Directory.Read.All")],
    "apps": [("Application.Read.All", "Directory.Read.All")],
    "roles": [("RoleManagement.Read.Directory", "Directory.Read.All")],
    "ca": [("Policy.Read.All",)],
    "pim": [("RoleManagementPolicy.Read.Directory", "PrivilegedAccess.Read.AzureAD", "RoleManagement.Read.Directory")],
    # Schedule INSTANCES read with the ordinary role-management scope, and Azure PIM needs no
    # Graph scope at all, so this domain is never fully blind on a tenant that can read roles.
    # RoleAssignmentSchedule.Read.Directory only adds justification, ticket and requestor.
    "activations": [("RoleManagement.Read.Directory", "RoleAssignmentSchedule.Read.Directory",
                     "PrivilegedAccess.Read.AzureAD")],
    "risk": [("IdentityRiskyUser.Read.All",)],
    "governance": [("AccessReview.Read.All", "EntitlementManagement.Read.All")],
    "devices": [("Device.Read.All", "Directory.Read.All")],
    "hybrid": [("Organization.Read.All", "Directory.Read.All")],
}

# What a probe response is evidence OF. Only a 403 says anything about consent; treating
# every other failure as one tells operators to grant scopes that cannot help.
PROBE_PERMITTED = "permitted"
PROBE_DENIED = "denied"
PROBE_UNLICENSED = "unlicensed"
PROBE_INCONCLUSIVE = "inconclusive"

# Graph reports a missing Entra ID P2 / Governance license as a 400 with a message, not a
# 403 — the same quirk the roles collector already has to handle.
_LICENCE_MARKERS = (
    "aadpremiumlicenserequired",
    "premium license",
    "premium licence",
    "insufficient license",
    "governance license",
    "governance licence",
)


def classify_probe(status: int, code: str = "", message: str = "") -> str:
    """Decide what one probe response proves about PERMISSION specifically."""
    if 200 <= status < 300:
        return PROBE_PERMITTED
    # Checked BEFORE the 403 rule: Graph answers a missing Entra ID Governance license with
    # a 403 on some collections and a 400 on others, so the status alone cannot distinguish
    # "grant consent" from "buy a license".
    haystack = f"{code} {message}".lower()
    if any(marker in haystack for marker in _LICENCE_MARKERS):
        return PROBE_UNLICENSED
    if status == 403:
        return PROBE_DENIED
    return PROBE_INCONCLUSIVE


# One cheap GET per domain for the live probe.
_PROBE_URLS: dict[str, str] = {
    "tenant": "/organization?$select=id&$top=1",
    "people": "/users?$select=id&$top=1",
    "apps": "/applications?$select=id&$top=1",
    "ca": "/identity/conditionalAccess/policies?$select=id&$top=1",
    "pim": "/roleManagement/directory/roleEligibilitySchedules?$select=id&$top=1",
    # No `$top`: this collection rejects it outright. `$top=1` answers 400 "This resource
    # requires a minimum page size of 20" and `$top=20` answers 400 "Invalid/unsupported
    # query request" — so ANY paged probe here fails on a tenant that can read roles
    # perfectly well, and the roles domain (the privileged-access pillar) was reported
    # unpermitted on every first collection.
    "roles": "/roleManagement/directory/roleDefinitions?$select=id",
    "activations": "/roleManagement/directory/roleAssignmentScheduleInstances?$select=id&$top=1",
    "risk": "/identityProtection/riskyUsers?$select=id&$top=1",
    "governance": "/identityGovernance/accessReviews/definitions?$select=id&$top=1",
    "devices": "/devices?$select=id&$top=1",
    "hybrid": "/organization?$select=onPremisesSyncEnabled&$top=1",
}


def decode_token_roles(token: str) -> tuple[list[str], str]:
    """Extract the ``roles`` (application permissions) claim from a JWT.

    Returns ``(roles, error)``. The token is *decoded*, never validated — we are reading
    the claims of a token we just acquired ourselves.
    """
    if not token or token.count(".") < 2:
        return [], "Token is not a JWT (no claims to read)."
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
        claims = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        return [], f"Could not decode token claims: {exc}"
    if not isinstance(claims, dict):
        return [], "Token claims were not an object."
    roles = claims.get("roles")
    if isinstance(roles, list):
        return [str(r) for r in roles], ""
    scp = claims.get("scp")
    if isinstance(scp, str) and scp.strip():
        # Delegated token: scopes are space-separated and named without the resource prefix.
        return [s for s in scp.split(" ") if s], ""
    return [], "Token carries no application-permission (roles) claim."


def evaluate_domains(
    granted: list[str], *, probe: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map granted scopes -> per-domain ``{ok, missing, reason}``.

    A live probe refines the claim-based verdict, but only where it is actually evidence
    ABOUT PERMISSION:

    * ``permitted``    a 200 proves the read works whatever the claims said -> clear it.
    * ``denied``       a 403 proves it does not -> mark it blind.
    * ``unlicensed``   a license error says nothing about consent, so the permission verdict
                       is left alone and the collector reports the license itself. No amount
                       of granting will fix it, and telling someone to grant a scope they
                       already hold sends them in a circle.
    * ``inconclusive`` a malformed query, a throttle or an outage. Trusting it would blame
                       the operator's permissions for our own bug — which is exactly what a
                       bad ``$top`` on roleDefinitions did to the privileged-access pillar.
    """
    held = {g for g in granted}
    out: dict[str, dict[str, Any]] = {}
    for domain, groups in DOMAIN_REQUIREMENTS.items():
        missing: list[str] = []
        for alternatives in groups:
            if not (held & set(alternatives)):
                missing.append(" or ".join(alternatives))
        ok = not missing
        reason = "" if ok else "Missing " + "; ".join(missing)
        entry: dict[str, Any] = {"ok": ok, "missing": missing, "reason": reason}

        state = (probe or {}).get(domain)
        if state:
            verdict = str(state.get("verdict") or "")
            entry["probe_status"] = state.get("status")
            entry["probe_verdict"] = verdict
            if verdict == PROBE_PERMITTED and not ok:
                entry.update(ok=True, missing=[], reason="")
            elif verdict == PROBE_DENIED:
                entry["ok"] = False
                entry["reason"] = reason or f"Microsoft Graph refused this read (403) for {domain}."
            elif verdict == PROBE_UNLICENSED:
                entry["licence_blocked"] = True
                entry["licence_reason"] = str(
                    state.get("message") or "Requires a higher Entra ID licence.")
        out[domain] = entry
    return out


async def probe_live(
    client: GraphClient, domains: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """One ``$batch`` of cheap GETs -> ``{domain: {status, verdict, code, message}}``.

    Never raises. Only domains that produced a response appear in the result; a domain that
    told us nothing must not be mistaken for one that failed.
    """
    names = [d for d in (domains or list(_PROBE_URLS)) if d in _PROBE_URLS]
    if not names:
        return {}
    reqs = [GraphRequest(id=d, url=_PROBE_URLS[d]) for d in names]
    try:
        responses = await client.batch(reqs)
    except GraphError as exc:
        log.info("entra live permission probe failed: %s", exc)
        return {}
    except Exception:  # noqa: BLE001 - probing is best-effort
        log.warning("entra live permission probe crashed", exc_info=True)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for response in responses:
        if not response.status:
            continue
        body = response.body if isinstance(response.body, dict) else {}
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str((error or {}).get("code") or "")
        message = str((error or {}).get("message") or "")
        out[response.id] = {
            "status": response.status,
            "verdict": classify_probe(response.status, code, message),
            "code": code,
            "message": message[:300],
        }
    return out


async def build(client: GraphClient, *, live: bool = False) -> dict[str, Any]:
    """Full permission state for a connection: granted scopes + per-domain blindness."""
    token, token_err = await client.probe_token()
    if not token:
        blind = {d: {"ok": False, "missing": list(DOMAIN_REQUIREMENTS[d][0]), "reason": token_err}
                 for d in DOMAIN_REQUIREMENTS}
        return {
            "token_ok": False,
            "token_error": token_err,
            "granted": [],
            "granted_known": False,
            "claim_error": token_err,
            "domains": blind,
            "tiers": _tier_state([]),
            "probed": False,
        }

    granted, claim_err = decode_token_roles(token)
    probe = await probe_live(client) if (live or not granted) else None
    domains = evaluate_domains(granted, probe=probe)
    return {
        "token_ok": True,
        "token_error": "",
        "granted": sorted(granted),
        "granted_known": bool(granted),
        "claim_error": claim_err,
        "domains": domains,
        "tiers": _tier_state(granted),
        "probed": probe is not None,
    }


def _tier_state(granted: list[str]) -> list[dict[str, Any]]:
    held = set(granted)
    out = []
    for tier in TIERS:
        scopes = tier["scopes"]
        have = [s for s in scopes if s in held]
        out.append({
            **tier,
            "granted": have,
            "missing": [s for s in scopes if s not in held],
            "complete": len(have) == len(scopes),
        })
    return out


def blind_domains(permissions: dict[str, Any]) -> set[str]:
    """Domains the connection cannot read at all."""
    return {d for d, state in (permissions.get("domains") or {}).items() if not state.get("ok")}
