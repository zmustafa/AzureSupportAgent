"""Applications and consent pillar — credential hygiene, granted Graph permissions,
tenant consent posture, ownership and federated credentials.

The distinction that matters throughout: ``requested_permissions`` is what an application
*asks for*; ``granted_app_permissions`` is what it has actually *been granted*. Only the
latter is risk. Conflating the two produces a wall of false positives that trains people to
ignore the whole screen.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors.apps import TIER_CRITICAL, TIER_HIGH
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    pop_applications,
    pop_service_principals,
)

APP_DOC = "https://learn.microsoft.com/entra/identity-platform/security-best-practices-for-app-registration"
CONSENT_DOC = "https://learn.microsoft.com/entra/identity/enterprise-apps/configure-user-consent"


def _apps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return domain(data, "apps").get("applications") or []


def _sps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return domain(data, "apps").get("service_principals") or []


def _caps(data: dict[str, Any]) -> dict[str, Any]:
    return domain(data, "apps").get("capabilities") or {}


def _credential_owner(app: dict[str, Any]) -> tuple[str, str]:
    return str(app.get("object_id") or ""), str(app.get("display_name") or app.get("app_id") or "")


# ---------------------------------------------------------------- credential hygiene
def _credentials(kind: str, expired: bool, signal_id: str, severity: str):
    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        out = []
        for app in _apps(data):
            oid, name = _credential_owner(app)
            for cred in app.get("credentials") or []:
                if cred.get("kind") != kind:
                    continue
                days = cred.get("days_left")
                if days is None:
                    continue
                if expired and days >= 0:
                    continue
                if not expired and not (0 <= days <= ctx.expiry_window_days):
                    continue
                label = cred.get("display_name") or cred.get("id") or kind
                out.append(model.finding(
                    signal_id=signal_id, severity=severity, pillar="app",
                    object_kind="app", object_id=oid, object_name=name,
                    title=(f"{name}: {kind} '{label}' expired {abs(days)} day(s) ago"
                           if expired else f"{name}: {kind} '{label}' expires in {days} day(s)"),
                    detail=("An expired credential left on the object is dead weight that hides "
                            "which credential is really in use."
                            if expired else
                            "A credential expiring without a rotation plan is an outage waiting to happen."),
                    evidence={"credential_id": cred.get("id"), "credential_name": label,
                              "end": cred.get("end"), "days_left": days,
                              "lifetime_days": cred.get("lifetime_days"), "app_id": app.get("app_id")},
                    discriminator=str(cred.get("id") or label),
                    portal_link=model.portal_app(str(app.get("app_id") or "")),
                ))
        return out
    return _inner


def _secret_long_lived(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for app in _apps(data):
        oid, name = _credential_owner(app)
        for cred in app.get("credentials") or []:
            lifetime = cred.get("lifetime_days")
            if lifetime is None or lifetime <= ctx.max_credential_lifetime_days:
                continue
            out.append(model.finding(
                signal_id="app.secret_long_lived", severity="medium", pillar="app",
                object_kind="app", object_id=oid, object_name=name,
                title=f"{name}: credential '{cred.get('display_name') or cred.get('id')}' has a "
                      f"{lifetime}-day lifetime",
                detail="A multi-year credential is a multi-year window for a leaked secret to keep working.",
                evidence={"lifetime_days": lifetime, "policy_max_days": ctx.max_credential_lifetime_days,
                          "end": cred.get("end")},
                discriminator=str(cred.get("id") or ""),
                portal_link=model.portal_app(str(app.get("app_id") or "")),
            ))
    return out


def _secret_never_rotated(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for app in _apps(data):
        oid, name = _credential_owner(app)
        for cred in app.get("credentials") or []:
            age = ctx.days_since(str(cred.get("start") or ""))
            if age is None or age <= ctx.credential_rotation_days:
                continue
            if cred.get("expired"):
                continue
            out.append(model.finding(
                signal_id="app.secret_never_rotated", severity="medium", pillar="app",
                object_kind="app", object_id=oid, object_name=name,
                title=f"{name}: credential '{cred.get('display_name') or cred.get('id')}' is {age} days old",
                detail="A credential that has never been rotated has had a long time to leak into a "
                       "pipeline log, a laptop or a wiki.",
                evidence={"age_days": age, "start": cred.get("start"), "end": cred.get("end")},
                discriminator=str(cred.get("id") or ""),
                portal_link=model.portal_app(str(app.get("app_id") or "")),
            ))
    return out


def _too_many_credentials(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for app in _apps(data):
        oid, name = _credential_owner(app)
        active = [c for c in app.get("credentials") or [] if not c.get("expired")]
        if len(active) <= ctx.max_credentials_per_app:
            continue
        out.append(model.finding(
            signal_id="app.too_many_credentials", severity="low", pillar="app",
            object_kind="app", object_id=oid, object_name=name,
            title=f"{name} has {len(active)} active credentials",
            detail="More credentials than a rotation needs usually means nobody knows which are in use.",
            evidence={"active_credentials": len(active), "max": ctx.max_credentials_per_app,
                      "names": [c.get("display_name") for c in active][:10]},
            discriminator=str(len(active)),
            portal_link=model.portal_app(str(app.get("app_id") or "")),
        ))
    return out


# ------------------------------------------------------------------------- ownership
# App registrations that Microsoft creates and manages inside a customer tenant. Telling an
# administrator to "assign an owner" to one of these is bad advice — Microsoft explicitly says
# not to modify them — and a wall of findings nobody can action is how a signal gets ignored.
_MANAGED_APP_MARKERS = ("do not modify", "aad-extensions-app")


def _is_microsoft_managed(app: dict[str, Any]) -> bool:
    name = str(app.get("display_name") or "").lower()
    return any(marker in name for marker in _MANAGED_APP_MARKERS)


def _ownerless(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("owners"):
        raise SignalUnavailable("Application owners were not collected.")
    out = []
    for app in _apps(data):
        if not app.get("owners_known") or app.get("owner_ids"):
            continue
        if _is_microsoft_managed(app):
            continue
        oid, name = _credential_owner(app)
        out.append(model.finding(
            signal_id="app.ownerless", severity="high", pillar="app",
            object_kind="app", object_id=oid, object_name=name,
            title=f"Application '{name}' has no owner",
            detail="Nobody is accountable for rotating its credentials, reviewing its permissions "
                   "or retiring it. Ownerless applications are how expired secrets become outages.",
            evidence={"app_id": app.get("app_id"), "created_at": app.get("created_at"),
                      "credential_count": len(app.get("credentials") or []),
                      "sign_in_audience": app.get("sign_in_audience")},
            portal_link=model.portal_app(str(app.get("app_id") or "")),
        ))
    return out


def _orphaned_sp(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for sp in _sps(data):
        if not sp.get("orphaned"):
            continue
        out.append(model.finding(
            signal_id="app.orphaned_sp", severity="medium", pillar="app",
            object_kind="sp", object_id=str(sp.get("object_id")), object_name=sp.get("display_name") or "",
            title=f"Service principal '{sp.get('display_name')}' has no application object",
            detail="Its application registration was deleted but the enterprise application and its "
                   "grants survive — access that no longer has a definition behind it.",
            evidence={"app_id": sp.get("app_id"), "granted_permissions":
                      [p.get("permission") for p in sp.get("granted_app_permissions") or []][:20]},
            portal_link=model.portal_sp(str(sp.get("object_id"))),
        ))
    return out


# ---------------------------------------------------------------------- permissions
def _granted_matching(predicate, signal_id: str, severity: str, title_fn, detail: str):
    """Factory for the 'service principal holds permissions matching X' signal family."""

    def _inner(d: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        if not _caps(d).get("granted_permissions"):
            raise SignalUnavailable("Granted application permissions were not collected.")
        out = []
        for sp in _sps(d):
            if sp.get("is_first_party"):
                continue
            hits = [p for p in sp.get("granted_app_permissions") or [] if predicate(p)]
            if not hits:
                continue
            names = sorted({p["permission"] for p in hits})
            out.append(model.finding(
                signal_id=signal_id, severity=severity, pillar="app",
                object_kind="sp", object_id=str(sp.get("object_id")),
                object_name=sp.get("display_name") or str(sp.get("app_id") or ""),
                title=title_fn(sp, names),
                detail=detail,
                evidence={"permissions": names, "app_id": sp.get("app_id"),
                          "sp_type": sp.get("sp_type"), "enabled": sp.get("enabled"),
                          "owners_known": sp.get("owners_known"), "owner_ids": sp.get("owner_ids"),
                          "credential_count": len(sp.get("credentials") or []),
                          "external": sp.get("is_external")},
                discriminator=",".join(names),
                portal_link=model.portal_sp(str(sp.get("object_id"))),
            ))
        return out
    return _inner


_consent_grant = _granted_matching(
    lambda p: p.get("flags", {}).get("consent_grant"), "app.consent_grant_capable", "critical",
    lambda sp, names: f"'{sp.get('display_name')}' can grant itself any permission ({', '.join(names)})",
    "This application holds a tenant-takeover primitive: it can assign application permissions or "
    "directory roles, including to itself. A compromise of its credential is a compromise of the tenant.",
)
_directory_write = _granted_matching(
    lambda p: p.get("flags", {}).get("directory_write"), "app.directory_write_permission", "critical",
    lambda sp, names: f"'{sp.get('display_name')}' holds directory write permissions ({', '.join(names)})",
    "Write access to the whole directory allows creating accounts, adding credentials to other "
    "applications and modifying group membership.",
)
_mail = _granted_matching(
    lambda p: p.get("flags", {}).get("mail"), "app.tenant_wide_mail", "critical",
    lambda sp, names: f"'{sp.get('display_name')}' can read every mailbox in the tenant ({', '.join(names)})",
    "An application permission on mail is not scoped to one mailbox — it reaches everyone's mail, "
    "including executives and the security team.",
)
_files = _granted_matching(
    lambda p: p.get("flags", {}).get("files"), "app.tenant_wide_files", "critical",
    lambda sp, names: f"'{sp.get('display_name')}' can read all SharePoint and OneDrive content ({', '.join(names)})",
    "Tenant-wide file access reaches every document library and personal drive in the organization.",
)
_chat = _granted_matching(
    lambda p: p.get("flags", {}).get("chat"), "app.tenant_wide_chat", "critical",
    lambda sp, names: f"'{sp.get('display_name')}' can read Teams chat and channel messages ({', '.join(names)})",
    "Tenant-wide messaging access reaches private chats as well as channels.",
)


def _high_privilege(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("granted_permissions"):
        raise SignalUnavailable("Granted application permissions were not collected.")
    out = []
    for sp in _sps(data):
        if sp.get("is_first_party"):
            continue
        hits = [
            p for p in sp.get("granted_app_permissions") or []
            if p.get("tier") in (TIER_CRITICAL, TIER_HIGH)
            and not any(p.get("flags", {}).get(k) for k in ("mail", "files", "chat", "consent_grant", "directory_write"))
        ]
        if not hits:
            continue
        names = sorted({p["permission"] for p in hits})
        out.append(model.finding(
            signal_id="app.high_privilege_permission", severity="high", pillar="app",
            object_kind="sp", object_id=str(sp.get("object_id")),
            object_name=sp.get("display_name") or "",
            title=f"'{sp.get('display_name')}' holds high-privilege Graph permissions ({', '.join(names[:4])}"
                  f"{'…' if len(names) > 4 else ''})",
            detail="These permissions apply tenant-wide without per-object scoping.",
            evidence={"permissions": names, "app_id": sp.get("app_id"),
                      "owner_ids": sp.get("owner_ids"), "external": sp.get("is_external")},
            discriminator=",".join(names),
            portal_link=model.portal_sp(str(sp.get("object_id"))),
        ))
    return out


def _admin_consent_all_principals(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("delegated_grants"):
        raise SignalUnavailable("Delegated consent grants were not collected (needs Directory.Read.All).")
    out = []
    for sp in _sps(data):
        for grant in sp.get("granted_delegated") or []:
            if grant.get("consent_type") != "AllPrincipals":
                continue
            if grant.get("max_tier") not in (TIER_CRITICAL, TIER_HIGH):
                continue
            out.append(model.finding(
                signal_id="app.admin_consent_all_principals", severity="high", pillar="app",
                object_kind="sp", object_id=str(sp.get("object_id")),
                object_name=sp.get("display_name") or "",
                title=f"'{sp.get('display_name')}' has tenant-wide delegated consent to "
                      f"{', '.join(sorted(grant.get('scopes') or [])[:4])}",
                detail="A delegated grant consented for all principals behaves like an application "
                       "permission — it applies to every user who signs in, not just the one who consented.",
                evidence={"scopes": grant.get("scopes"), "resource": grant.get("resource"),
                          "max_tier": grant.get("max_tier")},
                discriminator=str(grant.get("id") or ""),
                portal_link=model.portal_sp(str(sp.get("object_id"))),
            ))
    return out


def _multitenant_unverified(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for app in _apps(data):
        if not app.get("multi_tenant") or app.get("verified_publisher"):
            continue
        sp = next((s for s in _sps(data) if s.get("app_id") == app.get("app_id")), None)
        powerful = [
            p["permission"] for p in ((sp or {}).get("granted_app_permissions") or [])
            if p.get("tier") in (TIER_CRITICAL, TIER_HIGH)
        ]
        if not powerful:
            continue
        oid, name = _credential_owner(app)
        out.append(model.finding(
            signal_id="app.multitenant_unverified", severity="high", pillar="app",
            object_kind="app", object_id=oid, object_name=name,
            title=f"'{name}' is multi-tenant, unverified and holds powerful permissions",
            detail="A multi-tenant application with no verified publisher can be consented to "
                   "elsewhere, and its powerful permissions here make it a high-value target.",
            evidence={"sign_in_audience": app.get("sign_in_audience"), "permissions": sorted(set(powerful)),
                      "app_id": app.get("app_id")},
            portal_link=model.portal_app(str(app.get("app_id") or "")),
        ))
    return out


def _redirect_uri_risky(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for app in _apps(data):
        risky = [r for r in app.get("redirect_uris") or [] if r.get("risk") in ("wildcard", "plaintext-http")]
        if not risky:
            continue
        oid, name = _credential_owner(app)
        out.append(model.finding(
            signal_id="app.redirect_uri_risky", severity="high", pillar="app",
            object_kind="app", object_id=oid, object_name=name,
            title=f"'{name}' has a risky redirect URI",
            detail="A wildcard or plaintext-HTTP redirect lets an attacker who controls a matching "
                   "host receive authorisation codes issued for this application.",
            evidence={"uris": risky, "app_id": app.get("app_id")},
            discriminator=",".join(sorted(r["uri"] for r in risky))[:120],
            portal_link=model.portal_app(str(app.get("app_id") or "")),
        ))
    return out


def _fic_untrusted(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("federated_credentials"):
        raise SignalUnavailable("Federated identity credentials were not collected.")
    out = []
    for app in _apps(data):
        bad = [
            f for f in app.get("federated_credentials") or []
            if not f.get("trusted") or f.get("wildcard_subject")
        ]
        if not bad:
            continue
        oid, name = _credential_owner(app)
        out.append(model.finding(
            signal_id="app.fic_untrusted_issuer", severity="critical", pillar="app",
            object_kind="app", object_id=oid, object_name=name,
            title=f"'{name}' trusts an unrecognized or wildcard federated identity credential",
            detail="A federated credential is a credential-less way to obtain this application's "
                   "tokens. An unexpected issuer, or a wildcard subject, is a persistence mechanism.",
            evidence={"credentials": [
                {"name": f.get("name"), "issuer": f.get("issuer"), "subject": f.get("subject"),
                 "trusted": f.get("trusted"), "wildcard_subject": f.get("wildcard_subject")}
                for f in bad
            ], "app_id": app.get("app_id")},
            discriminator=",".join(sorted(f.get("id", "") for f in bad)),
            portal_link=model.portal_app(str(app.get("app_id") or "")),
        ))
    return out


# ------------------------------------------------------------------- tenant consent
def _provisioning_failing(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("provisioning"):
        raise SignalUnavailable(
            "Provisioning jobs were not collected (needs Synchronization.Read.All)."
        )
    out = []
    for sp in _sps(data):
        bad = [j for j in sp.get("provisioning_jobs") or []
               if j.get("quarantine") or str(j.get("code", "")).lower() in ("quarantine", "paused")]
        if not bad:
            continue
        out.append(model.finding(
            signal_id="app.provisioning_failing", severity="high", pillar="app",
            object_kind="sp", object_id=str(sp.get("object_id")), object_name=sp.get("display_name") or "",
            title=f"Provisioning for '{sp.get('display_name')}' is quarantined",
            detail="User provisioning has stopped. Joiners are not being created and — more "
                   "importantly — leavers are not being de-provisioned in the target application.",
            evidence={"jobs": bad, "app_id": sp.get("app_id")},
            portal_link=model.portal_sp(str(sp.get("object_id"))),
        ))
    return out


def _no_ca_coverage(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    analysis = data.get("_ca_analysis") or {}
    if not analysis:
        raise SignalUnavailable("Conditional Access policies were not collected.")
    enforced = [p for p in analysis.get("policies") or [] if p.get("is_enforced")]
    if not enforced:
        raise SignalUnavailable(
            "No enforced Conditional Access policy exists, so per-application coverage is not "
            "meaningful — see ca.no_policies instead."
        )
    if any(p.get("targets_all_apps") for p in enforced):
        return []
    covered: set[str] = set()
    for p in enforced:
        conditions = p.get("conditions") or {}
        covered |= set(conditions.get("include_apps") or [])
        covered -= set(conditions.get("exclude_apps") or [])
    out = []
    for sp in _sps(data):
        if sp.get("is_first_party") or not sp.get("enabled") or sp.get("sp_type") != "Application":
            continue
        if sp.get("app_id") in covered:
            continue
        risk = (sp.get("risk") or {}).get("score", 0)
        if risk < 40:
            continue          # only worth reporting for applications that matter
        out.append(model.finding(
            signal_id="app.no_ca_coverage", severity="medium", pillar="app",
            object_kind="sp", object_id=str(sp.get("object_id")), object_name=sp.get("display_name") or "",
            title=f"'{sp.get('display_name')}' is not covered by any enforced Conditional Access policy",
            detail="A higher-risk application reachable with no policy in the way is a route around "
                   "every other control.",
            evidence={"risk_score": risk, "app_id": sp.get("app_id"),
                      "enforced_policies": len(enforced)},
            portal_link=model.portal_sp(str(sp.get("object_id"))),
        ))
    return out


def _user_consent_unrestricted(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policy = domain(data, "tenant").get("authorization_policy") or {}
    if not policy.get("present"):
        raise SignalUnavailable("The tenant authorization policy was not collected (needs Policy.Read.All).")
    if not policy.get("user_consent_unrestricted"):
        return []
    return [model.finding(
        signal_id="app.user_consent_unrestricted", severity="critical", pillar="app",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="Users may consent to any application",
        detail="Any user can grant a third-party application access to their mail, files and profile. "
               "This is the mechanism behind illicit-consent phishing.",
        evidence={"policies_assigned": policy.get("user_consent_policies")},
        portal_link=model.portal_consent(),
    )]


def _no_admin_consent_workflow(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    tenant = domain(data, "tenant")
    policy = tenant.get("admin_consent_policy") or {}
    authz = tenant.get("authorization_policy") or {}
    if not authz.get("present"):
        raise SignalUnavailable("The tenant authorization policy was not collected (needs Policy.Read.All).")
    if policy.get("is_enabled"):
        return []
    if authz.get("user_consent_unrestricted"):
        return []          # the unrestricted-consent finding is the real problem there
    return [model.finding(
        signal_id="app.no_admin_consent_workflow", severity="medium", pillar="app",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="The admin consent request workflow is disabled",
        detail="With user consent restricted and no request workflow, users hit a dead end and "
               "route around IT — often by using a personal account instead.",
        evidence={"admin_consent_enabled": policy.get("is_enabled"),
                  "user_consent_policies": authz.get("user_consent_policies")},
        portal_link=model.portal_consent(),
    )]


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="app.secret_expired", title="Expired client secrets still present",
        question="Which applications carry dead credentials?",
        why="An expired credential left on the object hides which credential is actually in use, "
            "and usually means nobody is watching this application at all.",
        pillar="app", severity="high", weight=5, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Remove expired credentials from the application registration.",
        doc_link=APP_DOC, evaluate=_credentials("secret", True, "app.secret_expired", "high"),
    ),
    SignalSpec(
        id="app.secret_expiring", title="Client secrets expiring soon",
        question="What breaks in the next 90 days?",
        why="An unnoticed secret expiry is one of the most common causes of a production outage "
            "that looks like a platform failure.",
        pillar="app", severity="medium", weight=5, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Rotate the credential and record the owner so the next expiry is expected.",
        doc_link=APP_DOC, evaluate=_credentials("secret", False, "app.secret_expiring", "medium"),
        tags=("quick-win",),
    ),
    SignalSpec(
        id="app.cert_expired", title="Expired certificates still present",
        question="Which applications carry dead certificates?",
        why="Same as an expired secret — dead weight that obscures the live credential.",
        pillar="app", severity="high", weight=5, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Remove expired certificates from the application registration.",
        doc_link=APP_DOC, evaluate=_credentials("certificate", True, "app.cert_expired", "high"),
    ),
    SignalSpec(
        id="app.cert_expiring", title="Certificates expiring soon",
        question="Which certificate-authenticated applications are about to fail?",
        why="Certificate expiry breaks workloads silently and at the worst possible moment.",
        pillar="app", severity="medium", weight=5, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Renew the certificate and update the workload before the expiry date.",
        doc_link=APP_DOC, evaluate=_credentials("certificate", False, "app.cert_expiring", "medium"),
    ),
    SignalSpec(
        id="app.secret_long_lived", title="Credentials with an excessive lifetime",
        question="Which credentials are valid for years?",
        why="A multi-year credential is a multi-year window for a leaked secret to keep working.",
        pillar="app", severity="medium", weight=4, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Issue credentials with a lifetime of months, not years; prefer certificates or "
                    "federated identity credentials.",
        doc_link=APP_DOC, evaluate=_secret_long_lived,
    ),
    SignalSpec(
        id="app.secret_never_rotated", title="Credentials that have never been rotated",
        question="Which secrets have been in place for over a year?",
        why="Long-lived secrets end up in pipeline logs, laptops and wikis.",
        pillar="app", severity="medium", weight=4, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Rotate on a schedule, or move to workload identity federation and remove the secret.",
        doc_link="https://learn.microsoft.com/entra/workload-id/workload-identity-federation",
        evaluate=_secret_never_rotated,
    ),
    SignalSpec(
        id="app.too_many_credentials", title="Applications with more credentials than they need",
        question="Which applications have accumulated credentials?",
        why="More credentials than a rotation requires usually means nobody knows which are live.",
        pillar="app", severity="low", weight=2, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Keep at most two active credentials — the current one and its replacement during rotation.",
        doc_link=APP_DOC, evaluate=_too_many_credentials,
    ),
    SignalSpec(
        id="app.ownerless", title="Applications with no owner",
        question="Who would you call about this application?",
        why="Ownerless applications never get their credentials rotated, their permissions reviewed "
            "or their lifecycle ended.",
        pillar="app", severity="high", weight=6, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        benchmarks=("MCSB IM-3",), impact=IMPACT_RATIO, population=pop_applications,
        remediation="Assign at least two directory owners and record a business owner.",
        remediation_steps=(
            "Entra admin center > App registrations > select the app > Owners.",
            "Add two owners; prefer a group over an individual so departures do not orphan it.",
        ),
        doc_link=APP_DOC, evaluate=_ownerless,
    ),
    SignalSpec(
        id="app.orphaned_sp", title="Service principals with no application object",
        question="Which enterprise applications outlived their registration?",
        why="Their permission grants and assignments survive with nothing defining them.",
        pillar="app", severity="medium", weight=4, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_service_principals,
        remediation="Delete the enterprise application if it is genuinely unused.",
        doc_link=APP_DOC, evaluate=_orphaned_sp,
    ),
    SignalSpec(
        id="app.consent_grant_capable", title="Applications that can grant themselves any permission",
        question="Which applications hold a tenant-takeover primitive?",
        why="An application that can assign app roles or directory roles can escalate itself to "
            "Global Administrator. Compromising its secret compromises the tenant.",
        pillar="app", severity="critical", weight=10, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        benchmarks=("MCSB PA-7",), impact=IMPACT_SATURATING, saturation=2,
        remediation="Remove the permission and replace it with the narrowest scope the workload needs.",
        remediation_steps=(
            "Entra admin center > Enterprise applications > Permissions.",
            "Revoke AppRoleAssignment.ReadWrite.All / RoleManagement.ReadWrite.Directory.",
            "Re-grant only the specific permission the workload actually calls.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/enterprise-apps/manage-application-permissions",
        evaluate=_consent_grant, tags=("zero-trust",),
    ),
    SignalSpec(
        id="app.directory_write_permission", title="Applications with directory write access",
        question="Which applications can rewrite the directory?",
        why="Directory write allows creating accounts, adding credentials to other applications and "
            "changing group membership — all of which are privilege escalation.",
        pillar="app", severity="critical", weight=9, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Replace with a read-only or narrowly scoped permission.",
        doc_link=APP_DOC, evaluate=_directory_write,
    ),
    SignalSpec(
        id="app.tenant_wide_mail", title="Applications that can read all mail",
        question="Which applications can read everyone's mailbox?",
        why="An application permission on mail is not scoped to one mailbox — it reaches every "
            "mailbox in the organization.",
        pillar="app", severity="critical", weight=9, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Scope with application access policies, or move to delegated permissions.",
        doc_link="https://learn.microsoft.com/graph/auth-limit-mailbox-access",
        evaluate=_mail,
    ),
    SignalSpec(
        id="app.tenant_wide_files", title="Applications that can read all files",
        question="Which applications can read every SharePoint site and OneDrive?",
        why="Tenant-wide file access reaches every document library and personal drive.",
        pillar="app", severity="critical", weight=9, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Use Sites.Selected and grant per-site access instead.",
        doc_link="https://learn.microsoft.com/sharepoint/dev/solution-guidance/security-apponly-azuread",
        evaluate=_files,
    ),
    SignalSpec(
        id="app.tenant_wide_chat", title="Applications that can read Teams messages",
        question="Which applications can read private chats?",
        why="Tenant-wide messaging access reaches private conversations, not only channels.",
        pillar="app", severity="critical", weight=8, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Apply resource-specific consent, or remove the permission.",
        doc_link="https://learn.microsoft.com/microsoftteams/platform/graph-api/rsc/resource-specific-consent",
        evaluate=_chat,
    ),
    SignalSpec(
        id="app.high_privilege_permission", title="Applications with other high-privilege permissions",
        question="Which applications hold broad Graph permissions beyond mail, files and chat?",
        why="These permissions apply tenant-wide with no per-object scoping.",
        pillar="app", severity="high", weight=6, object_kind="sp",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_service_principals,
        remediation="Review each grant against what the workload actually calls and revoke the rest.",
        doc_link=APP_DOC, evaluate=_high_privilege,
    ),
    SignalSpec(
        id="app.admin_consent_all_principals", title="Tenant-wide delegated consent grants",
        question="Which delegated grants apply to every user?",
        why="A grant consented for all principals behaves like an application permission — it "
            "applies to everyone who signs in, not just the person who clicked accept.",
        pillar="app", severity="high", weight=6, object_kind="sp",
        domains=("apps",), requires=("Directory.Read.All",),
        impact=IMPACT_RATIO, population=pop_service_principals,
        remediation="Revoke the tenant-wide grant and consent per user, or narrow the scopes.",
        doc_link=CONSENT_DOC, evaluate=_admin_consent_all_principals,
    ),
    SignalSpec(
        id="app.user_consent_unrestricted", title="Users may consent to any application",
        question="Can a user grant a third-party app access to their data unaided?",
        why="Unrestricted user consent is the mechanism behind illicit-consent phishing, and it "
            "bypasses every review process you have.",
        pillar="app", severity="critical", weight=9, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",),
        benchmarks=("CIS 5.1.5", "MCSB PA-7"), impact=IMPACT_BINARY,
        remediation="Restrict user consent to verified publishers and low-impact permissions, and "
                    "enable the admin consent request workflow.",
        remediation_steps=(
            "Entra admin center > Enterprise applications > Consent and permissions > User consent settings.",
            "Select 'Allow user consent for apps from verified publishers, for selected permissions'.",
            "Enable the admin consent request workflow so users have a route to ask.",
        ),
        doc_link=CONSENT_DOC, evaluate=_user_consent_unrestricted, tags=("quick-win",),
    ),
    SignalSpec(
        id="app.no_admin_consent_workflow", title="Admin consent request workflow disabled",
        question="When a user needs an app, can they ask for it?",
        why="Restricted consent with no request path pushes people to personal accounts and shadow IT.",
        pillar="app", severity="medium", weight=3, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",), impact=IMPACT_BINARY,
        remediation="Enable the admin consent request workflow and nominate reviewers.",
        doc_link="https://learn.microsoft.com/entra/identity/enterprise-apps/configure-admin-consent-workflow",
        evaluate=_no_admin_consent_workflow,
    ),
    SignalSpec(
        id="app.multitenant_unverified", title="Unverified multi-tenant apps with powerful permissions",
        question="Which powerful applications could also be consented to elsewhere?",
        why="No verified publisher plus multi-tenant plus powerful permissions is the profile of a "
            "consent-phishing application.",
        pillar="app", severity="high", weight=6, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Complete publisher verification, or make the application single-tenant.",
        doc_link="https://learn.microsoft.com/entra/identity-platform/publisher-verification-overview",
        evaluate=_multitenant_unverified,
    ),
    SignalSpec(
        id="app.redirect_uri_risky", title="Risky redirect URIs",
        question="Could an authorisation code be delivered somewhere unintended?",
        why="A wildcard or plaintext-HTTP redirect lets whoever controls a matching host receive "
            "tokens issued for this application.",
        pillar="app", severity="high", weight=6, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_RATIO, population=pop_applications,
        remediation="Use exact HTTPS redirect URIs only.",
        doc_link="https://learn.microsoft.com/entra/identity-platform/reply-url",
        evaluate=_redirect_uri_risky,
    ),
    SignalSpec(
        id="app.fic_untrusted_issuer", title="Federated credentials from unrecognized issuers",
        question="Which external systems can mint tokens for your applications?",
        why="A federated identity credential is a credential-less persistence mechanism — no secret "
            "to expire, no secret to rotate, and nothing in the credential list to notice.",
        pillar="app", severity="critical", weight=8, object_kind="app",
        domains=("apps",), requires=("Application.Read.All",),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Remove unexpected federated credentials and tighten wildcard subjects to exact values.",
        doc_link="https://learn.microsoft.com/entra/workload-id/workload-identity-federation",
        evaluate=_fic_untrusted,
    ),
    SignalSpec(
        id="app.provisioning_failing", title="User provisioning is quarantined",
        question="Which applications have stopped receiving joiner and leaver updates?",
        why="A quarantined provisioning job means leavers are not being de-provisioned in the "
            "target application — offboarding you believe happened, did not.",
        pillar="app", severity="high", weight=6, object_kind="sp",
        domains=("apps",), requires=("Synchronization.Read.All",),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Resolve the provisioning error and restart the synchronisation job.",
        remediation_steps=(
            "Entra admin center > Enterprise applications > the app > Provisioning.",
            "Review the quarantine reason and fix the credential or mapping error.",
            "Restart provisioning and confirm the next cycle succeeds.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/app-provisioning/application-provisioning-quarantine-status",
        evaluate=_provisioning_failing,
    ),
    SignalSpec(
        id="app.no_ca_coverage", title="Higher-risk applications outside Conditional Access",
        question="Which risky applications can be reached with no policy in the way?",
        why="An application that matters, reachable with no control applied, is a route around "
            "every other protection in the tenant.",
        pillar="app", severity="medium", weight=5, object_kind="sp",
        domains=("apps", "ca"), requires=("Policy.Read.All", "Application.Read.All"), licence="p1",
        impact=IMPACT_RATIO, population=pop_service_principals,
        remediation="Target 'All cloud apps' in your baseline policies rather than an allow-list.",
        doc_link="https://learn.microsoft.com/entra/identity/conditional-access/overview",
        evaluate=_no_ca_coverage,
    ),
]
