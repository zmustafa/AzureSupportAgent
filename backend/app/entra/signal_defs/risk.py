"""Risk signals pillar — Identity Protection joins and deterministic sign-in patterns.

This pillar deliberately does **not** re-implement Microsoft's detections. Microsoft is
better at deciding whether a sign-in was atypical travel than any rule we could write, and
a second opinion on the same question is noise.

What Microsoft does *not* do is join risk to the rest of the tenant. A "medium risk" user
is a different conversation when they hold Global Administrator, are excluded from every
Conditional Access policy, and cannot self-remediate because they have no MFA method
registered. Those joins are the whole value of this pillar, and they are what every signal
below computes.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
)

RISK_DOC = "https://learn.microsoft.com/entra/id-protection/overview-identity-protection"
SIGNIN_DOC = "https://learn.microsoft.com/entra/identity/monitoring-health/concept-sign-ins"
LEGACY_DOC = "https://learn.microsoft.com/entra/identity/conditional-access/block-legacy-authentication"

_UNREMEDIATED_STATES = ("atRisk", "confirmedCompromised")


def _risk(data: dict[str, Any]) -> dict[str, Any]:
    return domain(data, "risk")


def _caps(data: dict[str, Any]) -> dict[str, Any]:
    value = _risk(data).get("capabilities")
    return value if isinstance(value, dict) else {}


def _signins(data: dict[str, Any]) -> dict[str, Any]:
    value = _risk(data).get("signins")
    return value if isinstance(value, dict) else {}


def _privileged_ids(data: dict[str, Any]) -> set[str]:
    from app.entra.collectors.roles import privileged_principal_ids

    return privileged_principal_ids(domain(data, "roles"))


def _users_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(u.get("id") or ""): u
            for u in domain(data, "people").get("users") or [] if u.get("id")}


def _enabled_user_count(data: dict[str, Any]) -> int:
    return sum(1 for u in domain(data, "people").get("users") or [] if u.get("enabled"))


# ------------------------------------------------------------------ Identity Protection
def _privileged_user_at_risk(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """The single most important join in the pillar."""
    if not _caps(data).get("risky_users"):
        raise SignalUnavailable("Identity Protection risky users were not collected.")
    privileged = _privileged_ids(data)
    if not privileged:
        raise SignalUnavailable("Directory role assignments were not collected, so risk cannot "
                                "be joined to privilege.")
    users = _users_by_id(data)
    out = []
    for row in _risk(data).get("risky_users") or []:
        if row.get("state") not in _UNREMEDIATED_STATES:
            continue
        uid = str(row.get("id") or "")
        if uid not in privileged:
            continue
        user = users.get(uid) or {}
        can_self_remediate = user.get("mfa_registered")
        out.append(model.finding(
            signal_id="risk.privileged_user_at_risk", severity="critical", pillar="risk",
            object_kind="user", object_id=uid,
            object_name=row.get("upn") or row.get("name") or uid,
            title=f"Privileged user '{row.get('upn') or row.get('name')}' is flagged at risk",
            detail="Identity Protection has this account at risk and it holds a privileged "
                   "directory role. Risk level is secondary here — the combination is what "
                   "matters, because a compromised admin is a compromised tenant."
                   + ("" if can_self_remediate is not False else
                      " This account has no registered MFA method, so it cannot self-remediate."),
            evidence={"risk_level": row.get("level"), "risk_state": row.get("state"),
                      "risk_detail": row.get("detail"), "last_updated": row.get("last_updated"),
                      "mfa_registered": can_self_remediate},
            portal_link=model.portal_user(uid),
        ))
    return out


def _high_risk_unremediated(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("risky_users"):
        raise SignalUnavailable("Identity Protection risky users were not collected.")
    out = []
    for row in _risk(data).get("risky_users") or []:
        if row.get("level") != "high" or row.get("state") not in _UNREMEDIATED_STATES:
            continue
        days = ctx.days_since(str(row.get("last_updated") or ""))
        uid = str(row.get("id") or "")
        age = f" for {days} day(s)" if days is not None else ""
        out.append(model.finding(
            signal_id="risk.high_risk_user_unremediated", severity="high", pillar="risk",
            object_kind="user", object_id=uid,
            object_name=row.get("upn") or row.get("name") or uid,
            title=f"High-risk user '{row.get('upn') or row.get('name')}' unremediated{age}",
            detail="Identity Protection raised this account to high risk and nothing has "
                   "dismissed, confirmed or remediated it since. Unactioned risk is the same "
                   "as no risk detection at all.",
            evidence={"risk_state": row.get("state"), "risk_detail": row.get("detail"),
                      "last_updated": row.get("last_updated"), "days_open": days},
            portal_link=model.portal_user(uid),
        ))
    return out


def _risky_user_cannot_remediate(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("risky_users"):
        raise SignalUnavailable("Identity Protection risky users were not collected.")
    users = _users_by_id(data)
    if not any(u.get("mfa_registered") is not None for u in users.values()):
        raise SignalUnavailable("MFA registration data was not collected, so self-remediation "
                                "capability cannot be determined.")
    out = []
    for row in _risk(data).get("risky_users") or []:
        if row.get("state") not in _UNREMEDIATED_STATES:
            continue
        uid = str(row.get("id") or "")
        if (users.get(uid) or {}).get("mfa_registered") is not False:
            continue
        out.append(model.finding(
            signal_id="risk.risky_user_cannot_remediate", severity="high", pillar="risk",
            object_kind="user", object_id=uid,
            object_name=row.get("upn") or row.get("name") or uid,
            title=f"At-risk user '{row.get('upn') or row.get('name')}' cannot self-remediate",
            detail="A risk-based policy would ask this user to prove who they are, but they have "
                   "no registered authentication method — so the remediation loop cannot close "
                   "and the account stays at risk until an administrator intervenes.",
            evidence={"risk_level": row.get("level"), "risk_state": row.get("state"),
                      "mfa_registered": False},
            portal_link=model.portal_user(uid),
        ))
    return out


def _risky_workload_identity(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("risky_workload_identities"):
        raise SignalUnavailable("Risky workload identities require Workload Identities Premium.")
    sps = {str(s.get("object_id") or ""): s
           for s in domain(data, "apps").get("service_principals") or []}
    out = []
    for row in _risk(data).get("risky_service_principals") or []:
        if row.get("state") not in _UNREMEDIATED_STATES:
            continue
        sid = str(row.get("id") or "")
        sp = sps.get(sid) or {}
        perms = [p.get("permission") for p in sp.get("granted_app_permissions") or []][:20]
        out.append(model.finding(
            signal_id="risk.risky_workload_identity", severity="critical", pillar="risk",
            object_kind="sp", object_id=sid, object_name=row.get("name") or sid,
            title=f"Workload identity '{row.get('name')}' is flagged at risk",
            detail="A service principal at risk is worse than a user at risk: it has no human to "
                   "notice, no MFA to fall back on, and typically holds application permissions "
                   "that apply tenant-wide.",
            evidence={"risk_level": row.get("level"), "risk_state": row.get("state"),
                      "risk_detail": row.get("detail"), "granted_permissions": perms},
            portal_link=model.portal_sp(sid),
        ))
    return out


def _risk_from_trusted_location(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Detections coming from IP ranges the tenant treats as trusted."""
    if not _caps(data).get("risk_detections"):
        raise SignalUnavailable("Identity Protection risk detections were not collected.")
    trusted = {
        str(loc.get("display_name") or "")
        for loc in domain(data, "ca").get("named_locations") or []
        if loc.get("is_trusted")
    }
    if not trusted:
        return []
    countries: set[str] = set()
    for loc in domain(data, "ca").get("named_locations") or []:
        if loc.get("is_trusted"):
            countries.update(str(c) for c in loc.get("countries") or [])
    if not countries:
        return []
    hits: dict[str, int] = {}
    for det in _risk(data).get("risk_detections") or []:
        if det.get("country") and det["country"] in countries:
            hits[det["country"]] = hits.get(det["country"], 0) + 1
    return [model.finding(
        signal_id="risk.detection_from_trusted_location", severity="medium", pillar="risk",
        object_kind="tenant", object_id=country, object_name=country,
        title=f"{count} risk detection(s) originated inside a trusted named location ({country})",
        detail="Conditional Access treats this location as trusted, which typically relaxes "
               "controls. Identity Protection is raising risk from inside it, so the trust "
               "assumption deserves re-examination.",
        evidence={"country": country, "detections": count,
                  "trusted_locations": sorted(trusted)[:10]},
        portal_link=model.portal_ca_policy(""),
    ) for country, count in sorted(hits.items(), key=lambda kv: -kv[1])]


