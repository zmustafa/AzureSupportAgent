"""Who can access ONE resource — the Inventory drawer's question.

The Inventory grid is where people already are when they ask it, so this exists to answer it
there rather than making them carry a resource id to another screen.

Three things are load-bearing:

* **Inherited access is the answer, not a footnote.** Almost nobody is assigned at a resource.
  They are Owner on the subscription, or Contributor on a management group covering 26
  subscriptions, and they reach this resource from there. A view that showed only assignments
  written *at* the resource would report "nobody" for a resource anyone can delete. Every row
  therefore carries the scope it was granted at and how far above the resource that is.

* **Never scanned is not nobody.** If IAM has no snapshot for the tenant, this returns
  ``measured: False`` and no list at all. An empty access list on a resource is the single most
  reassuring thing this drawer could render, and it would be a lie.

* **RBAC is not the only door.** A resource whose access list is short but whose shared keys are
  enabled is not locked down. The bypass verdict is returned in the same payload for exactly
  that reason — separating them lets a reader draw a conclusion from half the picture.
"""
from __future__ import annotations

from typing import Any

from app.iam import cache, compose, effective, schema

# The drawer is a summary, not the Effective Access tab. A resource inside a management group
# with thousands of grants would otherwise return every one of them into a side panel.
MAX_PRINCIPALS = 200


def _subscription_of(resource_id: str) -> str:
    """The subscription a resource lives in, lower-cased, or "" if it is not under one."""
    parts = effective.normalize_scope(resource_id).lower().strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "subscriptions":
        return parts[1]
    return ""


def mg_subscriptions(tenant_id: str) -> dict[str, set[str]]:
    """Management-group scope -> the subscription ids beneath it.

    Needed because **management-group scopes are not string prefixes of the resources they
    govern**. `/providers/Microsoft.Management/managementGroups/<name>` does not start
    `/subscriptions/...`, so pure scope arithmetic reports that an MG Owner cannot reach
    anything. On one real tenant that silently hid 300 grants — the broadest and most
    privileged in the estate — from every resource in it.

    The ancestry is derived from the collection itself rather than a second Azure call: ARM
    returns an MG assignment as an inherited copy inside EVERY child subscription's slice, and
    those copies carry both the MG scope and the subscription they were seen under. Reading the
    raw (pre-dedupe) rows therefore reconstructs the parentage exactly for every MG that has any
    assignment — and an MG with no assignments has nothing to attribute anyway."""
    out: dict[str, set[str]] = {}
    for r in cache.all_scope_rows(tenant_id):
        if r.get("scopeType") != schema.SCOPE_MANAGEMENT_GROUP:
            continue
        sub = str(r.get("subscriptionId") or "").lower()
        if not sub:
            continue
        out.setdefault(effective.normalize_scope(str(r.get("scope", ""))).lower(), set()).add(sub)
    return out


def _covers(assignment_scope: str, resource_id: str, subscription: str,
            mg_map: dict[str, set[str]]) -> bool:
    """Does this assignment reach the resource — including down an MG edge?"""
    if effective.scope_covers(assignment_scope, resource_id):
        return True
    key = effective.normalize_scope(assignment_scope).lower()
    return bool(subscription) and subscription in mg_map.get(key, ())


def _distance(assignment_scope: str, resource_id: str) -> str:
    """How the access reaches this resource, in the reader's terms."""
    a = effective.normalize_scope(assignment_scope).lower()
    t = effective.normalize_scope(resource_id).lower()
    if a == t:
        return "this resource"
    if a == "/":
        return "tenant root"
    if "/providers/microsoft.management/managementgroups/" in a:
        return "management group"
    if "/resourcegroups/" in a:
        return "resource group"
    if a.startswith("/subscriptions/"):
        return "subscription"
    return "inherited"


def _bypass_for(tenant_id: str, resource_id: str) -> dict[str, Any]:
    """The non-RBAC doors into this resource, and whether we actually looked for them."""
    payload = cache.read_bypass(tenant_id)
    rows = payload.get("rows") or []
    # A sweep that ran and found nothing is not the same as no sweep. The meta entry is written
    # by `write_bypass` whether or not it produced rows, so it is the honest witness.
    swept = bool(cache.read_bypass_meta(tenant_id))
    target = effective.normalize_scope(resource_id).lower()
    mine = [r for r in rows
            if effective.normalize_scope(str(r.get("resourceId", ""))).lower() == target]
    enabled = [r for r in mine if r.get("enabled")]
    return {
        # A resource absent from a sweep that DID run genuinely has no known bypass. A resource
        # absent because no sweep ran is unknown, and the two must not render identically.
        "measured": swept,
        "checked": len(mine),
        "openDoors": [
            {
                "key": r.get("key", ""),
                "title": r.get("title", ""),
                "bypassKind": r.get("bypassKind", ""),
                "severity": r.get("severity", ""),
                "detail": r.get("detail", ""),
                "credentialAction": r.get("credentialAction", ""),
                "reachableCount": r.get("reachableCount", 0),
                "reachabilityAvailable": r.get("reachabilityAvailable", False),
                "remediation": r.get("remediation", ""),
            }
            for r in enabled
        ],
        "reason": "" if swept else (
            "No RBAC-bypass sweep has been run for this tenant, so whether this resource can be "
            "reached without a role assignment is unknown — not no."
        ),
    }


