"""Conditional Access pillar.

Every signal here reads the pre-computed analysis produced by :mod:`app.entra.ca_engine`
(stored on the snapshot as ``_ca_analysis``) rather than re-resolving policies. Resolving
include/exclude sets is O(policies x users); doing it once per snapshot instead of once per
signal is the difference between a page that loads and one that does not.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors.ca import STATE_REPORT_ONLY
from app.entra.collectors.roles import privileged_principal_ids
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    pop_enabled_members,
    pop_policies,
    user_index,
)

CA_DOC = "https://learn.microsoft.com/entra/identity/conditional-access/overview"


def _analysis(data: dict[str, Any]) -> dict[str, Any]:
    analysis = data.get("_ca_analysis")
    if not isinstance(analysis, dict) or not analysis:
        raise SignalUnavailable("Conditional Access policies were not collected.")
    return analysis


def _policies(data: dict[str, Any]) -> list[dict[str, Any]]:
    return _analysis(data).get("policies") or []


def _conflicts(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [c for c in (_analysis(data).get("conflicts") or []) if c.get("kind") == kind]


def _name(data: dict[str, Any], uid: str) -> str:
    u = user_index(data).get(uid) or {}
    return u.get("upn") or u.get("display_name") or uid


# ------------------------------------------------------------------------- evaluators
def _no_policies(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    analysis = _analysis(data)
    if analysis["counts"]["enforced"] > 0:
        return []
    return [model.finding(
        signal_id="ca.no_policies", severity="critical", pillar="ca",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="No enforced Conditional Access policy exists",
        detail="Nothing in this tenant is protected by Conditional Access. Sign-ins are governed "
               "by per-user settings alone.",
        evidence={"total_policies": analysis["counts"]["policies"],
                  "report_only": analysis["counts"]["report_only"],
                  "disabled": analysis["counts"]["disabled"]},
        portal_link=model.portal_ca_policy(""),
    )]


def _cohort_uncovered(data: dict[str, Any], cohort_key: str, signal_id: str, severity: str,
                      control: str = "mfa", app_class: str = "all_cloud_apps") -> list[dict[str, Any]]:
    analysis = _analysis(data)
    coverage = analysis.get("coverage") or {}
    row = next((r for r in coverage.get("matrix") or [] if r.get("cohort") == cohort_key), None)
    if not row or not row.get("size"):
        return []
    cell = (row.get("cells") or {}).get(f"{app_class}|{control}") or {}
    uncovered = cell.get("uncovered_total") or 0
    if not uncovered:
        return []
    sample = cell.get("uncovered_sample") or []
    return [model.finding(
        signal_id=signal_id, severity=severity, pillar="ca",
        object_kind="user", object_id=uid, object_name=_name(data, uid),
        title=f"{_name(data, uid)} is not covered by any enforced Conditional Access policy requiring MFA",
        detail="No enabled policy applies an MFA control to this principal for all cloud apps.",
        evidence={"cohort": row.get("label"), "cohort_size": row.get("size"),
                  "covered": cell.get("users_covered"), "cell_state": cell.get("state")},
        portal_link=model.portal_user(uid),
    ) for uid in sample]


def _admins_uncovered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return _cohort_uncovered(data, "privileged", "ca.admins_uncovered", "critical")


def _users_uncovered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return _cohort_uncovered(data, "members", "ca.users_uncovered", "high")


def _guests_uncovered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return _cohort_uncovered(data, "guests", "ca.guests_uncovered", "high")


def _apps_uncovered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    headline = (_analysis(data).get("coverage") or {}).get("headline") or {}
    if not headline.get("uncovered_apps"):
        return []
    return [model.finding(
        signal_id="ca.apps_uncovered", severity="high", pillar="ca",
        object_kind="app", object_id=str(app.get("app_id") or ""), object_name=str(app.get("name") or ""),
        title=f"Application '{app.get('name')}' is not in scope of any enforced policy",
        detail="No enabled Conditional Access policy targets this application, so sign-ins to it are "
               "governed by nothing.",
        evidence={"total_uncovered_apps": headline.get("uncovered_apps"),
                  "total_apps": headline.get("total_apps")},
        portal_link=model.portal_app(str(app.get("app_id") or "")),
    ) for app in headline.get("uncovered_app_sample") or []]


def _legacy_auth_not_blocked(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policies = _policies(data)
    if any(p["is_enforced"] and p["blocks_legacy"] for p in policies):
        return []
    return [model.finding(
        signal_id="ca.legacy_auth_not_blocked", severity="critical", pillar="ca",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="No enforced policy blocks legacy authentication",
        detail="Legacy protocols (Exchange ActiveSync, IMAP, POP, SMTP AUTH, older Office clients) "
               "cannot perform MFA. While they are reachable, every MFA policy can be bypassed.",
        evidence={"enforced_policies": sum(1 for p in policies if p["is_enforced"])},
        portal_link=model.portal_ca_policy(""),
    )]


def _admin_portal_unprotected(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    analysis = _analysis(data)
    row = next((r for r in (analysis.get("coverage") or {}).get("matrix") or []
                if r.get("cohort") == "privileged"), None)
    if not row or not row.get("size"):
        return []
    cell = (row.get("cells") or {}).get("admin_portals|mfa") or {}
    if cell.get("state") == "enforced":
        return []
    return [model.finding(
        signal_id="ca.admin_portal_unprotected", severity="high", pillar="ca",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Microsoft Admin Portals",
        title="Microsoft Admin Portals are not fully protected by MFA for privileged users",
        detail="The Entra, Azure, Exchange and Intune admin portals should require MFA for every "
               "principal that holds a directory role.",
        evidence={"cell_state": cell.get("state"), "covered": cell.get("covered"), "size": cell.get("size")},
        portal_link=model.portal_ca_policy(""),
    )]


def _disabled_policy(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.disabled_policy", severity="low", pillar="ca",
        object_kind="policy", object_id=p["id"], object_name=p.get("display_name") or p["id"],
        title=f"Conditional Access policy '{p.get('display_name')}' is disabled",
        detail="A disabled policy protects nobody. Either enable it, or delete it so the policy set "
               "reflects reality.",
        evidence={"state": p.get("state"), "controls": p.get("controls"),
                  "effective_users": p.get("effective_user_count")},
        portal_link=model.portal_ca_policy(p["id"]),
    ) for p in _policies(data) if p["is_disabled"]]


def _report_only_stale(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for p in _policies(data):
        if p.get("state") != STATE_REPORT_ONLY:
            continue
        age = ctx.days_since(str(p.get("modified_at") or p.get("created_at") or ""))
        if age is None or age < ctx.stale_days:
            continue
        out.append(model.finding(
            signal_id="ca.report_only_stale", severity="medium", pillar="ca",
            object_kind="policy", object_id=p["id"], object_name=p.get("display_name") or p["id"],
            title=f"'{p.get('display_name')}' has been report-only for {age} days",
            detail="Report-only policies never protect anyone. A policy left in report-only for "
                   "months is usually one that everyone has stopped looking at.",
            evidence={"days_report_only": age, "modified_at": p.get("modified_at"),
                      "effective_users": p.get("effective_user_count")},
            portal_link=model.portal_ca_policy(p["id"]),
        ))
    return out


def _policy_no_effect(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.policy_no_effect", severity="medium", pillar="ca",
        object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
        title=f"Policy '{c['policy_name']}' can never apply",
        detail=c["detail"],
        evidence={"kind": c["kind"], "state": c.get("policy_state")},
        portal_link=model.portal_ca_policy(c["policy_id"]),
    ) for c in _conflicts(data, "policy_no_effect")]


def _unreachable(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.unreachable_condition", severity="medium", pillar="ca",
        object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
        title=f"Policy '{c['policy_name']}' has a self-cancelling condition",
        detail=c["detail"],
        evidence={"kind": c["kind"]},
        portal_link=model.portal_ca_policy(c["policy_id"]),
    ) for c in _conflicts(data, "unreachable_condition")]


def _conflicting_block_grant(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.conflicting_block_grant", severity="high", pillar="ca",
        object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
        title=f"'{c['policy_name']}' is contradicted by the block policy '{c['other_name']}'",
        detail=c["detail"],
        evidence={"blocked_by": c["other_name"], "affected_users": c["affected"]},
        discriminator=c["other_id"],
        portal_link=model.portal_ca_policy(c["policy_id"]),
    ) for c in _conflicts(data, "conflicting_block_grant")]


def _redundant_policy(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for kind, severity in (("redundant_policy", "low"), ("duplicate_intent", "low")):
        for c in _conflicts(data, kind):
            out.append(model.finding(
                signal_id="ca.redundant_policy", severity=severity, pillar="ca",
                object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
                title=f"'{c['policy_name']}' adds nothing over '{c['other_name']}'",
                detail=c["detail"],
                evidence={"kind": kind, "subsumed_by": c["other_name"]},
                discriminator=c["other_id"],
                portal_link=model.portal_ca_policy(c["policy_id"]),
            ))
    return out


def _exclusion_privileged(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.exclusion_privileged", severity="critical", pillar="ca",
        object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
        title=f"Privileged principals are excluded from '{c['policy_name']}'",
        detail=c["detail"] + " An exclusion that removes the very population a control exists to "
                             "protect defeats the policy.",
        evidence={"excluded_privileged": c["affected"], "sample": c["sample"]},
        portal_link=model.portal_ca_policy(c["policy_id"]),
    ) for c in _conflicts(data, "exclusion_privileged")]


def _exclusion_sprawl(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return [model.finding(
        signal_id="ca.exclusion_sprawl", severity="high", pillar="ca",
        object_kind="policy", object_id=c["policy_id"], object_name=c["policy_name"],
        title=f"'{c['policy_name']}' excludes a large share of its targeted users",
        detail=c["detail"] + " Exclusions accumulate silently; each one is a permanent hole.",
        evidence={"excluded": c["affected"]},
        portal_link=model.portal_ca_policy(c["policy_id"]),
    ) for c in _conflicts(data, "exclusion_sprawl")]


def _breakglass_missing(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    analysis = _analysis(data)
    bg = analysis.get("breakglass") or {}
    if bg.get("candidate_count"):
        return []
    if not analysis["counts"]["enforced"]:
        return []          # ca.no_policies already covers this tenant
    return [model.finding(
        signal_id="ca.breakglass_missing", severity="high", pillar="ca",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="No emergency access (break-glass) account could be identified",
        detail="If Conditional Access, MFA or federation fails, an account excluded from those "
               "controls is the only way back in. Microsoft recommends at least two.",
        evidence={"enforced_policies": analysis["counts"]["enforced"]},
        portal_link=model.portal_ca_policy(""),
    )]


def _breakglass_over_covered(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    bg = _analysis(data).get("breakglass") or {}
    return [model.finding(
        signal_id="ca.breakglass_over_covered", severity="critical", pillar="ca",
        object_kind="user", object_id=c["user_id"], object_name=c.get("upn") or c["user_id"],
        title=f"Break-glass account {c.get('upn')} is captured by an enforced policy it cannot satisfy",
        detail="This confirmed emergency account is covered by a policy requiring a control it has "
               "not registered. In an outage it would be locked out exactly when it is needed.",
        evidence={"covered_by": c.get("covered_by"), "mfa_registered": c.get("mfa_registered"),
                  "score": c.get("score"), "reasons": c.get("reasons")},
        portal_link=model.portal_user(c["user_id"]),
    ) for c in bg.get("over_covered") or []]


def _no_device_compliance(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policies = _policies(data)
    if any(p["is_enforced"] and ({"compliant_device", "hybrid_joined"} & set(p["controls"])) for p in policies):
        return []
    return [model.finding(
        signal_id="ca.no_device_compliance", severity="medium", pillar="ca",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="No enforced policy requires a compliant or hybrid-joined device",
        detail="Without a device control, a valid credential from any unmanaged machine is enough.",
        evidence={"enforced_policies": sum(1 for p in policies if p["is_enforced"])},
        portal_link=model.portal_ca_policy(""),
    )]


def _risk_policy(kind: str, signal_id: str):
    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        policies = _policies(data)
        field = "sign_in_risk" if kind == "signin" else "user_risk"
        if any(p["is_enforced"] and (p.get("conditions") or {}).get(field) for p in policies):
            return []
        label = "sign-in risk" if kind == "signin" else "user risk"
        return [model.finding(
            signal_id=signal_id, severity="high", pillar="ca",
            object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
            title=f"No enforced Conditional Access policy responds to {label}",
            detail=f"Identity Protection detects {label}, but nothing acts on it — detections are "
                   "recorded and then ignored.",
            evidence={"enforced_policies": sum(1 for p in policies if p["is_enforced"])},
            portal_link=model.portal_ca_policy(""),
        )]
    return _inner


def _weak_auth_strength_admins(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = domain(data, "roles")
    privileged = privileged_principal_ids(roles)
    if not privileged:
        return []
    out = []
    for p in _policies(data):
        if not p["is_enforced"] or "mfa" not in p["controls"]:
            continue
        if "phishing_resistant" in p["controls"]:
            continue
        if not (privileged & set(p["effective_ids"])):
            continue
        out.append(model.finding(
            signal_id="ca.weak_auth_strength_admins", severity="high", pillar="ca",
            object_kind="policy", object_id=p["id"], object_name=p.get("display_name") or p["id"],
            title=f"'{p.get('display_name')}' protects administrators with ordinary MFA",
            detail="Policies covering privileged principals should require a phishing-resistant "
                   "authentication strength rather than any second factor.",
            evidence={"privileged_covered": len(privileged & set(p["effective_ids"])),
                      "controls": p["controls"], "auth_strength": p.get("grant", {}).get("auth_strength_name")},
            portal_link=model.portal_ca_policy(p["id"]),
        ))
    return out


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="ca.no_policies", title="No enforced Conditional Access policy",
        question="Is Conditional Access doing anything at all in this tenant?",
        why="Without an enforced policy, Conditional Access is providing no protection whatsoever.",
        pillar="ca", severity="critical", weight=10, object_kind="tenant",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        benchmarks=("CIS 1.2", "MCSB IM-7"), impact=IMPACT_BINARY,
        remediation="Deploy the baseline policy set: block legacy auth, require MFA for admins, then for all users.",
        remediation_steps=(
            "Entra admin center > Protection > Conditional Access > Policies.",
            "Start from the Microsoft-managed templates in report-only mode.",
            "Simulate each policy before enforcing it.",
        ),
        doc_link=CA_DOC, evaluate=_no_policies,
    ),
    SignalSpec(
        id="ca.admins_uncovered", title="Privileged users not covered by an enforced MFA policy",
        question="Which administrators are protected by nothing?",
        why="An unprotected administrator is the single most valuable target in the tenant.",
        pillar="ca", severity="critical", weight=10, object_kind="user",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        benchmarks=("CIS 1.1.1",), impact=IMPACT_SATURATING, saturation=3,
        remediation="Create a policy targeting directory roles that requires phishing-resistant MFA.",
        remediation_steps=(
            "Conditional Access > New policy > Users > Directory roles > select all privileged roles.",
            "Cloud apps: All. Grant: require authentication strength (phishing-resistant).",
            "Exclude only confirmed break-glass accounts, then simulate before enforcing.",
        ),
        doc_link=CA_DOC, evaluate=_admins_uncovered, tags=("zero-trust",),
    ),
    SignalSpec(
        id="ca.users_uncovered", title="Members not covered by an enforced MFA policy",
        question="How much of the workforce signs in with no Conditional Access control?",
        why="Uncovered users are the tenant's real attack surface, and the number nobody can get "
            "from the portal without joining every policy by hand.",
        pillar="ca", severity="high", weight=9, object_kind="user",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Extend an all-users MFA policy, simulating first to find who would be blocked.",
        remediation_steps=(
            "Run the Conditional Access simulator against the proposed policy.",
            "Register MFA methods for the accounts that would be hard-blocked.",
            "Roll out in report-only, review, then enforce.",
        ),
        doc_link=CA_DOC, evaluate=_users_uncovered,
    ),
    SignalSpec(
        id="ca.guests_uncovered", title="Guests not covered by an enforced MFA policy",
        question="Are external identities held to any standard at all?",
        why="Guests usually outnumber the controls written for them, and their home tenant's "
            "security posture is not yours to manage.",
        pillar="ca", severity="high", weight=7, object_kind="user",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=lambda d: max(1, len(
            [u for u in (d.get("people") or {}).get("users") or [] if u.get("user_type") == "Guest" and u.get("enabled")]
        )),
        remediation="Create a policy targeting Guest or external users requiring MFA.",
        remediation_steps=(
            "Conditional Access > New policy > Users > Guest or external users > All guest types.",
            "Cloud apps: All. Grant: require MFA.",
            "Consider also requiring compliant devices for privileged guest scenarios.",
        ),
        doc_link=CA_DOC, evaluate=_guests_uncovered,
    ),
    SignalSpec(
        id="ca.apps_uncovered", title="Applications outside every enforced policy",
        question="Which applications can be reached with no policy in the way?",
        why="A single unprotected application is a way around every other control.",
        pillar="ca", severity="high", weight=7, object_kind="app",
        domains=("ca", "apps", "people"), requires=("Policy.Read.All", "Application.Read.All"), licence="p1",
        impact=IMPACT_RATIO, population=lambda d: max(1, len(
            [s for s in (d.get("apps") or {}).get("service_principals") or []
             if s.get("enabled") and s.get("sp_type") == "Application"]
        )),
        remediation="Target 'All cloud apps' rather than named applications, then exclude deliberately.",
        remediation_steps=(
            "Review each policy's application scope.",
            "Prefer All cloud apps with explicit exclusions over an allow-list of applications.",
        ),
        doc_link=CA_DOC, evaluate=_apps_uncovered,
    ),
    SignalSpec(
        id="ca.legacy_auth_not_blocked", title="Legacy authentication is not blocked",
        question="Can a client bypass MFA by using an old protocol?",
        why="Legacy protocols cannot perform MFA. While they are reachable, every MFA policy in "
            "the tenant has a documented bypass.",
        pillar="ca", severity="critical", weight=10, object_kind="tenant",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        benchmarks=("CIS 1.1.5", "MCSB IM-7"), impact=IMPACT_BINARY,
        remediation="Create a policy blocking the Exchange ActiveSync and 'other clients' client-app types.",
        remediation_steps=(
            "Conditional Access > New policy > Users: All. Cloud apps: All.",
            "Conditions > Client apps: select only 'Exchange ActiveSync clients' and 'Other clients'.",
            "Grant: Block. Run in report-only first to find the clients that would break.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/conditional-access/policy-block-legacy-authentication",
        evaluate=_legacy_auth_not_blocked, tags=("quick-win", "zero-trust"),
    ),
    SignalSpec(
        id="ca.admin_portal_unprotected", title="Microsoft Admin Portals not fully MFA-protected",
        question="Can an administrator open the Entra portal without MFA?",
        why="The admin portals are where privilege is exercised; they deserve the strongest control "
            "in the tenant.",
        pillar="ca", severity="high", weight=7, object_kind="tenant",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_BINARY,
        remediation="Add a policy targeting the Microsoft Admin Portals application for all privileged roles.",
        remediation_steps=(
            "Conditional Access > New policy > Cloud apps > Microsoft Admin Portals.",
            "Users: directory roles (all privileged). Grant: phishing-resistant MFA.",
        ),
        doc_link=CA_DOC, evaluate=_admin_portal_unprotected,
    ),
    SignalSpec(
        id="ca.disabled_policy", title="Disabled Conditional Access policies",
        question="Which policies look like protection but are switched off?",
        why="A disabled policy in the list creates false assurance during a review.",
        pillar="ca", severity="low", weight=3, object_kind="policy",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Enable the policy, or delete it so the policy set reflects reality.",
        doc_link=CA_DOC, evaluate=_disabled_policy,
    ),
    SignalSpec(
        id="ca.report_only_stale", title="Report-only policies never promoted",
        question="Which policies have been 'about to be enabled' for months?",
        why="Report-only protects nobody. A stale one usually means the rollout stalled and was forgotten.",
        pillar="ca", severity="medium", weight=5, object_kind="policy",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Review the report-only impact, then enforce or remove the policy.",
        remediation_steps=(
            "Compare the report-only outcome against enforced behaviour.",
            "Remediate the accounts that would break, then set the policy to On.",
        ),
        doc_link=CA_DOC, evaluate=_report_only_stale,
    ),
    SignalSpec(
        id="ca.policy_no_effect", title="Policies that can never apply",
        question="Which policies resolve to nobody or nothing?",
        why="A policy with an empty user or application scope is decoration; it also hides the fact "
            "that the intended population is unprotected.",
        pillar="ca", severity="medium", weight=5, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Fix the scope or delete the policy.",
        doc_link=CA_DOC, evaluate=_policy_no_effect,
    ),
    SignalSpec(
        id="ca.unreachable_condition", title="Policies with a self-cancelling condition",
        question="Which policies include and exclude the same thing?",
        why="A condition that is both included and excluded can never be satisfied, so the policy "
            "silently never fires.",
        pillar="ca", severity="medium", weight=4, object_kind="policy",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Remove the contradictory include or exclude condition.",
        doc_link=CA_DOC, evaluate=_unreachable,
    ),
    SignalSpec(
        id="ca.conflicting_block_grant", title="Grant policies contradicted by a block policy",
        question="Which policies can never be satisfied because a block always wins?",
        why="Entra evaluates every policy and a block beats any grant. Administrators frequently "
            "assume an ordering that does not exist.",
        pillar="ca", severity="high", weight=6, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Narrow the block policy's scope, or accept that the grant policy is inert for that population.",
        doc_link=CA_DOC, evaluate=_conflicting_block_grant,
    ),
    SignalSpec(
        id="ca.redundant_policy", title="Redundant or duplicate policies",
        question="Which policies add nothing over another?",
        why="Every extra policy is another thing to reason about during an incident.",
        pillar="ca", severity="low", weight=3, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Consolidate into the broader policy.",
        doc_link=CA_DOC, evaluate=_redundant_policy,
    ),
    SignalSpec(
        id="ca.exclusion_privileged", title="Privileged principals excluded from a security policy",
        question="Which exclusions remove the very people the control exists to protect?",
        why="An exclusion list is the quietest way to disable a control for exactly the wrong population.",
        pillar="ca", severity="critical", weight=9, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Remove the exclusion, or replace it with a confirmed break-glass account only.",
        remediation_steps=(
            "Open the policy's Users > Exclude tab.",
            "Remove privileged users and groups; keep only confirmed emergency accounts.",
            "Simulate the change before saving.",
        ),
        doc_link=CA_DOC, evaluate=_exclusion_privileged,
    ),
    SignalSpec(
        id="ca.exclusion_sprawl", title="Policies with excessive exclusions",
        question="Which policies have been eroded by accumulated exceptions?",
        why="Exclusions are added under pressure and never reviewed; each one is a permanent hole.",
        pillar="ca", severity="high", weight=6, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Move exceptions into a reviewed exclusion group with an access review attached.",
        doc_link=CA_DOC, evaluate=_exclusion_sprawl,
    ),
    SignalSpec(
        id="ca.breakglass_missing", title="No emergency access account identified",
        question="If Conditional Access breaks, can anyone still get in?",
        why="A misconfigured policy, a federation outage or an expired MFA provider can lock every "
            "administrator out. Microsoft recommends at least two excluded emergency accounts.",
        pillar="ca", severity="high", weight=7, object_kind="tenant",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_BINARY,
        remediation="Create two cloud-only Global Administrator accounts excluded from all policies, "
                    "with monitored, rotated credentials.",
        remediation_steps=(
            "Create two cloud-only accounts with permanent Global Administrator.",
            "Exclude them from every Conditional Access policy.",
            "Alert on any sign-in by them, and rotate their credentials on a schedule.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/role-based-access-control/security-emergency-access",
        evaluate=_breakglass_missing,
    ),
    SignalSpec(
        id="ca.breakglass_over_covered", title="Break-glass account captured by a policy it cannot satisfy",
        question="Would the emergency account be locked out in the emergency?",
        why="This is the single most expensive Conditional Access mistake in the field: the account "
            "that exists to recover the tenant is caught by the policy that broke it.",
        pillar="ca", severity="critical", weight=10, object_kind="user",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_BINARY,
        remediation="Exclude the confirmed emergency accounts from every enforced policy.",
        doc_link="https://learn.microsoft.com/entra/identity/role-based-access-control/security-emergency-access",
        evaluate=_breakglass_over_covered, tags=("breaking-change-risk",),
    ),
    SignalSpec(
        id="ca.no_device_compliance", title="No device-based control anywhere",
        question="Does any policy care what machine the sign-in came from?",
        why="Without a device control, a stolen credential works from any machine on the internet.",
        pillar="ca", severity="medium", weight=5, object_kind="tenant",
        domains=("ca",), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_BINARY,
        remediation="Require a compliant or hybrid-joined device for at least the privileged cohort.",
        doc_link=CA_DOC, evaluate=_no_device_compliance,
    ),
    SignalSpec(
        id="ca.no_signin_risk_policy", title="No sign-in risk policy",
        question="Does anything act on a risky sign-in?",
        why="Identity Protection will keep detecting risk that nothing responds to.",
        pillar="ca", severity="high", weight=6, object_kind="tenant",
        domains=("ca",), requires=("Policy.Read.All",), licence="p2",
        impact=IMPACT_BINARY,
        remediation="Create a policy requiring MFA at medium-or-above sign-in risk.",
        doc_link="https://learn.microsoft.com/entra/id-protection/howto-identity-protection-configure-risk-policies",
        evaluate=_risk_policy("signin", "ca.no_signin_risk_policy"),
    ),
    SignalSpec(
        id="ca.no_user_risk_policy", title="No user risk policy",
        question="Does anything act on a compromised user?",
        why="A confirmed-compromised user stays compromised until something forces a password change.",
        pillar="ca", severity="high", weight=6, object_kind="tenant",
        domains=("ca",), requires=("Policy.Read.All",), licence="p2",
        impact=IMPACT_BINARY,
        remediation="Create a policy requiring a secure password change at high user risk.",
        doc_link="https://learn.microsoft.com/entra/id-protection/howto-identity-protection-configure-risk-policies",
        evaluate=_risk_policy("user", "ca.no_user_risk_policy"),
    ),
    SignalSpec(
        id="ca.weak_auth_strength_admins", title="Administrators protected by ordinary MFA only",
        question="Are admin policies asking for any second factor, or a phishing-resistant one?",
        why="Push and code MFA can be relayed. For privileged access the control should be a bound "
            "credential, expressed as an authentication strength.",
        pillar="ca", severity="high", weight=6, object_kind="policy",
        domains=("ca", "people", "roles"), requires=("Policy.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_policies,
        remediation="Replace the MFA grant with the built-in phishing-resistant authentication strength.",
        doc_link="https://learn.microsoft.com/entra/identity/authentication/concept-authentication-strengths",
        evaluate=_weak_auth_strength_admins, tags=("zero-trust",),
    ),
]
