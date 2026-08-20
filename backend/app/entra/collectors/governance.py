"""Identity Governance collector — access reviews, entitlement management, lifecycle workflows.

Entirely new ground: the repository had zero ``identityGovernance`` coverage before this.

The product's job here is emphatically **not** to list campaigns — the portal already does
that. It is to compute what is *not* governed. A tenant with 40 immaculate access reviews
and 18 privileged roles that nobody has ever reviewed has a governance problem the portal
will never show it, because the portal only draws what exists.

That is why :func:`coverage` is the centerpiece and why it is deliberately computed from
the *inventory* domains rather than from the governance data. On a tenant with no P2 at
all, every row still renders — framed as "never reviewed" rather than "review overdue" —
so a free-tier tenant still learns that 24 privileged roles have never been looked at.
"""
from __future__ import annotations

import re
from typing import Any

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, clip, guarded
from app.entra.collectors.roles import _is_licence_error
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "governance"

_MAX_DEFINITIONS = 2_000
_MAX_INSTANCES_PER_DEFINITION = 12
_MAX_PACKAGES = 2_000
_MAX_ASSIGNMENTS = 20_000


def _gov_note(feature: str, exc: GraphError, scope: str, licence: str) -> str:
    """One sentence naming the ACTUAL blocker.

    Graph answers a missing Entra ID Governance license with a **403** here, which is also
    what a genuine consent failure looks like, so the exception type alone cannot be
    trusted. Checking the message first stops the banner telling an operator to grant
    ``LifecycleWorkflows.Read.All`` when they already hold it and the tenant simply is not
    licensed — advice they cannot act on, and the fastest way to lose their trust in every
    other line on the page.
    """
    if _is_licence_error(exc):
        return f"{feature} unavailable: {licence}"
    if isinstance(exc, GraphPermissionError):
        return f"{feature} not permitted (needs {scope}): {clip(exc.message, 110)}"
    return f"{feature}: {clip(exc, 150)}"
_MAX_WORKFLOWS = 500
_MAX_RUNS_PER_WORKFLOW = 20


# Access-review scopes are OData query strings, not typed objects, so the only way to know
# what a review covers is to read the query. Both shapes below are copied verbatim from a
# live tenant.
#   group:          /v1.0/groups/<id>/transitiveMembers/microsoft.graph.user
#   access package: /v1.0/identityGovernance/entitlementManagement/assignments
#                   ?$filter=(accessPackage/id eq '<id>' and assignmentPolicy/id eq '<id>')
_GROUP_IN_QUERY = re.compile(r"/groups/([0-9a-fA-F-]{36})")
_PACKAGE_IN_QUERY = re.compile(r"accessPackage/id\s+eq\s+'([0-9a-fA-F-]{36})'")


