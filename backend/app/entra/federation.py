"""Federation and hybrid identity — the tenant's authentication perimeter.

A tenant with a federated domain does not authenticate its own users. Someone else does,
and Entra takes their word for it. That single fact changes how half of this product should
be read: MFA registration looks catastrophic because the MFA is happening at the identity
provider, "no password hash sync" stops being a preference and becomes the reason a leaked
credential can never be detected, and the identity provider's signing certificate becomes a
tenant-wide outage waiting for a date.

None of it was visible before, because the tenant collector read ``/organization`` and took
the domain NAMES from it — a list of strings with no authentication type attached.

Everything here is pure: parsing, fingerprinting and classification, so it can be tested
without a tenant and without a network.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("app.entra.federation")

# --------------------------------------------------------------------------- vendors
# Matched against the issuer URI and every federation endpoint host. Ordered: the first
# hit wins, so the specific patterns sit above the generic ones.
#
# An unrecognized provider is reported as unrecognized WITH its host. A guess here would be
# worse than silence — "Okta" printed under a tenant that federates to something else is a
# claim about the authentication perimeter, and being confidently wrong about that is the
# one thing this feature must not do.
_VENDOR_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pingfederate", "PingFederate", ("pingfederate", "/pf/", "pf.", "sts_mex.ping")),
    ("pingone", "PingOne", ("pingone", "pingidentity.com")),
    ("okta", "Okta", ("okta.com", "oktapreview.com", "okta-emea.com", "/okta")),
    ("adfs", "Active Directory Federation Services", ("/adfs/services/trust", "/adfs/ls", "adfs.")),
    ("onelogin", "OneLogin", ("onelogin.com",)),
    ("shibboleth", "Shibboleth", ("shibboleth",)),
    ("duo", "Duo", ("duosecurity.com",)),
    ("google", "Google Workspace", ("accounts.google.com", "google.com/a/")),
    ("cyberark", "CyberArk Identity", ("idaptive.app", "cyberark.app", "my.idaptive")),
    ("auth0", "Auth0", ("auth0.com",)),
    ("keycloak", "Keycloak", ("/auth/realms", "keycloak")),
    ("entrust", "Entrust", ("entrust.net", "entrust.com")),
    ("sailpoint", "SailPoint", ("sailpoint.com",)),
    ("ibm", "IBM Security Verify", ("verify.ibm.com", "ice.ibmcloud.com")),
    ("oracle", "Oracle Identity Cloud", ("oraclecloud.com/fed", "idcs.")),
    ("secureauth", "SecureAuth", ("secureauth.com",)),
)

UNKNOWN_VENDOR = "unrecognised"


def fingerprint_vendor(issuer_uri: str, *endpoints: str) -> dict[str, str]:
    """Name the identity provider behind a federated domain.

    Reads the issuer first — it is the field an administrator actually configured, and on
    PingFederate it literally contains the product name — then falls back to the endpoint
    hosts, which is what ADFS and the SaaS providers give away.
    """
    haystack = " ".join(x.lower() for x in (issuer_uri, *endpoints) if x)
    for key, label, needles in _VENDOR_PATTERNS:
        if any(n in haystack for n in needles):
            return {"key": key, "label": label}
    return {"key": UNKNOWN_VENDOR, "label": "Unrecognized provider"}


def endpoint_host(*uris: str) -> str:
    """The host an administrator would recognize, from whichever endpoint has one."""
    for uri in uris:
        if not uri:
            continue
        host = urlparse(uri).hostname or ""
        if host:
            return host
    return ""


# ----------------------------------------------------------------------- certificates
def certificate_facts(raw: str) -> dict[str, Any]:
    """Subject, issuer, thumbprint and expiry from a base64 signing certificate.

    The certificate itself is parsed and dropped on the floor. Only these derived facts
    travel any further: the same rule this product already applies to application
    credentials, where the expiry is the finding and the key material is never read.

    An unparseable value is reported as unparseable rather than raising \u2014 a malformed
    certificate must not take the whole tenant collection down with it.
    """
    if not raw:
        return {}
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(base64.b64decode(raw))
    except Exception as exc:  # noqa: BLE001 - any malformed input lands here
        log.info("federation: signing certificate could not be parsed: %s", exc)
        return {"parsed": False}

    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    days_left = (not_after - datetime.now(timezone.utc)).days
    return {
        "parsed": True,
        "subject": _rfc4514(cert.subject),
        "issuer": _rfc4514(cert.issuer),
        "thumbprint": cert.fingerprint(_sha1()).hex().upper(),
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "days_left": days_left,
        "expired": days_left < 0,
    }


def _sha1():
    from cryptography.hazmat.primitives import hashes

    # SHA-1 because that is what a certificate thumbprint IS in every Microsoft console an
    # operator will compare this against — the Entra portal, AD FS, and the provider's own
    # console all display the SHA-1 fingerprint, so a SHA-256 digest here would be a value
    # nobody could match against anything. It is an identifier, never a security claim:
    # nothing is verified, compared or trusted on the strength of it, and the certificate's
    # own validity is established by its dates and its issuer, not by this string.
    return hashes.SHA1()  # noqa: S303 - thumbprint identity, not integrity  # nosec B303


def _rfc4514(name: Any) -> str:
    try:
        return name.rfc4514_string()
    except Exception:  # noqa: BLE001 - defensive: odd DNs must not break the row
        return str(name)


# ------------------------------------------------------------------------- behaviors
# What Entra does with an MFA claim from the federated identity provider. The default when
# unset is the permissive one, which is why an empty string is reported as a real state
# rather than "not configured".
MFA_BEHAVIOUR = {
    "acceptIfMfaDoneByFederatedIdp": {
        "label": "Entra accepts MFA performed by the identity provider",
        "trusted": True,
    },
    "enforceMfaByFederatedIdp": {
        "label": "Entra requires the identity provider to perform MFA",
        "trusted": False,
    },
    "rejectMfaByFederatedIdp": {
        "label": "Entra performs MFA itself and rejects the provider's claim",
        "trusted": False,
    },
}
DEFAULT_MFA_BEHAVIOUR = "acceptIfMfaDoneByFederatedIdp"


def mfa_behaviour(value: str) -> dict[str, Any]:
    key = value or DEFAULT_MFA_BEHAVIOUR
    meta = MFA_BEHAVIOUR.get(key) or {"label": key, "trusted": False}
    return {"value": key, "explicit": bool(value), **meta}


# ------------------------------------------------------------------------ normalising
def normalize_domain(raw: dict[str, Any]) -> dict[str, Any]:
    """One row of `/domains`, trimmed to what a reader can act on."""
    return {
        "name": raw.get("id", "") or "",
        "authentication_type": raw.get("authenticationType", "") or "",
        "federated": str(raw.get("authenticationType", "")).lower() == "federated",
        "is_default": bool(raw.get("isDefault")),
        "is_initial": bool(raw.get("isInitial")),
        "is_verified": bool(raw.get("isVerified")),
        "is_root": bool(raw.get("isRoot")),
        "supported_services": list(raw.get("supportedServices") or []),
        # Managed domains only. 0 or absent means passwords never expire, which is a policy
        # decision worth showing rather than a blank cell.
        "password_validity_days": raw.get("passwordValidityPeriodInDays"),
        "password_notification_days": raw.get("passwordNotificationWindowInDays"),
    }


def normalize_federation(domain: str, raw: dict[str, Any]) -> dict[str, Any]:
    """One `/domains/{id}/federationConfiguration` entry, plus everything derived from it."""
    issuer = str(raw.get("issuerUri") or "")
    passive = str(raw.get("passiveSignInUri") or "")
    active = str(raw.get("activeSignInUri") or "")
    sign_out = str(raw.get("signOutUri") or "")
    metadata = str(raw.get("metadataExchangeUri") or "")
    status = raw.get("signingCertificateUpdateStatus") or {}
    result = str(status.get("certificateUpdateResult") or "")
    return {
        "domain": domain,
        "display_name": raw.get("displayName") or "",
        "issuer_uri": issuer,
        "passive_sign_in_uri": passive,
        "active_sign_in_uri": active,
        "sign_out_uri": sign_out,
        "metadata_exchange_uri": metadata,
        "host": endpoint_host(passive, active, sign_out, metadata, issuer),
        "vendor": fingerprint_vendor(issuer, passive, active, sign_out, metadata),
        "protocol": raw.get("preferredAuthenticationProtocol", "") or "",
        "mfa_behaviour": mfa_behaviour(str(raw.get("federatedIdpMfaBehavior") or "")),
        "signed_request_required": raw.get("isSignedAuthenticationRequestRequired"),
        "prompt_login_behavior": raw.get("promptLoginBehavior", "") or "",
        "certificate": certificate_facts(str(raw.get("signingCertificate") or "")),
        "next_certificate": certificate_facts(str(raw.get("nextSigningCertificate") or "")),
        "auto_rollover": {
            "result": result,
            "last_run": status.get("lastRunDateTime", "") or "",
            # "NotFound" is Entra saying it looked for a certificate to roll and found
            # none — the rollover it advertises is not actually managing this trust.
            "healthy": result.lower() in ("success", "successful"),
        },
    }


def population(users: list[dict[str, Any]], domain: str) -> int:
    """How many users sign in through this domain.

    An issuer URI on its own is trivia. The same URI with "73% of the directory" attached
    is a statement about blast radius, which is the only reason the row is on screen.
    """
    needle = f"@{domain.lower()}"
    return sum(1 for u in users if str(u.get("upn") or "").lower().endswith(needle))


# ------------------------------------------------------------------ external providers
# How Graph names the built-in social providers, mapped to something a reader recognizes.
_IDP_LABELS = {
    "socialIdentityProvider": "Social",
    "builtInIdentityProvider": "Built-in",
    "samlOrWsFedExternalDomainFederation": "SAML / WS-Fed domain",
    "appleManagedIdentityProvider": "Apple",
    "openIdConnectIdentityProvider": "OpenID Connect",
}


def normalize_idp(raw: dict[str, Any]) -> dict[str, Any]:
    """One `/identity/identityProviders` entry.

    These are the providers GUESTS authenticate with, which is a different perimeter from
    domain federation and worth keeping separate: a tenant can authenticate its own staff
    entirely in the cloud and still accept Google or a partner's SAML for external users.

    The client secret is never requested and never stored — only the identifier, which is
    what an administrator needs to recognize the registration.
    """
    kind = str(raw.get("@odata.type") or "").split(".")[-1]
    return {
        "id": raw.get("id", "") or "",
        "display_name": raw.get("displayName", "") or "",
        "kind": kind,
        "kind_label": _IDP_LABELS.get(kind, kind or "Identity provider"),
        "identity_provider_type": raw.get("identityProviderType", "") or "",
        "client_id": raw.get("clientId", "") or "",
        "issuer_uri": raw.get("issuerUri", "") or "",
        "domain": raw.get("domainName", "") or "",
    }
