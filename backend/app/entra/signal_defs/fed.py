"""Federation pillar checks — the tenant's authentication perimeter.

A federated domain means Entra is not the authenticator. Someone else is, and every claim
this product makes about authentication for those users is really a claim about a system it
cannot see. These signals say so, and flag the three ways that arrangement fails in
practice: Entra trusting an MFA claim it cannot verify, a signing certificate nobody is
managing, and no password hash sync to fall back on when the provider is unreachable.

The pillars are shared rather than new: MFA trust and password hash sync are authentication
questions, certificate lifecycle and protocol choice are hybrid-plumbing questions.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.signals import IMPACT_BINARY, SignalContext, SignalSpec, SignalUnavailable, domain

FED_MFA_DOC = "https://learn.microsoft.com/entra/identity/authentication/how-to-mfa-server-migration-fed"
FED_CERT_DOC = "https://learn.microsoft.com/entra/identity/hybrid/connect/how-to-connect-fed-o365-certs"
PHS_DOC = "https://learn.microsoft.com/entra/identity/hybrid/connect/whatis-phs"

# A certificate this close to expiry is an outage with a date on it: every federated user
# stops signing in the moment it lapses, and renewing it is a change on someone else's
# infrastructure, not something an Entra administrator can do alone.
_CERT_CRITICAL_DAYS = 14
_CERT_HIGH_DAYS = 30
_CERT_WARN_DAYS = 60


def _fabric(data: dict[str, Any]) -> dict[str, Any]:
    tenant = domain(data, "tenant")
    if not tenant:
        raise SignalUnavailable("The tenant profile was not collected.")
    fabric = tenant.get("identity_fabric") or {}
    if not fabric.get("readable"):
        raise SignalUnavailable(
            "The domain list could not be read, so the tenant's authentication perimeter is "
            "unknown" + (f": {fabric.get('blind_reason')}" if fabric.get("blind_reason") else ".")
        )
    return fabric


def _trusts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_fabric(data).get("federation") or [])


# ------------------------------------------------------------------------- evaluators
def _federated_mfa_trusted(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for trust in _trusts(data):
        behaviour = trust.get("mfa_behaviour") or {}
        if not behaviour.get("trusted"):
            continue
        vendor = (trust.get("vendor") or {}).get("label") or "the identity provider"
        implicit = "" if behaviour.get("explicit") else " (by default, because the setting is unset)"
        out.append(model.finding(
            signal_id="auth.federated_mfa_trusted", severity="high", pillar="auth",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title=f"Entra accepts multi-factor authentication performed by {vendor}",
            detail=f"Sign-ins for this domain are authenticated by {vendor}{implicit}. Entra takes "
                   "its multi-factor claim on trust, so anyone who can issue tokens there \u2014 or "
                   "who compromises it \u2014 satisfies Entra MFA without performing any.",
            evidence={
                "domain": trust.get("domain", ""),
                "behaviour": behaviour.get("value", ""),
                "explicitly_set": behaviour.get("explicit", False),
                "issuer_uri": trust.get("issuer_uri", ""),
                "vendor": vendor,
            },
            discriminator=trust.get("domain", ""),
        ))
    return out


def _effective_certificate(trust: dict[str, Any]) -> dict[str, Any]:
    """The certificate the trust is actually standing on.

    Entra keeps two — the current one and a successor — and accepts tokens signed by
    either; that overlap is the entire point of `nextSigningCertificate`. Judging the trust
    by the primary alone would have declared an outage on a tenant whose users are signing
    in perfectly well. The live tenant this was built against is exactly that case: primary
    expired 74 days ago, successor valid for another four months.
    """
    certs = [c for c in (trust.get("certificate"), trust.get("next_certificate"))
             if isinstance(c, dict) and c.get("parsed") and c.get("days_left") is not None]
    if not certs:
        return {}
    return max(certs, key=lambda c: c["days_left"])


def _federation_cert_expiry(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for trust in _trusts(data):
        effective = _effective_certificate(trust)
        if not effective:
            continue
        days = effective["days_left"]
        if days > _CERT_WARN_DAYS:
            continue
        severity = ("critical" if days <= _CERT_CRITICAL_DAYS
                    else "high" if days <= _CERT_HIGH_DAYS else "medium")
        vendor = (trust.get("vendor") or {}).get("label") or "the identity provider"
        when = "has expired" if effective.get("expired") else f"expires in {days} day(s)"
        primary = trust.get("certificate") or {}
        overlap = ("" if not primary.get("expired")
                   else " The previous certificate has already lapsed, so there is no overlap "
                        "left to fall back on.")
        out.append(model.finding(
            signal_id="mon.federation_cert_expiry", severity=severity, pillar="mon",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title=f"The federation signing certificate {when}",
            detail=f"Every user on this domain authenticates through {vendor}. When the last "
                   "valid signing certificate lapses the trust breaks and those sign-ins stop "
                   f"\u2014 and the renewal happens on the provider, not in Entra.{overlap}",
            evidence={
                "domain": trust.get("domain", ""),
                "not_after": effective.get("not_after", ""),
                "days_left": days,
                "thumbprint": effective.get("thumbprint", ""),
                "primary_expired": bool(primary.get("expired")),
                "successor_present": bool((trust.get("next_certificate") or {}).get("parsed")),
            },
            discriminator=trust.get("domain", ""),
        ))
    return out


def _federation_cert_stale_primary(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """The trust is running on its successor because the primary already expired.

    Not an outage — Entra accepts either certificate — but the overlap that protects the
    next renewal has been spent, and nobody tidied up afterwards.
    """
    out = []
    for trust in _trusts(data):
        primary = trust.get("certificate") or {}
        successor = trust.get("next_certificate") or {}
        if not primary.get("expired") or not successor.get("parsed") or successor.get("expired"):
            continue
        out.append(model.finding(
            signal_id="mon.federation_cert_stale_primary", severity="low", pillar="mon",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title="The federation trust is running on its successor certificate",
            detail="The primary signing certificate expired and the successor is carrying the "
                   "trust. Sign-ins are unaffected, but the rollover was never completed, so "
                   "the next renewal has no overlap to fall back on.",
            evidence={
                "domain": trust.get("domain", ""),
                "primary_not_after": primary.get("not_after", ""),
                "successor_not_after": successor.get("not_after", ""),
                "successor_days_left": successor.get("days_left"),
            },
            discriminator=trust.get("domain", ""),
        ))
    return out


def _federation_cert_rollover(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for trust in _trusts(data):
        rollover = trust.get("auto_rollover") or {}
        result = str(rollover.get("result") or "")
        if not result or rollover.get("healthy"):
            continue
        out.append(model.finding(
            signal_id="mon.federation_cert_rollover", severity="medium", pillar="mon",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title=f"Automatic signing-certificate rollover reports \u201c{result}\u201d",
            detail="Entra's automatic certificate update is not managing this trust, so the "
                   "signing certificate has to be renewed by hand before it expires. Nothing "
                   "will remind you on the day it matters.",
            evidence={
                "domain": trust.get("domain", ""),
                "result": result,
                "last_run": rollover.get("last_run", ""),
            },
            discriminator=trust.get("domain", ""),
        ))
    return out


def _no_password_hash_sync(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    fabric = _fabric(data)
    if not fabric.get("federation"):
        return []                       # nothing federated: PHS is a preference, not a gap
    hybrid = domain(data, "tenant").get("hybrid") or {}
    if not hybrid.get("features_readable"):
        raise SignalUnavailable(
            "The on-premises synchronisation configuration was not collected "
            "(needs OnPremDirectorySynchronization.Read.All)."
        )
    if not hybrid.get("sync_enabled") or hybrid.get("password_sync"):
        return []
    return [model.finding(
        signal_id="auth.federated_no_password_hash_sync", severity="medium", pillar="auth",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="Federated tenant with password hash synchronisation disabled",
        detail="Authentication depends entirely on the federation provider being reachable: "
               "if it is down, nobody signs in, and there is no cloud fallback. It also means "
               "Entra never sees a password hash, so leaked-credential detection cannot run "
               "for these users at all.",
        evidence={
            "sync_enabled": True,
            "password_sync": False,
            "federated_domains": [t.get("domain", "") for t in fabric.get("federation") or []],
        },
        discriminator="phs",
    )]


def _federated_domain(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Informational: record the perimeter itself, with its blast radius."""
    out = []
    for trust in _trusts(data):
        vendor = (trust.get("vendor") or {}).get("label") or "an external provider"
        users = trust.get("user_count")
        share = trust.get("user_share")
        who = ""
        if users is not None:
            who = f" \u2014 {users:,} user(s)" + (f", {round((share or 0) * 100)}% of the directory" if share else "")
        out.append(model.finding(
            signal_id="mon.federated_domain", severity="info", pillar="mon",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title=f"Domain is federated to {vendor}{who}",
            detail="Sign-ins for this domain are authenticated outside Entra ID. Authentication "
                   "policy, multi-factor and lockout behaviour for these users live with the "
                   "provider, and are not described by anything on this screen.",
            evidence={
                "domain": trust.get("domain", ""),
                "vendor": vendor,
                "issuer_uri": trust.get("issuer_uri", ""),
                "protocol": trust.get("protocol", ""),
                "host": trust.get("host", ""),
                "user_count": users,
            },
            discriminator=trust.get("domain", ""),
        ))
    return out


