"""Application-class exposure detectors.

The pre-existing CA signals ask *are these users covered?*. These ask *is this class of
application covered, and does the policy that claims to cover it actually do so?* — which is a
different question with different answers, because a policy can reach every user and still miss
half the applications a class contains.

Every detector here reads the analysis already on the snapshot. None re-resolves policies.
"""
from __future__ import annotations

from typing import Any

from app.entra import ca_taxonomy, model
from app.entra.ca_coverage import CELL_COVERED, CELL_NA, CELL_PARTIAL, CELL_REPORT_ONLY
from app.entra.ca_engine import SESSION_CONTENT_CONTROLS, CTRL_BLOCK
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
)

CA_DOC = "https://learn.microsoft.com/entra/identity/conditional-access/concept-conditional-access-cloud-apps"


def _analysis(data: dict[str, Any]) -> dict[str, Any]:
    analysis = data.get("_ca_analysis")
    if not isinstance(analysis, dict) or not analysis:
        raise SignalUnavailable("Conditional Access policies were not collected.")
    return analysis


def _coverage(data: dict[str, Any]) -> dict[str, Any]:
    return _analysis(data).get("coverage") or {}


def _policies(data: dict[str, Any]) -> list[dict[str, Any]]:
    return _analysis(data).get("policies") or []


def _class_label(cid: str) -> str:
    for c in ca_taxonomy.classes():
        if c["id"] == cid:
            return c["label"]
    return cid


def _cells(data: dict[str, Any], cohort: str = "members"):
    row = next((r for r in _coverage(data).get("matrix") or [] if r.get("cohort") == cohort), None)
    return (row or {}).get("cells") or {}


def _name(data: dict[str, Any], uid: str) -> str:
    for u in ((data.get("people") or {}).get("users") or []):
        if str(u.get("id")) == uid:
            return str(u.get("upn") or u.get("display_name") or uid)
    return uid