def _scope_summary(scope: Any) -> dict[str, Any]:
    """Reduce an access-review scope union to something a grid can render.

    Getting this wrong is silent and expensive: an unrecognized scope makes the review
    invisible to the coverage join, so a tenant that reviews its groups every month still
    reads "0 reviewed" against every object class. Two live bugs came from here — access
    package reviews were not matched at all, and the group branch took the last path
    segment as the target, which is the literal string "microsoft.graph.user".
    """
    node = as_dict(scope)
    odata = str(node.get("@odata.type") or "")
    query = str(node.get("query") or "")
    package = _PACKAGE_IN_QUERY.search(query)
    group = _GROUP_IN_QUERY.search(query)
    kind = "unknown"
    target = ""
    if "principalResourceMembership" in odata:
        kind = "role"
        target = query
    elif package:
        kind = "access_package"
        target = package.group(1)
    elif group:
        kind = "group"
        target = group.group(1)
    elif "/servicePrincipals" in query:
        kind = "application"
    elif "guest" in query.lower() or "userType" in query:
        kind = "guests"
    return {"kind": kind, "target": target, "query": query[:300]}


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        caps = {
            "access_reviews": False, "entitlement": False, "lifecycle": False,
            "licensed_p2": True, "licensed_governance": True,
        }

        # --- access reviews --------------------------------------------------------
        reviews: list[dict[str, Any]] = []
        await ctx.say("info", "Governance: reading access review definitions\u2026")
        try:
            rows, trunc = await client.get_all(
                "/identityGovernance/accessReviews/definitions",
                top=0, max_items=_MAX_DEFINITIONS,
            )
            caps["access_reviews"] = True
            for raw in rows:
                row = as_dict(raw)
                settings = as_dict(row.get("settings"))
                recurrence = as_dict(as_dict(settings.get("recurrence")).get("pattern"))
                reviewers = as_list(row.get("reviewers"))
                reviews.append({
                    "id": str(row.get("id") or ""),
                    "display_name": str(row.get("displayName") or ""),
                    "status": str(row.get("status") or ""),
                    "created_at": str(row.get("createdDateTime") or ""),
                    "last_modified": str(row.get("lastModifiedDateTime") or ""),
                    "scope": _scope_summary(row.get("scope")),
                    "reviewer_count": len(reviewers),
                    "self_review": any(
                        "managers" not in str(as_dict(r).get("query") or "")
                        and "/me" in str(as_dict(r).get("query") or "") for r in reviewers),
                    "recurrence": str(recurrence.get("type") or "one-off"),
                    "auto_apply": bool(settings.get("autoApplyDecisionsEnabled")),
                    "default_decision": str(settings.get("defaultDecision") or ""),
                    "default_decision_enabled": bool(settings.get("defaultDecisionEnabled")),
                    "justification_required": bool(settings.get("justificationRequiredOnApproval")),
                    "instances": [],
                })
            if trunc:
                notes.append(f"Access review definitions were capped at {_MAX_DEFINITIONS:,}.")
            await ctx.say("ok", f"Governance: {len(reviews)} access review definition(s)")
        except GraphPermissionError as exc:
            notes.append(_gov_note(
                "Access reviews", exc, "AccessReview.Read.All",
                "this tenant is not licensed for Entra ID P2 / ID Governance."))
            if _is_licence_error(exc):
                caps["licensed_p2"] = False
        except GraphError as exc:
            if _is_licence_error(exc):
                caps["licensed_p2"] = False
                notes.append("Access reviews require Entra ID P2 or Entra ID Governance.")
            else:
                notes.append(f"Access reviews: {clip(exc, 150)}")

        # Instances carry the decision counts, which is where "overdue" and
        # "rubber-stamped" actually come from. Fetched only for active definitions.
        if reviews:
            live = [r for r in reviews if r["status"] not in ("Completed", "Applied")][:200]
            for review in live:
                try:
                    rows, _ = await client.get_all(
                        f"/identityGovernance/accessReviews/definitions/{review['id']}/instances",
                        top=0, max_items=_MAX_INSTANCES_PER_DEFINITION,
                    )
                except GraphError:
                    continue
                for raw in rows:
                    row = as_dict(raw)
                    review["instances"].append({
                        "id": str(row.get("id") or ""),
                        "status": str(row.get("status") or ""),
                        "start": str(row.get("startDateTime") or ""),
                        "end": str(row.get("endDateTime") or ""),
                    })

        # --- entitlement management --------------------------------------------------
        packages: list[dict[str, Any]] = []
        assignments: list[dict[str, Any]] = []
        try:
            rows, trunc = await client.get_all(
                "/identityGovernance/entitlementManagement/accessPackages",
                # v1.0 calls this `resourceRoleScopes`. The older beta name
                # `accessPackageResourceRoleScopes` 400s the whole query and costs the
                # entire access-package inventory. Verified against a live tenant.
                expand="resourceRoleScopes",
                top=0, max_items=_MAX_PACKAGES,
            )
            caps["entitlement"] = True
            for raw in rows:
                row = as_dict(raw)
                packages.append({
                    "id": str(row.get("id") or ""),
                    "display_name": str(row.get("displayName") or ""),
                    "description": str(row.get("description") or "")[:300],
                    "catalog_id": str(row.get("catalogId") or ""),
                    "hidden": bool(row.get("isHidden")),
                    "created_at": str(row.get("createdDateTime") or ""),
                    "resource_scopes": len(as_list(row.get("resourceRoleScopes"))),
                    "policies": [],
                })
            if trunc:
                notes.append(f"Access packages were capped at {_MAX_PACKAGES:,}.")
            await ctx.say("ok", f"Governance: {len(packages)} access package(s)")
        except GraphPermissionError as exc:
            notes.append(_gov_note(
                "Entitlement management", exc, "EntitlementManagement.Read.All",
                "this tenant is not licensed for Entra ID P2 / ID Governance."))
            if _is_licence_error(exc):
                caps["licensed_p2"] = False
        except GraphError as exc:
            if _is_licence_error(exc):
                caps["licensed_p2"] = False
                notes.append("Entitlement management requires Entra ID P2 or Entra ID Governance.")
            else:
                notes.append(f"Entitlement management: {clip(exc, 150)}")

        if caps["entitlement"]:
            by_package = {p["id"]: p for p in packages}
            try:
                rows, _ = await client.get_all(
                    "/identityGovernance/entitlementManagement/assignmentPolicies",
                    # v1.0 assignmentPolicies carry NO accessPackageId property and do not
                    # return the accessPackage navigation unless it is expanded. Without
                    # this every policy failed to join and every package showed "0 policies".
                    expand="accessPackage($select=id)",
                    top=0, max_items=_MAX_PACKAGES,
                )
                for raw in rows:
                    row = as_dict(raw)
                    pkg = by_package.get(str(as_dict(row.get("accessPackage")).get("id")
                                             or row.get("accessPackageId") or ""))
                    expiration = as_dict(row.get("expiration"))
                    approval = as_dict(row.get("requestApprovalSettings"))
                    requestors = as_dict(row.get("requestorSettings"))
                    policy = {
                        "id": str(row.get("id") or ""),
                        "display_name": str(row.get("displayName") or ""),
                        # v1.0 moved the requestor scope to the top level. Reading
                        # requestorSettings.scopeType (the beta name) reported every policy
                        # as "unknown".
                        "allowed_targets": str(row.get("allowedTargetScope")
                                               or requestors.get("scopeType") or "unknown"),
                        "approval_required": bool(approval.get("isApprovalRequired")
                                                  or approval.get("isApprovalRequiredForAdd")),
                        # v1.0 calls this reviewSettings, not accessReviewSettings. Reading
                        # the wrong key made every access package show "no review" even
                        # when quarterly reviews were configured and running.
                        "review_required": bool(
                            as_dict(row.get("reviewSettings")).get("isEnabled")
                            or as_dict(row.get("accessReviewSettings")).get("isEnabled")),
                        "expires": bool(expiration.get("endDateTime") or expiration.get("duration")),
                    }
                    if pkg is not None:
                        pkg["policies"].append(policy)
            except GraphError as exc:
                notes.append(f"Assignment policies: {clip(exc, 120)}")

            try:
                rows, trunc = await client.get_all(
                    "/identityGovernance/entitlementManagement/assignments",
                    expand="target,accessPackage",
                    top=0, max_items=_MAX_ASSIGNMENTS,
                )
                for raw in rows:
                    row = as_dict(raw)
                    target = as_dict(row.get("target"))
                    schedule = as_dict(row.get("schedule"))
                    expiration = as_dict(schedule.get("expiration"))
                    assignments.append({
                        "id": str(row.get("id") or ""),
                        "package_id": str(as_dict(row.get("accessPackage")).get("id") or ""),
                        "package_name": str(as_dict(row.get("accessPackage")).get("displayName") or ""),
                        "principal_id": str(target.get("objectId") or ""),
                        "principal_name": str(target.get("displayName") or ""),
                        "principal_type": str(target.get("subjectType") or ""),
                        "state": str(row.get("state") or ""),
                        "expires_at": str(expiration.get("endDateTime") or ""),
                    })
                if trunc:
                    notes.append(f"Access package assignments were capped at {_MAX_ASSIGNMENTS:,}.")
            except GraphError as exc:
                notes.append(f"Access package assignments: {clip(exc, 120)}")

        # --- lifecycle workflows -----------------------------------------------------
        workflows: list[dict[str, Any]] = []
        try:
            rows, _ = await client.get_all(
                "/identityGovernance/lifecycleWorkflows/workflows",
                top=0, max_items=_MAX_WORKFLOWS,
            )
            caps["lifecycle"] = True
            for raw in rows:
                row = as_dict(raw)
                workflows.append({
                    "id": str(row.get("id") or ""),
                    "display_name": str(row.get("displayName") or ""),
                    "category": str(row.get("category") or ""),
                    "enabled": bool(row.get("isEnabled")),
                    "scheduling_enabled": bool(row.get("isSchedulingEnabled")),
                    "task_count": len(as_list(row.get("tasks"))),
                    "last_modified": str(row.get("lastModifiedDateTime") or ""),
                    "runs": {"total": 0, "failed": 0, "successful": 0},
                })
            await ctx.say("ok", f"Governance: {len(workflows)} lifecycle workflow(s)")
        except GraphPermissionError as exc:
            notes.append(_gov_note(
                "Lifecycle workflows", exc, "LifecycleWorkflows.Read.All",
                "this tenant is not licensed for Entra ID Governance."))
            if _is_licence_error(exc):
                caps["licensed_governance"] = False
        except GraphError as exc:
            if _is_licence_error(exc):
                caps["licensed_governance"] = False
                notes.append("Lifecycle workflows require the Entra ID Governance licence.")
            else:
                notes.append(f"Lifecycle workflows: {clip(exc, 150)}")

        for workflow in workflows[:100]:
            try:
                rows, _ = await client.get_all(
                    f"/identityGovernance/lifecycleWorkflows/workflows/{workflow['id']}/runs",
                    top=0, max_items=_MAX_RUNS_PER_WORKFLOW,
                )
            except GraphError:
                continue
            for raw in rows:
                row = as_dict(raw)
                workflow["runs"]["total"] += 1
                failed = int(row.get("failedTasksCount") or 0)
                if failed or str(row.get("processingStatus") or "") == "failed":
                    workflow["runs"]["failed"] += 1
                else:
                    workflow["runs"]["successful"] += 1

        data = {
            "reviews": reviews,
            "packages": packages,
            "assignments": assignments,
            "workflows": workflows,
            "capabilities": caps,
            "counts": {
                "reviews": len(reviews),
                "reviews_active": sum(1 for r in reviews
                                      if r["status"] not in ("Completed", "Applied")),
                "packages": len(packages),
                "assignments": len(assignments),
                "workflows": len(workflows),
                "workflows_enabled": sum(1 for w in workflows if w["enabled"]),
                "leaver_workflows": sum(1 for w in workflows
                                        if w["category"] == "leaver" and w["enabled"]),
            },
        }

        if not any((caps["access_reviews"], caps["entitlement"], caps["lifecycle"])):
            if not (caps["licensed_p2"] and caps["licensed_governance"]):
                return model.unlicensed_payload(
                    DOMAIN,
                    "Access reviews and entitlement management require Entra ID P2; lifecycle "
                    "workflows require Entra ID Governance.",
                ) | {"data": data, "notes": notes}
            return model.blind_payload(
                DOMAIN, "No identity governance data could be read for this tenant.",
                ["AccessReview.Read.All", "EntitlementManagement.Read.All",
                 "LifecycleWorkflows.Read.All"],
            ) | {"data": data, "notes": notes}

        status = model.STATUS_PARTIAL if notes else model.STATUS_OK
        blockers = []
        if not caps.get("licensed_governance", True):
            blockers.append(model.blocker(
                model.BLOCKER_LICENCE,
                "Lifecycle workflows are not available on this tenant's licence.",
                scope="Entra ID Governance",
                impact="Joiner/mover/leaver automation cannot be reviewed.",
            ))
        if not caps.get("licensed_p2", True):
            blockers.append(model.blocker(
                model.BLOCKER_LICENCE,
                "Access reviews and entitlement management need a higher licence.",
                scope="Entra ID P2 / ID Governance",
                impact="Access certification cannot be reviewed.",
            ))
        return model.domain_payload(
            DOMAIN, data, status=status,
            item_count=len(reviews) + len(packages) + len(workflows), notes=notes,
            blockers=blockers,
        )

    return await guarded(DOMAIN, ctx, _run)