def _federation_protocol(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for trust in _trusts(data):
        if str(trust.get("protocol") or "").lower() != "wsfed":
            continue
        out.append(model.finding(
            signal_id="mon.federation_protocol_wsfed", severity="low", pillar="mon",
            object_kind="domain", object_id=trust.get("domain", ""), object_name=trust.get("domain", ""),
            title="Federation trust uses WS-Federation rather than SAML 2.0",
            detail="WS-Fed still works and is not a defect on its own. It is the older of the "
                   "two protocols Entra supports here, and worth noting when the trust is next "
                   "rebuilt.",
            evidence={"domain": trust.get("domain", ""), "protocol": trust.get("protocol", "")},
            discriminator=trust.get("domain", ""),
        ))
    return out


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="auth.federated_mfa_trusted", title="Entra trusts the federated provider's MFA claim",
        question="Can the identity provider satisfy Entra MFA without performing MFA?",
        why="Conditional Access can require multi-factor authentication and still be satisfied "
            "by a claim from a system Entra does not control. If that system is compromised or "
            "misconfigured, every MFA requirement in the tenant is satisfiable without a second "
            "factor.",
        pillar="auth", severity="high", weight=9, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="Set federatedIdpMfaBehavior to enforceMfaByFederatedIdp, or move the domain "
                    "to managed authentication so Entra performs MFA itself.",
        remediation_steps=(
            "Confirm with the identity-provider owner that it performs and asserts MFA.",
            "Set federatedIdpMfaBehavior to enforceMfaByFederatedIdp on the domain.",
            "Longer term, evaluate moving the domain to managed authentication with Entra MFA.",
        ),
        doc_link=FED_MFA_DOC, evaluate=_federated_mfa_trusted,
        tags=("federation",),
    ),
    SignalSpec(
        id="mon.federation_cert_expiry", title="Federation signing certificate is expiring",
        question="Is the certificate that holds the federation trust together about to lapse?",
        why="When it expires, every user on the federated domain stops being able to sign in. "
            "It is one of the few identity failures that takes an entire workforce offline at "
            "a predictable moment.",
        pillar="mon", severity="critical", weight=10, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="Renew the signing certificate on the identity provider and update the "
                    "federation trust before the expiry date.",
        remediation_steps=(
            "Renew the token-signing certificate on the identity provider.",
            "Update the Entra federation trust with the new certificate.",
            "Verify a test sign-in on the federated domain before the old certificate lapses.",
        ),
        doc_link=FED_CERT_DOC, evaluate=_federation_cert_expiry,
        tags=("federation",),
    ),
    SignalSpec(
        id="mon.federation_cert_stale_primary", title="Federation trust is running on its successor certificate",
        question="Did the last certificate rollover actually finish?",
        why="Entra accepts either the primary or the successor certificate, so an expired "
            "primary breaks nothing today. It does mean the overlap that makes the next "
            "renewal safe has already been spent.",
        pillar="mon", severity="low", weight=3, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="Promote the successor certificate on the federation trust and stage a new "
                    "successor, so the next renewal has overlap again.",
        doc_link=FED_CERT_DOC, evaluate=_federation_cert_stale_primary,
        tags=("federation",),
    ),
    SignalSpec(
        id="mon.federation_cert_rollover", title="Automatic certificate rollover is not managing the trust",
        question="Is anything watching the federation certificate other than a person?",
        why="Entra advertises automatic certificate rollover for federated domains. When it "
            "reports anything other than success, the renewal is manual and the only thing "
            "standing between the tenant and an outage is somebody's calendar.",
        pillar="mon", severity="medium", weight=5, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="Investigate the rollover result on the federation trust and re-establish "
                    "automatic certificate updates, or diarise the manual renewal.",
        doc_link=FED_CERT_DOC, evaluate=_federation_cert_rollover,
        tags=("federation",),
    ),
    SignalSpec(
        id="auth.federated_no_password_hash_sync", title="Federated tenant without password hash sync",
        question="Is there any authentication path if the federation provider is unreachable?",
        why="Password hash synchronisation is the fallback that keeps a federated tenant signing "
            "in during a provider outage, and it is what makes Entra's leaked-credential "
            "detection possible at all. Without it, both are simply unavailable.",
        pillar="auth", severity="medium", weight=6, object_kind="tenant",
        domains=("tenant",), requires=("OnPremDirectorySynchronization.Read.All",),
        impact=IMPACT_BINARY,
        remediation="Enable password hash synchronisation in Entra Connect. It can be enabled "
                    "alongside federation without changing which system authenticates users.",
        doc_link=PHS_DOC, evaluate=_no_password_hash_sync,
        tags=("federation",),
    ),
    SignalSpec(
        id="mon.federated_domain", title="Domain is federated to an external provider",
        question="Which domains does this tenant not authenticate itself?",
        why="Every authentication claim this product makes about a federated domain is really a "
            "claim about somebody else's system. Recording which domains those are, and how "
            "many users sit behind them, is what makes the rest of the numbers readable.",
        pillar="mon", severity="info", weight=1, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="No action. This records the authentication perimeter.",
        doc_link=FED_MFA_DOC, evaluate=_federated_domain, tags=("federation",),
    ),
    SignalSpec(
        id="mon.federation_protocol_wsfed", title="Federation trust uses WS-Federation",
        question="Which federation protocol does the trust use?",
        why="WS-Fed is the older of the two protocols Entra supports for federation. It is not "
            "a defect, but it is worth knowing when the trust is rebuilt or migrated.",
        pillar="mon", severity="low", weight=2, object_kind="domain",
        domains=("tenant",), requires=("Directory.Read.All",), impact=IMPACT_BINARY,
        remediation="Consider SAML 2.0 when the federation trust is next rebuilt.",
        doc_link=FED_MFA_DOC, evaluate=_federation_protocol, tags=("federation",),
    ),
]