# ------------------------------------------------------------------------- detectors
def _class_never_targeted(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """A whole class of applications that no enforced policy names, directly or via All."""
    targeted: set[str] = set()
    for p in _policies(data):
        if p.get("is_enforced"):
            targeted |= set(p.get("class_coverage") or {})
    out = []
    for cls in ca_taxonomy.classes():
        if cls.get("derived") or cls["id"] in targeted:
            continue
        # User-action classes have their own per-action detectors, which name the specific
        # action and give a remediation that fits it. Reporting the class as well would put
        # three findings on screen for one root cause, and an operator who closes the two
        # specific ones would still be left with a third that says nothing new.
        if cls.get("user_action_based"):
            continue
        out.append(model.finding(
            signal_id="ca.class_never_targeted", severity=cls.get("severity", "high"), pillar="ca",
            object_kind="app_class", object_id=cls["id"], object_name=cls["label"],
            title=f"No enforced policy targets {cls['label']}",
            detail=cls.get("exposure")
                or f"No enabled Conditional Access policy applies to {cls['label']}, so sign-ins "
                   "to those applications are governed by nothing.",
            evidence={"class_id": cls["id"], "taxonomy_version": _coverage(data).get("taxonomy_version")},
            portal_link=model.portal_ca_policy(""),
        ))
    return out


def _bundle_member_divergence(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """A policy names a bundle but reaches only part of it.

    The Office 365 target is a bundle whose membership Microsoft controls; naming it in a policy
    does not guarantee the policy reaches every app the bundle expands to in *this* tenant.
    """
    out = []
    for cohort_row in _coverage(data).get("matrix") or []:
        if cohort_row.get("cohort") != "members":
            continue
        for key, cell in (cohort_row.get("cells") or {}).items():
            cid, _, ctrl = key.partition("|")
            if cid != "office365_bundle" or cell.get("state") != CELL_PARTIAL:
                continue
            if not cell.get("apps_missing_total"):
                continue
            out.append(model.finding(
                signal_id="ca.bundle_member_divergence", severity="high", pillar="ca",
                object_kind="app_class", object_id=f"{cid}|{ctrl}", object_name=_class_label(cid),
                title=f"{_class_label(cid)}: {ctrl} does not reach every app in the bundle",
                detail=f"{cell['apps_missing_total']} of {cell['apps_total']} applications in this "
                       "bundle are not reached by any enforced policy applying this control. The "
                       "bundle's membership is defined by Microsoft and changes without notice.",
                evidence={"control": ctrl, "apps_total": cell.get("apps_total"),
                          "apps_covered": cell.get("apps_covered"),
                          "apps_missing": cell.get("apps_missing")},
                portal_link=model.portal_ca_policy(""),
            ))
    return out


def _dependency_split(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Teams protected but SharePoint/Exchange not — the classic data-path bypass.

    Teams content lives in SharePoint and Exchange. A policy that governs Teams while leaving
    those two open protects the front door and leaves the loading bay unlocked.
    """
    cells = _cells(data)
    out = []
    teams = "cc15fd57-2c6c-4117-a88c-83b1d56b4bbe"
    for ctrl in ("mfa", "compliant_or_hybrid_device", "block"):
        collab = cells.get(f"collaboration_content|{ctrl}") or {}
        if collab.get("state") not in (CELL_PARTIAL,):
            continue
        missing = set(collab.get("apps_missing") or [])
        if not missing or teams in missing:
            continue
        out.append(model.finding(
            signal_id="ca.dependency_split", severity="high", pillar="ca",
            object_kind="app_class", object_id=f"collaboration_content|{ctrl}",
            object_name="Collaboration content",
            title=f"Teams is covered by {ctrl} but its underlying content services are not",
            detail="Teams stores files in SharePoint and messages in Exchange. Applying this "
                   "control to Teams alone leaves the same data reachable through the services "
                   "beneath it.",
            evidence={"control": ctrl, "apps_missing": sorted(missing)},
            portal_link=model.portal_ca_policy(""),
        ))
    return out


def _no_session_control_on_content(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    cells = _cells(data)
    have = [c for c in SESSION_CONTENT_CONTROLS
            if (cells.get(f"collaboration_content|{c}") or {}).get("state") in
            (CELL_COVERED, CELL_PARTIAL)]
    if have:
        return []
    cell = cells.get("collaboration_content|mfa") or {}
    if cell.get("state") == CELL_NA:
        return []
    return [model.finding(
        signal_id="ca.no_session_control_on_content", severity="medium", pillar="ca",
        object_kind="app_class", object_id="collaboration_content",
        object_name="Collaboration content",
        title="No session control governs what happens after sign-in to content services",
        detail="Authentication is controlled but the session is not. Once a user is in, nothing "
               "limits download to an unmanaged device, re-evaluates the session when risk "
               "changes, or bounds how long it lasts.",
        evidence={"controls_checked": list(SESSION_CONTENT_CONTROLS)},
        portal_link=model.portal_ca_policy(""),
    )]


def _user_action_unprotected(data: dict[str, Any], action: str, signal_id: str, severity: str,
                             title: str, detail: str) -> list[dict[str, Any]]:
    """Is THIS user action protected, independent of the other one?

    Both Conditional Access user actions live in the `identity_lifecycle` class, and reading the
    class's matrix cell conflates them: a policy that protects security-information registration
    marks the whole class covered, which silenced the device-registration detector as well. The
    two are separately targetable in Entra and separately dangerous, so each is evaluated
    against the policies that actually name it.
    """
    protective = {"mfa", "auth_strength", "block", "phishing_resistant"}
    covering: list[str] = []
    for p in _policies(data):
        if not p.get("is_enforced"):
            continue
        detail_row = (p.get("class_coverage") or {}).get("identity_lifecycle") or {}
        hit = {str(a).lower() for a in detail_row.get("hit") or ()}
        if action in hit and set(p.get("controls") or ()) & protective:
            covering.append(str(p.get("display_name") or p.get("id") or ""))
    if covering:
        return []
    return [model.finding(
        signal_id=signal_id, severity=severity, pillar="ca",
        object_kind="app_class", object_id=f"identity_lifecycle|{action}",
        object_name=_class_label("identity_lifecycle"),
        title=title, detail=detail,
        evidence={"user_action": action, "controls_looked_for": sorted(protective)},
        portal_link=model.portal_ca_policy(""),
    )]


def _security_info_unprotected(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return _user_action_unprotected(
        data, ca_taxonomy.USER_ACTION_REGISTER_SECURITY_INFO,
        "ca.security_info_registration_unprotected", "critical",
        "Registering security information is not protected",
        "Nothing stands between a stolen password and the attacker enrolling their own MFA "
        "method. Once they do, they satisfy every MFA policy in the tenant legitimately.",
    )


def _device_join_unprotected(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    return _user_action_unprotected(
        data, ca_taxonomy.USER_ACTION_REGISTER_DEVICE,
        "ca.device_join_unprotected", "high",
        "Registering or joining devices is not protected",
        "Any account with a valid password can join a device to the tenant. That device then "
        "becomes an identity of its own, and on a tenant that grants access to compliant or "
        "hybrid-joined devices it can become the thing that satisfies your device control.",
    )


def _guest_scope_gap(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    guest_cells = _cells(data, "guests")
    member_cells = _cells(data, "members")
    if not guest_cells:
        return []
    for key, gcell in guest_cells.items():
        mcell = member_cells.get(key) or {}
        if gcell.get("state") == CELL_NA or mcell.get("state") != CELL_COVERED:
            continue
        if gcell.get("state") == CELL_COVERED:
            continue
        cid, _, ctrl = key.partition("|")
        out.append(model.finding(
            signal_id="ca.guest_scope_gap", severity="high", pillar="ca",
            object_kind="app_class", object_id=key, object_name=_class_label(cid),
            title=f"Guests are held to a weaker standard than members for {ctrl} on {_class_label(cid)}",
            detail="Members are fully covered by this control on this application class and guests "
                   "are not. Guest accounts are governed by an organisation you do not control.",
            evidence={"guest_state": gcell.get("state"), "member_state": mcell.get("state"),
                      "guests_uncovered": gcell.get("uncovered_total")},
            portal_link=model.portal_ca_policy(""),
        ))
    return out[:20]


def _management_api_gap(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """The portal is protected but the API underneath it is not."""
    cells = _cells(data)
    out = []
    for ctrl in ("mfa", "auth_strength", "compliant_or_hybrid_device"):
        portal = (cells.get(f"admin_planes|{ctrl}") or {}).get("state")
        api = (cells.get(f"management_apis|{ctrl}") or {}).get("state")
        if portal == CELL_COVERED and api not in (CELL_COVERED, CELL_NA):
            out.append(model.finding(
                signal_id="ca.management_api_gap", severity="critical", pillar="ca",
                object_kind="app_class", object_id=f"management_apis|{ctrl}",
                object_name="Management APIs",
                title=f"Admin portals require {ctrl} but the management APIs behind them do not",
                detail="The portal is a client of these APIs. Protecting the portal while leaving "
                       "the API open means the control is enforced only against people who choose "
                       "to use a browser — the command line is unaffected.",
                evidence={"portal_state": portal, "api_state": api, "control": ctrl},
                portal_link=model.portal_ca_policy(""),
            ))
    return out


def _shadowed_class(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    derived = (_coverage(data).get("derived") or {}).get("shadowed_classes") or {}
    detail_map = derived.get("detail") or {}
    return [model.finding(
        signal_id="ca.shadowed_class", severity="medium", pillar="ca",
        object_kind="app_class", object_id=cid, object_name=_class_label(cid),
        title=f"Every policy covering {_class_label(cid)} is disabled or report-only",
        detail="Policies exist for this application class and not one of them is enforcing. This "
               "reads as covered on a policy list and protects nobody.",
        evidence={"policies": detail_map.get(cid, [])},
        portal_link=model.portal_ca_policy(""),
    ) for cid in derived.get("classes") or []]


def _exclusion_defeats_control(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """A policy targets All cloud apps but excludes an app in a sensitive class."""
    out = []
    for p in _policies(data):
        if not p.get("is_enforced") or not p.get("targets_all_apps"):
            continue
        excluded = {str(a).lower() for a in (p.get("conditions") or {}).get("exclude_apps") or []}
        if not excluded:
            continue
        for cid, detail in (p.get("class_coverage") or {}).items():
            missed = {str(m).lower() for m in detail.get("missed") or ()} & excluded
            if not missed:
                continue
            out.append(model.finding(
                signal_id="ca.exclusion_defeats_control", severity="high", pillar="ca",
                object_kind="policy", object_id=str(p.get("id") or ""),
                object_name=str(p.get("display_name") or ""),
                title=f"'{p.get('display_name')}' targets all cloud apps but excludes "
                      f"{_class_label(cid)} applications",
                detail="A policy scoped to every application with an exclusion carved out of a "
                       "sensitive class protects everything except the thing most worth "
                       "protecting.",
                evidence={"class_id": cid, "excluded": sorted(missed),
                          "controls": p.get("controls")},
                portal_link=model.portal_ca_policy(str(p.get("id") or "")),
            ))
    return out


def _weak_grant_semantics(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """OR across controls where one branch is materially weaker than the other."""
    out = []
    for p in _policies(data):
        if not p.get("is_enforced"):
            continue
        grant = p.get("grant") or {}
        controls = set(p.get("controls") or ())
        if str(grant.get("operator") or "OR").upper() != "OR":
            continue
        if CTRL_BLOCK in controls or len(set(grant.get("controls") or ())) < 2:
            continue
        if "mfa" in controls and "compliant_or_hybrid_device" in controls:
            out.append(model.finding(
                signal_id="ca.weak_grant_semantics", severity="medium", pillar="ca",
                object_kind="policy", object_id=str(p.get("id") or ""),
                object_name=str(p.get("display_name") or ""),
                title=f"'{p.get('display_name')}' accepts the weakest of several controls",
                detail="The grant uses OR, so a user satisfies this policy by meeting any one "
                       "requirement. The effective strength of the policy is its weakest branch, "
                       "not the list of controls shown in the portal.",
                evidence={"operator": "OR", "controls": sorted(controls)},
                portal_link=model.portal_ca_policy(str(p.get("id") or "")),
            ))
    return out


def _unattributed_apps(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    derived = (_coverage(data).get("derived") or {}).get("unattributed_apps") or {}
    if not derived.get("measured"):
        # Not measured is NOT clean. Refusing to emit a finding here is correct; claiming zero
        # would be a lie told with data nobody collected.
        raise SignalUnavailable(str(derived.get("reason") or "Sign-in activity was not collected."))
    return [model.finding(
        signal_id="ca.unattributed_apps", severity="high", pillar="ca",
        object_kind="app", object_id=str(a.get("app_id") or ""), object_name=str(a.get("name") or ""),
        title=f"'{a.get('name')}' is being signed into and no enforced policy covers it",
        detail="This application shows sign-in activity and is matched by no enforced Conditional "
               "Access policy, either because nothing targets it or because it is excluded.",
        evidence={"window_days": derived.get("window_days"), "total": derived.get("total")},
        portal_link=model.portal_app(str(a.get("app_id") or "")),
    ) for a in derived.get("apps") or []]


def _class_population(data: dict[str, Any]) -> int:
    """Denominator for ratio signals: how many app classes the taxonomy resolves here."""
    return len([c for c in ca_taxonomy.classes() if not c.get("derived")])


def _app_population(data: dict[str, Any]) -> int:
    sps = ((data.get("apps") or {}).get("service_principals")) or []
    return len(sps)


def _breakglass_inconsistent(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Confirmed emergency accounts that are excluded from some policies but not others.

    This is a RELIABILITY finding, not a security one, and is tagged as such. A break-glass
    account is supposed to be uniformly excluded so it still works when everything else is
    broken. Excluding it from four policies and forgetting the fifth does not make the tenant
    more secure — it makes the account unreliable in precisely the incident it exists for, and
    that is discovered at the worst possible moment.
    """
    analysis = _analysis(data)
    bg = analysis.get("breakglass") or {}
    confirmed = {str(i) for i in bg.get("confirmed_ids") or []}
    if not confirmed:
        return []

    enforced = [p for p in _policies(data) if p.get("is_enforced")]
    # Only policies that would actually stand in the way are relevant. A policy the account is
    # not in scope of at all is not an inconsistency.
    blocking = [p for p in enforced
                if set(p.get("controls") or ()) & {"mfa", "block", "auth_strength",
                                                   "phishing_resistant",
                                                   "compliant_or_hybrid_device"}]
    if len(blocking) < 2:
        return []

    out = []
    for uid in sorted(confirmed):
        excluded = [p for p in blocking if uid in set(p.get("excluded_ids") or ())]
        caught = [p for p in blocking if uid in set(p.get("effective_ids") or ())]
        if not excluded or not caught:
            continue  # uniformly excluded, or uniformly in scope - both are consistent
        out.append(model.finding(
            signal_id="ca.breakglass_inconsistent", severity="high", pillar="ca",
            object_kind="user", object_id=uid, object_name=_name(data, uid),
            title=f"Break-glass account {_name(data, uid)} is excluded from some policies but not others",
            detail=f"Excluded from {len(excluded)} enforced policy/policies and still in scope of "
                   f"{len(caught)}. An emergency account is only useful if it works when nothing "
                   f"else does; a partial exclusion means it fails exactly when it is needed.",
            evidence={
                "excluded_from": [str(p.get("display_name") or "") for p in excluded][:10],
                "still_in_scope_of": [str(p.get("display_name") or "") for p in caught][:10],
            },
            portal_link=model.portal_user(uid),
        ))
    return out


# ----------------------------------------------------------------------------- specs
def _spec(sid, title, question, why, severity, weight, evaluate, *, object_kind="app_class",
          remediation="", impact=IMPACT_BINARY, tags=(), default_enabled=True,
          population=None) -> SignalSpec:
    return SignalSpec(
        id=sid, title=title, question=question, why=why, pillar="ca", severity=severity,
        weight=weight, object_kind=object_kind, evaluate=evaluate, doc_link=CA_DOC,
        remediation=remediation, impact=impact, tags=tags, default_enabled=default_enabled,
        requires=("ca",), domains=("ca",), population=population,
    )


SPECS: list[SignalSpec] = [
    _spec("ca.class_never_targeted", "Application class never targeted",
          "Is every class of application in scope of at least one enforced policy?",
          "A class no policy names is governed by nothing. This is the gap that a policy list "
          "cannot show you, because the absence of a policy has nothing to display.",
          "high", 8, _class_never_targeted,
          remediation="Create or extend an enforced policy that targets this application class."),
    _spec("ca.bundle_member_divergence", "Bundle does not cover all its members",
          "Does a policy naming the Office 365 bundle reach every application inside it?",
          "Microsoft defines what the bundle contains and changes it without notice. Naming the "
          "bundle is not the same as reaching everything currently inside it.",
          "high", 7, _bundle_member_divergence,
          remediation="Target the specific applications, or confirm the bundle's current membership.",
          impact=IMPACT_RATIO, population=_class_population),
    _spec("ca.dependency_split", "Front-end app protected, its data services are not",
          "Is Teams protected while SharePoint and Exchange are left open?",
          "Teams keeps its files in SharePoint and its messages in Exchange. Controlling Teams "
          "alone leaves the same data reachable one layer down.",
          "high", 8, _dependency_split,
          remediation="Extend the policy to SharePoint Online and Exchange Online."),
    _spec("ca.no_session_control_on_content", "No session control on content services",
          "Is anything governing the session after sign-in to content services?",
          "Authentication decides who gets in. Session controls decide what they can do once "
          "inside, and whether the decision is ever revisited.",
          "medium", 5, _no_session_control_on_content,
          remediation="Apply app-enforced restrictions, CAE, or a sign-in frequency."),
    _spec("ca.security_info_registration_unprotected", "Security info registration unprotected",
          "Is registering MFA methods itself protected?",
          "If an attacker with a stolen password can enrol their own MFA method, they stop "
          "needing to bypass MFA — they satisfy it.",
          "critical", 10, _security_info_unprotected,
          remediation="Target the 'Register security information' user action with MFA or a "
                      "trusted location requirement."),
    _spec("ca.device_join_unprotected", "Device registration unprotected",
          "Is joining a device to the tenant protected?",
          "A device joined by an attacker becomes an identity in its own right, and on a tenant "
          "that trusts compliant or hybrid-joined devices it can become the thing that satisfies "
          "the device control.",
          "high", 8, _device_join_unprotected,
          remediation="Target the 'Register or join devices' user action with MFA or an "
                      "authentication strength. Entra offers no other control here."),
    _spec("ca.breakglass_inconsistent", "Break-glass account excluded inconsistently",
          "Is every confirmed emergency account excluded from every blocking policy?",
          "An emergency account exists to work when everything else is broken. Excluded from "
          "four policies and forgotten in the fifth, it fails in the incident it exists for.",
          "high", 6, _breakglass_inconsistent, object_kind="user",
          remediation="Exclude the account from every enforced blocking policy, or from none, "
                      "and document which.",
          tags=("reliability",)),
    _spec("ca.guest_scope_gap", "Guests held to a weaker standard",
          "Are guests covered wherever members are?",
          "Guest accounts belong to organisations whose security you do not control, and they "
          "very often fall outside policies written with employees in mind.",
          "high", 7, _guest_scope_gap,
          remediation="Include guest and external users in the policy, or write a guest policy."),
    _spec("ca.management_api_gap", "Portal protected, API is not",
          "Are the management APIs held to the same standard as the portals in front of them?",
          "Protecting only the portal enforces the control against people who use a browser. The "
          "CLI, the SDKs and any script reach the same plane without it.",
          "critical", 9, _management_api_gap,
          remediation="Target Windows Azure Service Management API and Microsoft Graph."),
    _spec("ca.shadowed_class", "Class covered only by inactive policies",
          "Is every policy that covers this class disabled or report-only?",
          "A policy list showing coverage for a class where nothing is enforcing is the most "
          "convincing way to believe you are protected when you are not.",
          "medium", 6, _shadowed_class,
          remediation="Enable one of the policies, or accept and document the gap."),
    _spec("ca.exclusion_defeats_control", "Exclusion carves a hole in a sensitive class",
          "Does an all-apps policy exclude something that matters?",
          "An exclusion on an otherwise universal policy is invisible on the coverage summary and "
          "is exactly where an attacker would look.",
          "high", 8, _exclusion_defeats_control, object_kind="policy",
          remediation="Remove the exclusion or replace it with a narrower, documented one."),
    _spec("ca.weak_grant_semantics", "Grant accepts its weakest branch",
          "Does an OR grant let a weak control satisfy a policy meant to be strong?",
          "The portal lists the controls; it does not tell you a user needs only one of them. "
          "The policy is as strong as its weakest branch.",
          "medium", 5, _weak_grant_semantics, object_kind="policy",
          remediation="Change the grant operator to AND, or split the policy."),
    _spec("ca.unattributed_apps", "Application signed into but never covered",
          "Are applications being used that no enforced policy governs?",
          "An application with real sign-in traffic and no policy is a live, exercised gap rather "
          "than a theoretical one.",
          "high", 8, _unattributed_apps, object_kind="app",
          remediation="Bring the application into an existing policy's scope.",
          impact=IMPACT_RATIO, population=_app_population),
]
