"""Governance pillar — access reviews, entitlement management and lifecycle workflows.

The design rule that shapes every signal here: **absence is the finding**.

Listing the access reviews a tenant has is a portal feature. Naming the privileged roles,
role-assignable groups and guest population that *no review has ever looked at* is the
product feature — and it is computable from the inventory alone, which is why most of these
signals still fire on a tenant with no P2 license at all. A free-tier tenant learning that
18 privileged roles have never been reviewed is a better outcome than an empty screen
saying "requires Entra ID P2".
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
)

REVIEW_DOC = "https://learn.microsoft.com/entra/id-governance/access-reviews-overview"
ENTITLEMENT_DOC = "https://learn.microsoft.com/entra/id-governance/entitlement-management-overview"
LIFECYCLE_DOC = "https://learn.microsoft.com/entra/id-governance/what-are-lifecycle-workflows"


def _gov(data: dict[str, Any]) -> dict[str, Any]:
    return domain(data, "governance")


def _caps(data: dict[str, Any]) -> dict[str, Any]:
    value = _gov(data).get("capabilities")
    return value if isinstance(value, dict) else {}


def _coverage_row(data: dict[str, Any], key: str) -> dict[str, Any]:
    from app.entra.collectors.governance import coverage

    return next((r for r in coverage(data) if r["key"] == key), {})


# ------------------------------------------------------------------- coverage gaps
def _unreviewed(key: str, signal_id: str, severity: str, noun: str, needs: tuple[str, ...]):
    """Factory for the 'this object class has never been reviewed' family.

    Deliberately computed from the inventory domains, so it reports on a tenant with no
    governance license — where the honest answer is "never reviewed", not "not measured"."""

    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        for need in needs:
            if not domain(data, need):
                raise SignalUnavailable(f"The {need} inventory was not collected.")
        row = _coverage_row(data, key)
        gap = int(row.get("gap") or 0)
        if gap <= 0:
            return []
        reviews_known = bool(_caps(data).get("access_reviews"))
        qualifier = ("" if reviews_known else
                     " Access reviews could not be read for this tenant, so this counts every "
                     f"{noun} as unreviewed — which is the correct assumption when no review "
                     "data exists.")
        return [model.finding(
            signal_id=signal_id, severity=severity, pillar="gov",
            object_kind="tenant", object_id=key, object_name=row.get("label") or key,
            title=f"{gap} {noun}(s) are covered by no access review",
            detail=str(row.get("why") or "") + qualifier,
            evidence={"total": row.get("count"), "reviewed": row.get("reviewed"),
                      "governed_by_package": row.get("governed"), "gap": gap,
                      "examples": (row.get("objects") or [])[:15],
                      "access_reviews_readable": reviews_known},
            discriminator=key,
        )]

    return _inner


# --------------------------------------------------------------------- review quality
def _review_overdue(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("access_reviews"):
        raise SignalUnavailable("Access reviews were not collected.")
    out = []
    for review in _gov(data).get("reviews") or []:
        for instance in review.get("instances") or []:
            end = str(instance.get("end") or "")
            if not end or str(instance.get("status") or "") in ("Completed", "Applied"):
                continue
            days = ctx.days_since(end)
            if days is None or days <= 0:
                continue
            out.append(model.finding(
                signal_id="gov.review_overdue", severity="high", pillar="gov",
                object_kind="review", object_id=str(instance.get("id") or review.get("id") or ""),
                object_name=str(review.get("display_name") or ""),
                title=f"Access review '{review.get('display_name')}' is {days} day(s) overdue",
                detail="The review window closed and decisions are still outstanding. Access that "
                       "nobody re-justified is still in place, and the campaign has produced "
                       "nothing but a false sense of assurance.",
                evidence={"instance_status": instance.get("status"), "ended": end,
                          "days_overdue": days, "auto_apply": review.get("auto_apply"),
                          "scope": review.get("scope")},
                discriminator=str(instance.get("id") or ""),
            ))
    return out


def _review_no_auto_apply(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("access_reviews"):
        raise SignalUnavailable("Access reviews were not collected.")
    return [model.finding(
        signal_id="gov.review_no_auto_apply", severity="medium", pillar="gov",
        object_kind="review", object_id=str(r.get("id") or ""),
        object_name=str(r.get("display_name") or ""),
        title=f"Access review '{r.get('display_name')}' does not apply its own decisions",
        detail="Reviewers are denying access that is never actually removed. Somebody has to "
               "action the results by hand, and in practice nobody does — which makes the "
               "campaign a reporting exercise rather than a control.",
        evidence={"auto_apply": False, "status": r.get("status"),
                  "recurrence": r.get("recurrence"), "scope": r.get("scope")},
    ) for r in _gov(data).get("reviews") or [] if not r.get("auto_apply")]


def _review_default_approve(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("access_reviews"):
        raise SignalUnavailable("Access reviews were not collected.")
    return [model.finding(
        signal_id="gov.review_default_approve", severity="high", pillar="gov",
        object_kind="review", object_id=str(r.get("id") or ""),
        object_name=str(r.get("display_name") or ""),
        title=f"Access review '{r.get('display_name')}' approves anything nobody decides",
        detail="When reviewers do not respond, this campaign keeps the access. A review whose "
               "default outcome is 'approve' cannot remove anything, so it can only ever "
               "confirm the status quo.",
        evidence={"default_decision": r.get("default_decision"),
                  "default_decision_enabled": True, "scope": r.get("scope")},
    ) for r in _gov(data).get("reviews") or []
        if r.get("default_decision_enabled") and str(r.get("default_decision")) == "Approve"]


def _review_one_off_only(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("access_reviews"):
        raise SignalUnavailable("Access reviews were not collected.")
    return [model.finding(
        signal_id="gov.review_not_recurring", severity="medium", pillar="gov",
        object_kind="review", object_id=str(r.get("id") or ""),
        object_name=str(r.get("display_name") or ""),
        title=f"Access review '{r.get('display_name')}' runs once and never again",
        detail="Access drifts continuously; a single review is a point-in-time snapshot that is "
               "wrong the following week. Recurrence is what turns a review into a control.",
        evidence={"recurrence": r.get("recurrence"), "created_at": r.get("created_at"),
                  "scope": r.get("scope")},
    ) for r in _gov(data).get("reviews") or [] if str(r.get("recurrence")) == "one-off"]


# ------------------------------------------------------------------- entitlement
def _entitlement_no_review(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("entitlement"):
        raise SignalUnavailable("Entitlement management was not collected.")
    out = []
    for pkg in _gov(data).get("packages") or []:
        policies = pkg.get("policies") or []
        if policies and any(p.get("review_required") for p in policies):
            continue
        out.append(model.finding(
            signal_id="gov.entitlement_no_review", severity="medium", pillar="gov",
            object_kind="package", object_id=str(pkg.get("id") or ""),
            object_name=str(pkg.get("display_name") or ""),
            title=f"Access package '{pkg.get('display_name')}' has no recurring review",
            detail="Entitlement management grants this access on request. Without a review, "
                   "the grant is permanent by default and the package quietly accumulates "
                   "members who no longer need it.",
            evidence={"policy_count": len(policies), "resource_scopes": pkg.get("resource_scopes"),
                      "catalog_id": pkg.get("catalog_id")},
        ))
    return out


def _entitlement_no_expiry(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("entitlement"):
        raise SignalUnavailable("Entitlement management was not collected.")
    out = []
    for pkg in _gov(data).get("packages") or []:
        policies = pkg.get("policies") or []
        offenders = [p for p in policies if not p.get("expires")]
        if not offenders:
            continue
        out.append(model.finding(
            signal_id="gov.entitlement_no_expiry", severity="medium", pillar="gov",
            object_kind="package", object_id=str(pkg.get("id") or ""),
            object_name=str(pkg.get("display_name") or ""),
            title=f"Access package '{pkg.get('display_name')}' grants access that never expires",
            detail="An assignment policy with no expiry turns a request-based grant into a "
                   "permanent one. The request workflow gave the appearance of governance; the "
                   "missing expiry removed it.",
            evidence={"policies_without_expiry": [p.get("display_name") for p in offenders],
                      "policy_count": len(policies)},
        ))
    return out


def _entitlement_expiring(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("entitlement"):
        raise SignalUnavailable("Entitlement management was not collected.")
    out = []
    for row in _gov(data).get("assignments") or []:
        days = ctx.days_until(str(row.get("expires_at") or ""))
        if days is None or days < 0 or days > 14:
            continue
        out.append(model.finding(
            signal_id="gov.entitlement_expiring", severity="low", pillar="gov",
            object_kind="package", object_id=str(row.get("id") or ""),
            object_name=f"{row.get('principal_name')} \u2192 {row.get('package_name')}",
            title=f"Access package assignment expires in {days} day(s)",
            detail="Advance notice so the loss of access is planned rather than discovered. "
                   "Expiring entitlement assignments are the intended behavior — this is a "
                   "heads-up, not a fault.",
            evidence={"package": row.get("package_name"), "principal": row.get("principal_name"),
                      "principal_type": row.get("principal_type"),
                      "expires_at": row.get("expires_at"), "days_left": days},
        ))
    return out


def _direct_assignment_bypass(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Where governance is being routed around."""
    if not _caps(data).get("entitlement"):
        raise SignalUnavailable("Entitlement management was not collected.")
    assignments = _gov(data).get("assignments") or []
    if not assignments:
        return []
    governed = {str(a.get("principal_id") or "") for a in assignments if a.get("principal_id")}
    guests = [u for u in domain(data, "people").get("users") or []
              if str(u.get("user_type")) == "Guest" and u.get("enabled")]
    if not guests:
        return []
    direct = [g for g in guests if str(g.get("id") or "") not in governed]
    if not direct or len(direct) == len(guests):
        # Either nothing is governed (a different finding entirely) or everything is.
        return []
    return [model.finding(
        signal_id="gov.direct_assignment_bypass", severity="medium", pillar="gov",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"{len(direct)} guest(s) hold access outside entitlement management",
        detail="Entitlement management is in use for guests, but these accounts were invited and "
               "granted access directly instead. The governed path exists and is being bypassed, "
               "which is worse than not having it: the reporting says guests are governed.",
        evidence={"guests_total": len(guests), "guests_governed": len(guests) - len(direct),
                  "guests_direct": len(direct),
                  "examples": [g.get("upn") for g in direct][:15]},
    )]