# ------------------------------------------------------------------------- coverage
# The synthesis, and the reason this module exists. Computed from the INVENTORY domains so
# that it renders on a tenant with no governance license at all — where every row reads
# "never reviewed", which is exactly the finding that tenant needs to see.
COVERAGE_CLASSES: tuple[dict[str, str], ...] = (
    {"key": "privileged_roles", "label": "Privileged directory roles",
     "why": "An unreviewed privileged role is standing power nobody has re-justified."},
    {"key": "role_assignable_groups", "label": "Role-assignable groups",
     "why": "Membership of these groups confers directory roles; unreviewed membership is unreviewed privilege."},
    {"key": "guests", "label": "Guest accounts",
     "why": "Guests accumulate. Without a recurring review, external access only ever grows."},
    {"key": "high_privilege_apps", "label": "High-privilege applications",
     "why": "Applications holding critical Graph permissions are rarely re-examined after consent."},
    {"key": "tenant_wide_consent", "label": "Tenant-wide delegated consent",
     "why": "An AllPrincipals grant applies to everyone and should be re-justified periodically."},
)


def _reviewed_targets(gov_data: dict[str, Any]) -> dict[str, set[str]]:
    """Which object ids / classes have an access review pointed at them."""
    out: dict[str, set[str]] = {"role": set(), "group": set(), "guests": set(),
                                "application": set(), "access_package": set()}
    for review in gov_data.get("reviews") or []:
        scope = as_dict(review.get("scope"))
        kind = str(scope.get("kind") or "")
        if kind in out:
            target = str(scope.get("target") or "")
            out[kind].add(target or "*")
    return out


