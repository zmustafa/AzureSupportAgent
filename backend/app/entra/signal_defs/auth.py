"""Authentication pillar — MFA coverage, method strength and the tenant methods policy.

MFA truth comes from ``/reports/authenticationMethods/userRegistrationDetails`` (collected
by the people domain), not from a capped per-user scan. When that report was unavailable
these signals raise :class:`SignalUnavailable` rather than returning zero findings — a
tenant we could not measure must not score as if it were clean.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors.people import weak_only
from app.entra.collectors.roles import privileged_principal_ids
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    enabled_members,
    pop_enabled_members,
    user_index,
)

_MFA_REPORT_MISSING = (
    "The authentication-method registration report was not collected "
    "(needs AuditLog.Read.All / UserAuthenticationMethod.Read.All and Entra ID P1)."
)


def _require_mfa_report(data: dict[str, Any]) -> None:
    caps = domain(data, "people").get("capabilities") or {}
    if not caps.get("mfa_registration_report"):
        raise SignalUnavailable(_MFA_REPORT_MISSING)


# ------------------------------------------------------------------------- evaluators
def _privileged_no_mfa(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_mfa_report(data)
    users = user_index(data)
    out = []
    for pid in sorted(privileged_principal_ids(domain(data, "roles"))):
        u = users.get(pid)
        if not u or not u.get("enabled") or u.get("mfa_registered") is not False:
            continue
        out.append(model.finding(
            signal_id="auth.privileged_no_mfa", severity="critical", pillar="auth",
            object_kind="user", object_id=pid, object_name=u.get("upn") or u.get("display_name") or pid,
            title=f"Privileged user {u.get('upn') or u.get('display_name')} has no MFA method registered",
            detail="A privileged account without a registered MFA method cannot satisfy any MFA "
                   "control, and is a single password away from tenant compromise.",
            evidence={"methods": u.get("methods") or [], "mfa_capable": u.get("mfa_capable"),
                      "enabled": u.get("enabled"), "user_type": u.get("user_type")},
            portal_link=model.portal_user(pid),
        ))
    return out


def _no_mfa_registered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_mfa_report(data)
    privileged = privileged_principal_ids(domain(data, "roles"))
    out = []
    for u in enabled_members(data):
        uid = str(u.get("id"))
        if uid in privileged or u.get("mfa_registered") is not False:
            continue
        out.append(model.finding(
            signal_id="auth.no_mfa_registered", severity="high", pillar="auth",
            object_kind="user", object_id=uid, object_name=u.get("upn") or uid,
            title=f"{u.get('upn') or u.get('display_name')} has no MFA method registered",
            detail="This account cannot satisfy an MFA requirement, so enabling one would block it.",
            evidence={"methods": u.get("methods") or [], "department": u.get("department", ""),
                      "last_signin": u.get("last_signin", "")},
            portal_link=model.portal_user(uid),
        ))
    return out


def _weak_method_only(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_mfa_report(data)
    out = []
    for u in enabled_members(data):
        methods = u.get("methods") or []
        if not methods or not weak_only(methods):
            continue
        uid = str(u.get("id"))
        out.append(model.finding(
            signal_id="auth.weak_method_only", severity="high", pillar="auth",
            object_kind="user", object_id=uid, object_name=u.get("upn") or uid,
            title=f"{u.get('upn') or u.get('display_name')} can only use phishable MFA methods",
            detail="SMS, voice and email one-time codes are interceptable. They satisfy an MFA "
                   "control while offering little real protection against a determined attacker.",
            evidence={"methods": methods},
            portal_link=model.portal_user(uid),
        ))
    return out


def _admin_not_phishing_resistant(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_mfa_report(data)
    users = user_index(data)
    out = []
    for pid in sorted(privileged_principal_ids(domain(data, "roles"))):
        u = users.get(pid)
        if not u or not u.get("enabled"):
            continue
        if u.get("phishing_resistant") is not False:
            continue
        out.append(model.finding(
            signal_id="auth.admin_not_phishing_resistant", severity="high", pillar="auth",
            object_kind="user", object_id=pid, object_name=u.get("upn") or pid,
            title=f"Administrator {u.get('upn') or u.get('display_name')} has no phishing-resistant method",
            detail="Privileged accounts should hold FIDO2, Windows Hello for Business or "
                   "certificate-based authentication so that an MFA prompt cannot simply be relayed.",
            evidence={"methods": u.get("methods") or [], "passwordless_capable": u.get("passwordless_capable")},
            portal_link=model.portal_user(pid),
        ))
    return out


def _methods_policy_weak(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policy = domain(data, "tenant").get("authentication_methods_policy") or {}
    if not policy.get("present"):
        raise SignalUnavailable("The authentication methods policy was not collected (needs Policy.Read.All).")
    weak = [name for name, on in (("SMS", policy.get("sms_enabled")), ("Voice", policy.get("voice_enabled")),
                                  ("Email OTP", policy.get("email_otp_enabled"))) if on]
    if not weak:
        return []
    return [model.finding(
        signal_id="auth.methods_policy_weak", severity="medium", pillar="auth",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"Phishable authentication methods are enabled tenant-wide: {', '.join(weak)}",
        detail="Any user may register an interceptable second factor, which weakens every MFA "
               "control that does not specify an authentication strength.",
        evidence={"enabled_weak_methods": weak, "methods": policy.get("methods") or {}},
        discriminator=",".join(sorted(weak)),
    )]


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="auth.privileged_no_mfa",
        title="Privileged users without a registered MFA method",
        question="Which administrators could sign in with a password alone?",
        why="A privileged account without MFA is the shortest path from a phished password to "
            "tenant compromise, and it will also be hard-blocked the moment an MFA policy is enabled.",
        pillar="auth", severity="critical", weight=10, object_kind="user",
        domains=("people", "roles"), requires=("AuditLog.Read.All", "RoleManagement.Read.Directory"),
        licence="p1", benchmarks=("CIS 1.1.1", "MCSB IM-4"),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Register a phishing-resistant method for every privileged account, then require it "
                    "with a Conditional Access authentication strength.",
        remediation_steps=(
            "Entra admin center > Protection > Authentication methods > Registration campaign.",
            "Ask each administrator to register FIDO2 or Windows Hello for Business.",
            "Create a Conditional Access policy targeting directory roles with a phishing-resistant strength.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/authentication/concept-authentication-methods",
        evaluate=_privileged_no_mfa, tags=("zero-trust", "quick-win"),
    ),
    SignalSpec(
        id="auth.no_mfa_registered",
        title="Users with no MFA method registered",
        question="How much of the directory cannot satisfy an MFA requirement today?",
        why="These accounts are both unprotected and the population that a new MFA policy would "
            "block outright rather than merely challenge.",
        pillar="auth", severity="high", weight=8, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        benchmarks=("CIS 1.1.1",),
        impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Run an authentication-method registration campaign, then enforce MFA in Conditional Access.",
        remediation_steps=(
            "Entra admin center > Protection > Authentication methods > Registration campaign.",
            "Enable the campaign for the affected group and give it a grace period.",
            "Simulate the MFA policy before enforcing it to confirm nobody is hard-blocked.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/authentication/how-to-mfa-registration-campaign",
        evaluate=_no_mfa_registered,
    ),
    SignalSpec(
        id="auth.weak_method_only",
        title="Users whose only MFA methods are phishable",
        question="Who can only prove identity with SMS, voice or email codes?",
        why="Interceptable factors satisfy an MFA control while offering little real protection, "
            "which makes coverage statistics look better than the tenant actually is.",
        pillar="auth", severity="high", weight=6, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Move users to the Authenticator app or a passkey, then disable SMS and voice tenant-wide.",
        remediation_steps=(
            "Entra admin center > Protection > Authentication methods > Policies.",
            "Enable Microsoft Authenticator / FIDO2 for all users.",
            "Scope SMS and Voice down to an exception group, then disable them.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/authentication/concept-authentication-methods",
        evaluate=_weak_method_only,
    ),
    SignalSpec(
        id="auth.admin_not_phishing_resistant",
        title="Administrators without a phishing-resistant method",
        question="Could an attacker relay an MFA prompt to reach an administrator account?",
        why="Push and code-based MFA can be relayed or fatigued. Privileged accounts should require "
            "a bound credential that cannot be replayed.",
        pillar="auth", severity="high", weight=7, object_kind="user",
        domains=("people", "roles"), requires=("AuditLog.Read.All",), licence="p1",
        benchmarks=("MCSB IM-6",),
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Issue FIDO2 keys or enable Windows Hello for Business for every privileged account.",
        remediation_steps=(
            "Entra admin center > Protection > Authentication methods > FIDO2 security key.",
            "Enable for the privileged group and register keys.",
            "Create an authentication strength policy requiring phishing-resistant MFA for admin roles.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/authentication/concept-authentication-strengths",
        evaluate=_admin_not_phishing_resistant, tags=("zero-trust",),
    ),
    SignalSpec(
        id="auth.methods_policy_weak",
        title="Phishable authentication methods enabled tenant-wide",
        question="Can any user still register SMS, voice or email as a second factor?",
        why="While these methods remain enabled, MFA coverage numbers overstate real protection.",
        pillar="auth", severity="medium", weight=5, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",),
        impact=IMPACT_BINARY,
        remediation="Disable SMS, voice and email one-time passcode in the authentication methods policy.",
        remediation_steps=(
            "Entra admin center > Protection > Authentication methods > Policies.",
            "Set SMS, Voice call and Email OTP to Disabled (or scope to a small exception group).",
            "Confirm every affected user has registered a stronger method first.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/authentication/concept-authentication-methods-manage",
        evaluate=_methods_policy_weak,
    ),
]