# --------------------------------------------------------------------- lifecycle
def _no_leaver_workflow(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("lifecycle"):
        raise SignalUnavailable("Lifecycle workflows were not collected (requires Entra ID "
                                "Governance).")
    workflows = _gov(data).get("workflows") or []
    if any(w.get("category") == "leaver" and w.get("enabled") for w in workflows):
        return []
    return [model.finding(
        signal_id="gov.no_leaver_workflow", severity="high", pillar="gov",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="No enabled leaver workflow exists",
        detail="Offboarding is manual. Every departure depends on somebody remembering every "
               "system, and the evidence for how often that fails is the stale-account list on "
               "the Users screen.",
        evidence={"workflows": len(workflows),
                  "categories": sorted({str(w.get("category")) for w in workflows}),
                  "enabled": sum(1 for w in workflows if w.get("enabled"))},
    )]


def _lifecycle_workflow_failing(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    if not _caps(data).get("lifecycle"):
        raise SignalUnavailable("Lifecycle workflows were not collected (requires Entra ID "
                                "Governance).")
    out = []
    for workflow in _gov(data).get("workflows") or []:
        runs = workflow.get("runs") or {}
        failed = int(runs.get("failed") or 0)
        if not failed:
            continue
        out.append(model.finding(
            signal_id="gov.lifecycle_workflow_failed", severity="high", pillar="gov",
            object_kind="workflow", object_id=str(workflow.get("id") or ""),
            object_name=str(workflow.get("display_name") or ""),
            title=f"Lifecycle workflow '{workflow.get('display_name')}' has {failed} failed run(s)",
            detail="A configured workflow that does not complete is worse than no workflow, "
                   "because the organization believes offboarding is automated while access "
                   "quietly survives.",
            evidence={"category": workflow.get("category"), "runs": runs,
                      "enabled": workflow.get("enabled"),
                      "tasks": workflow.get("task_count")},
        ))
    return out


def _leaver_workflow_ineffective(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """The join that matters: a leaver workflow exists, and leavers still hold access."""
    if not _caps(data).get("lifecycle"):
        raise SignalUnavailable("Lifecycle workflows were not collected (requires Entra ID "
                                "Governance).")
    workflows = _gov(data).get("workflows") or []
    if not any(w.get("category") == "leaver" and w.get("enabled") for w in workflows):
        return []           # covered by gov.no_leaver_workflow instead
    # "Retains access" means exactly what the People pillar means by it: a disabled account
    # that still holds a directory role or a license.
    roles = domain(data, "roles")
    holders: set[str] = set()
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles.get(bucket) or []:
            if row.get("principal_id"):
                holders.add(str(row["principal_id"]))
    survivors = [
        u for u in domain(data, "people").get("users") or []
        if not u.get("enabled") and (str(u.get("id") or "") in holders or u.get("licence_count"))
    ]
    if not survivors:
        return []
    return [model.finding(
        signal_id="gov.leaver_workflow_ineffective", severity="high", pillar="gov",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"A leaver workflow is enabled but {len(survivors)} disabled account(s) retain access",
        detail="The workflow runs and the access survives. False assurance is the most expensive "
               "kind of governance failure: nobody looks, because the control is green.",
        evidence={"disabled_with_access": len(survivors),
                  "examples": [u.get("upn") for u in survivors][:15],
                  "leaver_workflows": [w.get("display_name") for w in workflows
                                       if w.get("category") == "leaver"]},
    )]


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="gov.privileged_roles_unreviewed", title="Privileged roles have never been reviewed",
        question="Which privileged directory roles are covered by no access review?",
        why="Standing privilege that nobody re-justifies is how an emergency grant from three "
            "years ago becomes permanent. This is computable without a governance license.",
        pillar="gov", severity="high", weight=9, object_kind="tenant",
        domains=("roles",), impact=IMPACT_BINARY,
        remediation="Create a recurring access review over the privileged directory roles.",
        remediation_steps=(
            "Create an access review scoped to privileged directory roles.",
            "Set it to recur quarterly with auto-apply enabled.",
            "Set the default decision to Deny so inaction removes access rather than keeping it.",
        ),
        doc_link=REVIEW_DOC,
        evaluate=_unreviewed("privileged_roles", "gov.privileged_roles_unreviewed", "high",
                             "privileged role", ("roles",)),
        tags=("coverage", "privileged"),
    ),
    SignalSpec(
        id="gov.assignable_groups_unreviewed",
        title="Role-assignable groups have never been reviewed",
        question="Which role-assignable groups are covered by no access review?",
        why="Membership of a role-assignable group is a directory role in disguise. Reviewing the "
            "role but not the group that confers it reviews nothing.",
        pillar="gov", severity="high", weight=8, object_kind="tenant",
        domains=("people",), impact=IMPACT_BINARY,
        remediation="Add the role-assignable groups to a recurring access review.",
        doc_link=REVIEW_DOC,
        evaluate=_unreviewed("role_assignable_groups", "gov.assignable_groups_unreviewed", "high",
                             "role-assignable group", ("people",)),
        tags=("coverage", "privileged"),
    ),
    SignalSpec(
        id="gov.guests_unreviewed", title="Guest accounts have never been reviewed",
        question="Are external identities covered by a recurring review or an access package?",
        why="Guests only ever accumulate. Without a review, external access to the tenant grows "
            "monotonically and nobody owns the decision to remove it.",
        pillar="gov", severity="medium", weight=8, object_kind="tenant",
        domains=("people",), impact=IMPACT_BINARY,
        remediation="Create a recurring guest access review, or bring guests into entitlement "
                    "management where expiry is built in.",
        doc_link=REVIEW_DOC,
        evaluate=_unreviewed("guests", "gov.guests_unreviewed", "medium", "guest", ("people",)),
        tags=("coverage", "guest"),
    ),
    SignalSpec(
        id="gov.high_privilege_apps_unreviewed",
        title="High-privilege applications have never been reviewed",
        question="Which applications holding critical Graph permissions are covered by no review?",
        why="Consent is granted once and examined never. An application holding Mail.ReadWrite "
            "tenant-wide deserves the same periodic justification as a human administrator.",
        pillar="gov", severity="medium", weight=7, object_kind="tenant",
        domains=("apps",), impact=IMPACT_BINARY,
        remediation="Review the high-privilege application list and remove permissions no longer "
                    "needed; add the applications to a recurring review.",
        doc_link=REVIEW_DOC,
        evaluate=_unreviewed("high_privilege_apps", "gov.high_privilege_apps_unreviewed",
                             "medium", "high-privilege application", ("apps",)),
        tags=("coverage", "app"),
    ),
    SignalSpec(
        id="gov.review_overdue", title="Access reviews are overdue",
        question="Are there review instances past their end date with decisions outstanding?",
        why="An overdue review is access that nobody re-justified, sitting behind a control "
            "everyone believes is working.",
        pillar="gov", severity="high", weight=9, object_kind="review",
        domains=("governance",), requires=("AccessReview.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Chase the reviewers, or enable auto-apply with a Deny default so inaction "
                    "removes access instead of preserving it.",
        doc_link=REVIEW_DOC, evaluate=_review_overdue, tags=("review-quality",),
    ),
    SignalSpec(
        id="gov.review_no_auto_apply", title="Access review decisions are never applied",
        question="Do the reviews actually remove the access reviewers denied?",
        why="Without auto-apply, a denial is a note in a report. The access stays until somebody "
            "does it by hand, which in practice means it stays.",
        pillar="gov", severity="medium", weight=7, object_kind="review",
        domains=("governance",), requires=("AccessReview.Read.All",), licence="p2",
        impact=IMPACT_RATIO,
        population=lambda d: len((d.get("governance") or {}).get("reviews") or []),
        remediation="Enable 'Auto apply results to resource' on each review.",
        doc_link=REVIEW_DOC, evaluate=_review_no_auto_apply, tags=("review-quality",),
    ),
    SignalSpec(
        id="gov.review_default_approve",
        title="Access reviews approve by default when nobody responds",
        question="What happens to access when a reviewer ignores the campaign?",
        why="A review whose default decision is Approve can only ever confirm the status quo. It "
            "produces evidence of governance without performing any.",
        pillar="gov", severity="high", weight=8, object_kind="review",
        domains=("governance",), requires=("AccessReview.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Set the default decision to Deny (or Take recommendations) so inaction "
                    "removes access.",
        doc_link=REVIEW_DOC, evaluate=_review_default_approve, tags=("review-quality",),
    ),
    SignalSpec(
        id="gov.review_not_recurring", title="Access reviews run once and never again",
        question="Are the reviews recurring, or one-off campaigns?",
        why="Access drifts continuously. A one-off review is accurate on the day it closes and "
            "progressively wrong from then on.",
        pillar="gov", severity="medium", weight=5, object_kind="review",
        domains=("governance",), requires=("AccessReview.Read.All",), licence="p2",
        impact=IMPACT_RATIO,
        population=lambda d: len((d.get("governance") or {}).get("reviews") or []),
        remediation="Convert one-off reviews to a quarterly or monthly recurrence.",
        doc_link=REVIEW_DOC, evaluate=_review_one_off_only, tags=("review-quality",),
    ),
    SignalSpec(
        id="gov.entitlement_no_review", title="Access packages have no recurring review",
        question="Is package membership ever re-justified?",
        why="A request-based grant with no review is a permanent grant with extra paperwork.",
        pillar="gov", severity="medium", weight=6, object_kind="package",
        domains=("governance",), requires=("EntitlementManagement.Read.All",), licence="p2",
        impact=IMPACT_RATIO,
        population=lambda d: len((d.get("governance") or {}).get("packages") or []),
        remediation="Enable access reviews on each assignment policy.",
        doc_link=ENTITLEMENT_DOC, evaluate=_entitlement_no_review, tags=("entitlement",),
    ),
    SignalSpec(
        id="gov.entitlement_no_expiry", title="Access packages grant access that never expires",
        question="Do assignment policies set an expiry?",
        why="Expiry is the mechanism that makes entitlement management self-cleaning. Without it "
            "the catalogue only grows.",
        pillar="gov", severity="medium", weight=6, object_kind="package",
        domains=("governance",), requires=("EntitlementManagement.Read.All",), licence="p2",
        impact=IMPACT_RATIO,
        population=lambda d: len((d.get("governance") or {}).get("packages") or []),
        remediation="Set an expiry duration on every assignment policy.",
        doc_link=ENTITLEMENT_DOC, evaluate=_entitlement_no_expiry, tags=("entitlement",),
    ),
    SignalSpec(
        id="gov.entitlement_expiring", title="Access package assignments expiring soon",
        question="Whose package access lapses in the next two weeks?",
        why="Expiry working as designed still surprises people. Advance notice turns a support "
            "ticket into a planned renewal.",
        pillar="gov", severity="low", weight=3, object_kind="package",
        domains=("governance",), requires=("EntitlementManagement.Read.All",), licence="p2",
        impact=IMPACT_SATURATING, saturation=10,
        remediation="Renew or let lapse deliberately; notify the assignees either way.",
        doc_link=ENTITLEMENT_DOC, evaluate=_entitlement_expiring, tags=("entitlement",),
    ),
    SignalSpec(
        id="gov.direct_assignment_bypass", title="Governed access is being granted directly",
        question="Is entitlement management being routed around?",
        why="A governed path that people bypass is worse than no governed path, because the "
            "reporting claims coverage the tenant does not have.",
        pillar="gov", severity="medium", weight=6, object_kind="tenant",
        domains=("governance", "people"), requires=("EntitlementManagement.Read.All",),
        licence="p2", impact=IMPACT_BINARY,
        remediation="Route guest access through the access package and remove the direct grants.",
        doc_link=ENTITLEMENT_DOC, evaluate=_direct_assignment_bypass, tags=("entitlement",),
    ),
    SignalSpec(
        id="gov.no_leaver_workflow", title="No leaver workflow is configured",
        question="Is offboarding automated?",
        why="Manual offboarding fails silently and the evidence is the stale-account list. A "
            "leaver workflow is the only thing that makes departure deterministic.",
        pillar="gov", severity="high", weight=9, object_kind="tenant",
        domains=("governance",), requires=("LifecycleWorkflows.Read.All",), licence="governance",
        impact=IMPACT_BINARY,
        remediation="Create and enable a leaver workflow triggered on employeeLeaveDateTime.",
        remediation_steps=(
            "Create a leaver workflow in Identity Governance \u2192 Lifecycle workflows.",
            "Add tasks: disable account, remove group memberships, revoke sessions.",
            "Enable scheduling so it runs without manual intervention.",
        ),
        doc_link=LIFECYCLE_DOC, evaluate=_no_leaver_workflow, tags=("lifecycle",),
    ),
    SignalSpec(
        id="gov.lifecycle_workflow_failed", title="Lifecycle workflow runs are failing",
        question="Do the configured workflows actually complete?",
        why="A failing workflow creates false assurance: the control looks configured, so nobody "
            "checks whether access was really removed.",
        pillar="gov", severity="high", weight=8, object_kind="workflow",
        domains=("governance",), requires=("LifecycleWorkflows.Read.All",), licence="governance",
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Open the workflow's run history, read the failing task, and fix the "
                    "underlying permission or attribute problem.",
        doc_link=LIFECYCLE_DOC, evaluate=_lifecycle_workflow_failing, tags=("lifecycle",),
    ),
    SignalSpec(
        id="gov.leaver_workflow_ineffective",
        title="A leaver workflow exists but leavers retain access",
        question="Does the offboarding automation actually remove access?",
        why="This is the join that matters. A workflow that runs green while disabled accounts "
            "keep their role assignments is the most expensive kind of governance failure.",
        pillar="gov", severity="high", weight=10, object_kind="tenant",
        domains=("governance", "people", "roles"), requires=("LifecycleWorkflows.Read.All",),
        licence="governance", impact=IMPACT_BINARY,
        remediation="Extend the leaver workflow to remove directory role assignments and group "
                    "memberships, not just to disable the account.",
        doc_link=LIFECYCLE_DOC, evaluate=_leaver_workflow_ineffective, tags=("lifecycle",),
    ),
]