def _package_resource_ids(gov_data: dict[str, Any]) -> set[str]:
    return {str(a.get("principal_id") or "") for a in gov_data.get("assignments") or []
            if a.get("principal_id")}


def _principals_in_reviewed_packages(gov_data: dict[str, Any], packages: set[str]) -> set[str]:
    """Principals whose access comes through a package that has a review pointed at it.

    Entitlement management is the other way an object gets reviewed: the review targets the
    package's assignments, not the principals directly. Counting only directly-scoped
    reviews reported "0 reviewed" on a tenant running a monthly review over every package.
    """
    if not packages:
        return set()
    return {
        str(a.get("principal_id") or "")
        for a in gov_data.get("assignments") or []
        if a.get("principal_id") and str(a.get("package_id") or "") in packages
    }


def coverage(snapshot_data: dict[str, Any]) -> list[dict[str, Any]]:
    """For each thing that should be governed: is it? One row per object class."""
    gov = as_dict(snapshot_data.get("governance"))
    roles = as_dict(snapshot_data.get("roles"))
    people = as_dict(snapshot_data.get("people"))
    apps = as_dict(snapshot_data.get("apps"))

    reviewed = _reviewed_targets(gov)
    governed_principals = _package_resource_ids(gov)
    reviewed_via_package = _principals_in_reviewed_packages(gov, reviewed["access_package"])
    role_review_all = "*" in reviewed["role"]
    guest_review = bool(reviewed["guests"])

    privileged_roles = sorted({
        str(a.get("role_name") or "") for a in as_list(roles.get("assignments"))
        if a.get("role_privileged")
    } | {
        str(e.get("role_name") or "") for e in as_list(roles.get("eligible"))
        if e.get("role_privileged")
    } - {""})
    reviewed_roles = len(privileged_roles) if role_review_all else len(
        [r for r in privileged_roles if r in reviewed["role"]])

    assignable = [g for g in as_list(people.get("groups")) if g.get("is_assignable_to_role")]
    reviewed_groups = len([g for g in assignable
                           if str(g.get("id") or "") in reviewed["group"]
                           or str(g.get("id") or "") in reviewed_via_package])

    guests = [u for u in as_list(people.get("users")) if str(u.get("user_type")) == "Guest"]
    governed_guests = len([g for g in guests
                           if str(g.get("id") or "") in governed_principals])
    reviewed_guests = len(guests) if guest_review else len(
        [g for g in guests if str(g.get("id") or "") in reviewed_via_package])

    high_priv_apps = [
        sp for sp in as_list(apps.get("service_principals"))
        if any(str(p.get("tier")) in ("critical", "high")
               for p in as_list(sp.get("granted_app_permissions")))
    ]
    reviewed_apps = len([sp for sp in high_priv_apps
                         if str(sp.get("object_id") or "") in reviewed["application"]])

    tenant_wide = [
        sp for sp in as_list(apps.get("service_principals"))
        if any(str(g.get("consent_type")) == "AllPrincipals"
               for g in as_list(sp.get("granted_delegated")))
    ]

    rows = [
        {"key": "privileged_roles", "count": len(privileged_roles), "reviewed": reviewed_roles,
         "governed": 0, "objects": privileged_roles[:200]},
        {"key": "role_assignable_groups", "count": len(assignable), "reviewed": reviewed_groups,
         "governed": 0,
         "objects": [str(g.get("display_name") or "") for g in assignable][:200]},
        {"key": "guests", "count": len(guests),
         "reviewed": reviewed_guests, "governed": governed_guests,
         "objects": [str(g.get("upn") or g.get("display_name") or "") for g in guests][:200]},
        {"key": "high_privilege_apps", "count": len(high_priv_apps), "reviewed": reviewed_apps,
         "governed": 0,
         "objects": [str(s.get("display_name") or "") for s in high_priv_apps][:200]},
        {"key": "tenant_wide_consent", "count": len(tenant_wide), "reviewed": 0, "governed": 0,
         "objects": [str(s.get("display_name") or "") for s in tenant_wide][:200]},
    ]
    meta = {c["key"]: c for c in COVERAGE_CLASSES}
    for row in rows:
        row["label"] = meta[row["key"]]["label"]
        row["why"] = meta[row["key"]]["why"]
        row["gap"] = max(0, row["count"] - max(row["reviewed"], row["governed"]))
    return rows