# ----------------------------------------------------------------- sign-in intelligence
def _legacy_auth_success(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("signins"):
        raise SignalUnavailable("Sign-in logs were not collected (needs AuditLog.Read.All and "
                                "Entra ID P1).")
    out = []
    for row in _signins(data).get("legacy") or []:
        if not row.get("success"):
            continue
        out.append(model.finding(
            signal_id="risk.legacy_auth_success", severity="critical", pillar="risk",
            object_kind="tenant", object_id=str(row.get("protocol") or ""),
            object_name=str(row.get("protocol") or ""),
            title=f"{row['success']} successful legacy sign-in(s) over {row.get('protocol')}",
            detail="Legacy authentication protocols cannot present a multi-factor challenge. A "
                   "*successful* legacy sign-in means MFA was bypassed, whatever the Conditional "
                   "Access policy set says.",
            evidence={"protocol": row.get("protocol"), "successful": row.get("success"),
                      "attempts": row.get("total"), "distinct_users": row.get("users"),
                      "applications": row.get("apps"), "last_success": row.get("last_success"),
                      "sampled": _signins(data).get("sampled", False)},
            portal_link=model.portal_ca_policy(""),
        ))
    return out


def _signin_pattern(kind: str, signal_id: str, severity: str):
    """Factory for the deterministic pattern family. Each finding states its own rule."""

    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        if not _caps(data).get("signins"):
            raise SignalUnavailable("Sign-in logs were not collected (needs AuditLog.Read.All "
                                    "and Entra ID P1).")
        out = []
        for pattern in _risk(data).get("patterns") or []:
            if pattern.get("kind") != kind:
                continue
            evidence = pattern.get("evidence") or {}
            # The key is an object id for the per-principal patterns. The collector resolves
            # a name for it where Graph has one, and showing the id in the name column next
            # to a title that reads "Repeated MFA denials for Alice" is just confusing.
            name = (str(evidence.get("display_name") or "") or str(evidence.get("upn") or "")
                    or str(pattern.get("key") or ""))
            out.append(model.finding(
                signal_id=signal_id, severity=severity, pillar="risk",
                object_kind="tenant", object_id=str(pattern.get("key") or ""),
                object_name=name,
                title=str(pattern.get("label") or ""),
                detail=str(pattern.get("rule") or ""),
                evidence={**evidence,
                          "rule": pattern.get("rule"),
                          "sampled": _signins(data).get("sampled", False)},
            ))
        return out

    return _inner


def _priv_signin_unmanaged_device(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """The privileged join the collector deliberately left undone."""
    if not _caps(data).get("signins"):
        raise SignalUnavailable("Sign-in logs were not collected (needs AuditLog.Read.All and "
                                "Entra ID P1).")
    privileged = _privileged_ids(data)
    if not privileged:
        raise SignalUnavailable("Directory role assignments were not collected, so device "
                                "compliance cannot be joined to privilege.")
    out = []
    for row in _signins(data).get("unmanaged_signin_users") or []:
        uid = str(row.get("user_id") or "")
        if uid not in privileged:
            continue
        out.append(model.finding(
            signal_id="risk.priv_signin_unmanaged_device", severity="high", pillar="risk",
            object_kind="user", object_id=uid, object_name=row.get("upn") or uid,
            title=f"Privileged user '{row.get('upn') or uid}' signed in from a non-compliant device",
            detail="Administrative work from an unmanaged endpoint puts tenant-wide credentials on "
                   "a device the organization cannot attest to. A device-compliance requirement "
                   "on the admin cohort closes this.",
            evidence={"upn": row.get("upn"), "sign_ins": row.get("count"),
                      "device": row.get("device"), "last_seen": row.get("last_seen"),
                      "sampled": _signins(data).get("sampled", False)},
            portal_link=model.portal_user(uid),
        ))
    return out


def _signin_failure_rate(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("signins"):
        raise SignalUnavailable("Sign-in logs were not collected (needs AuditLog.Read.All and "
                                "Entra ID P1).")
    signins = _signins(data)
    total = int(signins.get("total") or 0)
    if total < 500:
        return []           # too little traffic for a rate to mean anything
    rate = float(signins.get("failure_rate") or 0.0)
    if rate < 0.35:
        return []
    top = (signins.get("by_failure_code") or [])[:5]
    return [model.finding(
        signal_id="risk.signin_failure_rate_high", severity="medium", pillar="risk",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"{rate:.0%} of sign-ins in the window failed",
        detail="A sustained failure rate this high is usually a misconfigured application, a "
               "broken service account, or an attack that nobody has looked at. The top failure "
               "codes name which.",
        evidence={"failure_rate": rate, "total": total, "failures": signins.get("failure"),
                  "top_codes": [{"code": c.get("code"), "meaning": c.get("meaning"),
                                 "count": c.get("count")} for c in top],
                  "sampled": signins.get("sampled", False)},
    )]


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="risk.privileged_user_at_risk", title="A privileged user is flagged at risk",
        question="Is any account holding a directory role currently at risk?",
        why="A compromised administrator is a compromised tenant. This join — risk level "
            "against privilege — is the one Microsoft's own risk report does not make for you.",
        pillar="risk", severity="critical", weight=10, object_kind="user",
        domains=("risk", "roles"), requires=("IdentityRiskyUser.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Investigate the account immediately, force credential reset and revoke "
                    "sessions, then confirm or dismiss the risk in Identity Protection.",
        remediation_steps=(
            "Open the user in Identity Protection and review the detections behind the risk.",
            "Revoke sign-in sessions and require a password change.",
            "Confirm compromise or dismiss the risk so the state stops being ambiguous.",
            "Consider moving the role assignment to PIM so the privilege is not standing.",
        ),
        doc_link=RISK_DOC, evaluate=_privileged_user_at_risk,
        tags=("identity-protection", "privileged"),
    ),
    SignalSpec(
        id="risk.high_risk_user_unremediated", title="High-risk users left unremediated",
        question="Has anybody acted on the high-risk users Identity Protection flagged?",
        why="Risk detection without remediation is theatre. An unactioned high-risk user is "
            "indistinguishable from having no detection at all.",
        pillar="risk", severity="high", weight=10, object_kind="user",
        domains=("risk",), requires=("IdentityRiskyUser.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Work the high-risk queue: confirm compromise, dismiss false positives, or "
                    "let a risk-based Conditional Access policy remediate automatically.",
        remediation_steps=(
            "Review each high-risk user and decide: confirm, dismiss or remediate.",
            "Enable a user-risk Conditional Access policy so remediation is automatic.",
        ),
        doc_link=RISK_DOC, evaluate=_high_risk_unremediated, tags=("identity-protection",),
    ),
    SignalSpec(
        id="risk.risky_user_cannot_remediate",
        title="At-risk users have no way to self-remediate",
        question="Can the users a risk policy would challenge actually pass the challenge?",
        why="A user-risk policy asks for MFA. A user with no registered method cannot answer it, "
            "so the policy locks them out instead of remediating them.",
        pillar="risk", severity="high", weight=8, object_kind="user",
        domains=("risk", "people"), requires=("IdentityRiskyUser.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Register an authentication method for these users before enabling automatic "
                    "risk remediation.",
        remediation_steps=(
            "Export the list and drive MFA registration for it.",
            "Use a registration-campaign or a temporary access pass for accounts that cannot enrol.",
        ),
        doc_link=RISK_DOC, evaluate=_risky_user_cannot_remediate, tags=("identity-protection",),
    ),
    SignalSpec(
        id="risk.risky_workload_identity", title="A workload identity is flagged at risk",
        question="Is any service principal currently flagged as risky?",
        why="A risky service principal has no human to notice, no MFA to fall back on, and "
            "usually holds tenant-wide application permissions.",
        pillar="risk", severity="critical", weight=9, object_kind="sp",
        domains=("risk", "apps"), requires=("IdentityRiskyServicePrincipal.Read.All",),
        licence="workload_id_premium", impact=IMPACT_SATURATING, saturation=2,
        remediation="Rotate the credential, review what the principal can reach, and confirm or "
                    "dismiss the risk.",
        remediation_steps=(
            "Rotate every secret and certificate on the application.",
            "Review its granted application permissions and Azure role assignments.",
            "Confirm compromise or dismiss the detection.",
        ),
        doc_link=RISK_DOC, evaluate=_risky_workload_identity, tags=("identity-protection", "app"),
    ),
    SignalSpec(
        id="risk.detection_from_trusted_location",
        title="Risk detections originate inside a trusted location",
        question="Is risk being raised from IP ranges Conditional Access treats as safe?",
        why="Trusted locations relax controls. Risk raised from inside one means the trust "
            "boundary is not where the policy assumes it is.",
        pillar="risk", severity="medium", weight=5, object_kind="tenant",
        domains=("risk", "ca"), requires=("IdentityRiskEvent.Read.All",), licence="p2",
        impact=IMPACT_BINARY,
        remediation="Re-examine the trusted named location and consider removing the trust flag.",
        doc_link=RISK_DOC, evaluate=_risk_from_trusted_location, tags=("identity-protection",),
    ),
    SignalSpec(
        id="risk.legacy_auth_success", title="Legacy authentication is succeeding",
        question="Are sign-ins over protocols that cannot do MFA still succeeding?",
        why="A successful legacy sign-in means multi-factor authentication was bypassed. This is "
            "the highest-yield finding in most tenants and the easiest to fix.",
        pillar="risk", severity="critical", weight=10, object_kind="tenant",
        domains=("risk",), requires=("AuditLog.Read.All",), licence="p1", impact=IMPACT_BINARY,
        remediation="Block legacy authentication with a Conditional Access policy, after using "
                    "the per-protocol breakdown to migrate the applications still using it.",
        remediation_steps=(
            "Identify the applications and users in the breakdown below.",
            "Move them to modern authentication.",
            "Deploy a Conditional Access policy blocking legacy clients, in report-only first.",
        ),
        doc_link=LEGACY_DOC, evaluate=_legacy_auth_success, tags=("legacy-auth",),
    ),
    SignalSpec(
        id="risk.password_spray_pattern", title="Password spray pattern detected",
        question="Is one source failing credential checks across many accounts?",
        why="Spray is quiet by design — a handful of attempts per account stays under lockout "
            "thresholds. It only becomes visible when you count distinct users per source.",
        pillar="risk", severity="high", weight=10, object_kind="tenant",
        domains=("risk",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Block the source, confirm no account succeeded from it, and enforce "
                    "smart lockout plus a strong-authentication policy.",
        doc_link=SIGNIN_DOC, evaluate=_signin_pattern("password_spray",
                                                      "risk.password_spray_pattern", "high"),
        tags=("pattern",),
    ),
    SignalSpec(
        id="risk.mfa_fatigue_pattern", title="MFA fatigue pattern detected",
        question="Is anyone being bombarded with multi-factor prompts they keep denying?",
        why="Repeated denials mean somebody holds the password and is pushing prompts until the "
            "user gives in. The denial is the user doing the right thing — under pressure.",
        pillar="risk", severity="high", weight=10, object_kind="user",
        domains=("risk",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Reset the user's credentials, revoke sessions, and enable number matching "
                    "so a push cannot be approved by reflex.",
        remediation_steps=(
            "Reset the password and revoke all sessions for the affected user.",
            "Enable number matching and additional context in the Authenticator settings.",
        ),
        doc_link=SIGNIN_DOC, evaluate=_signin_pattern("mfa_fatigue", "risk.mfa_fatigue_pattern",
                                                      "high"),
        tags=("pattern",),
    ),
    SignalSpec(
        id="risk.signin_failure_spike", title="Sign-in failure spike",
        question="Did failures jump well above the normal daily rate?",
        why="A spike is either an outage nobody reported or an attack nobody noticed. Both are "
            "worth a look on the day it happens rather than at the next review.",
        pillar="risk", severity="medium", weight=6, object_kind="tenant",
        domains=("risk",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Compare the spike day's top failure codes with the baseline to separate a "
                    "broken application from an attack.",
        doc_link=SIGNIN_DOC,
        evaluate=_signin_pattern("failure_spike", "risk.signin_failure_spike", "medium"),
        tags=("pattern",),
    ),
    SignalSpec(
        id="risk.priv_signin_unmanaged_device",
        title="Privileged sign-in from a non-compliant device",
        question="Are administrators working from endpoints the organization cannot attest to?",
        why="Administrative credentials on an unmanaged device are one keylogger away from a "
            "tenant compromise, and no directory control can see that device.",
        pillar="risk", severity="high", weight=9, object_kind="user",
        domains=("risk", "roles"), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Require a compliant or hybrid-joined device for the administrative cohort in "
                    "Conditional Access.",
        doc_link=SIGNIN_DOC, evaluate=_priv_signin_unmanaged_device, tags=("privileged",),
    ),
    SignalSpec(
        id="risk.signin_failure_rate_high", title="Sustained high sign-in failure rate",
        question="What proportion of sign-ins in the window failed?",
        why="A third or more of all sign-ins failing is not normal noise. It is a broken "
            "integration, a stuck service account, or sustained credential attack.",
        pillar="risk", severity="medium", weight=5, object_kind="tenant",
        domains=("risk",), requires=("AuditLog.Read.All",), licence="p1", impact=IMPACT_BINARY,
        remediation="Work the top failure codes; each names a distinct root cause.",
        doc_link=SIGNIN_DOC, evaluate=_signin_failure_rate,
    ),
]