def for_resource(tenant_id: str, resource_id: str) -> dict[str, Any]:
    """Everyone who can reach ``resource_id``, grouped by principal, plus the bypass verdict."""
    resource_id = (resource_id or "").strip()
    if not resource_id:
        return {"measured": False, "principals": [], "total": 0,
                "reason": "No resource id was supplied.",
                "bypass": {"measured": False, "openDoors": [], "checked": 0, "reason": ""},
                "limitations": []}

    if not cache.has_any(tenant_id):
        # The wall. Everything below would be an artefact of not having looked.
        return {
            "measured": False,
            "principals": [],
            "total": 0,
            "reason": (
                "No access scan has been run for this connection, so who can reach this resource "
                "is unknown — not nobody. Run an access scan from the IAM screen."
            ),
            "bypass": _bypass_for(tenant_id, resource_id),
            "limitations": [],
        }

    rows = compose.build_master_rows(tenant_id)
    subscription = _subscription_of(resource_id)
    mg_map = mg_subscriptions(tenant_id)
    covering = [r for r in rows
                if _covers(str(r.get("scope", "")), resource_id, subscription, mg_map)]

    by_principal: dict[str, dict[str, Any]] = {}
    denied = 0
    for r in covering:
        if r.get("effect") == schema.EFFECT_DENY:
            # Deny assignments are reported separately: folding them into the allow list would
            # inflate "who can access this" with principals who specifically cannot.
            denied += 1
            continue
        pid = str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower()
        if not pid:
            continue
        entry = by_principal.setdefault(pid, {
            "principalId": pid,
            "principalName": r.get("effectivePrincipalName") or "",
            "principalType": r.get("effectivePrincipalType") or "",
            "principalExists": r.get("principalExists") or schema.EXISTS_UNKNOWN,
            # Carried for the same reason as principalExists: "Contributor" reads as live
            # access whether the account behind it can sign in or not, and the reader has no
            # other way to tell. Disabled is not deleted — the grant is dormant, not gone.
            "principalAccountEnabled": r.get("principalAccountEnabled") or schema.ENABLED_UNKNOWN,
            "privileged": False,
            "grants": [],
        })
        if not entry["principalName"] and r.get("effectivePrincipalName"):
            entry["principalName"] = r["effectivePrincipalName"]
        if r.get("roleIsPrivileged"):
            entry["privileged"] = True
        entry["grants"].append({
            "roleName": r.get("roleName", ""),
            "scope": r.get("scope", ""),
            "scopeLabel": r.get("scopeDisplayName") or r.get("scope", ""),
            "grantedAt": _distance(str(r.get("scope", "")), resource_id),
            "accessPath": r.get("accessPath", ""),
            "assignmentState": r.get("assignmentState", ""),
            "surface": r.get("surface", ""),
            "sourceGroupName": r.get("sourceGroupName", ""),
        })

    principals = sorted(
        by_principal.values(),
        key=lambda p: (not p["privileged"], (p["principalName"] or p["principalId"]).lower()),
    )
    total = len(principals)

    limitations: list[str] = []
    inherited = sum(1 for p in principals
                    for g in p["grants"] if g["grantedAt"] != "this resource")
    if inherited:
        limitations.append(
            f"{inherited} of these grants are inherited from a broader scope. Removing access "
            f"here means changing the assignment where it was made, which affects every other "
            f"resource under that scope."
        )
    if denied:
        limitations.append(
            f"{denied} deny assignment(s) also cover this resource and are not listed above. A "
            f"deny overrides an allow, so some principals listed may be blocked in practice."
        )
    if total > MAX_PRINCIPALS:
        limitations.append(
            f"Showing the first {MAX_PRINCIPALS} of {total} principals. Use the IAM Effective "
            f"Access tab for the full list."
        )
    if subscription and not mg_map:
        # Say it rather than quietly under-reporting. Missing MG ancestry removes exactly the
        # broadest grants, so the failure makes a resource look LESS exposed than it is.
        limitations.append(
            "No management-group ancestry could be derived from this scan, so access granted at "
            "a management group above this subscription is NOT included above. Collect the "
            "management-group scopes to see it."
        )

    return {
        "measured": True,
        "resourceId": resource_id,
        "principals": principals[:MAX_PRINCIPALS],
        "total": total,
        "privilegedTotal": sum(1 for p in principals if p["privileged"]),
        "reason": "",
        "bypass": _bypass_for(tenant_id, resource_id),
        "limitations": limitations,
    }
