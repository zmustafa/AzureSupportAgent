"""Normalized access-row schema for the RBAC (access review) feature.

Ports the wide schema produced by the standalone *all-azure-access* scanner
(github.com/zmustafa/AzureEntraIDIAMScanner) into the app so every access surface — Azure
RBAC (control & data plane), Entra directory roles, group-derived access, service-principal
ownership, PIM/eligible — can be compared in ONE grid. The exact column names are preserved
so a row here is interchangeable with the scanner's ``allAzureAccess.json`` (import parity).

A *row* is a flat dict with the 46 ``COLUMNS`` keys. :func:`make_row` fills every key with a
sane default so partial collectors never emit ragged rows. Role-privilege classification
(:func:`role_is_privileged`, :func:`role_category`) mirrors the scanner's heuristics."""
from __future__ import annotations

from typing import Any

# The 46 normalized columns, in the scanner's canonical order. **Frozen** — this tuple is the
# import/export contract with the standalone scanner, so nothing may be added, removed or
# reordered here. New columns go on ``EXTRA_COLUMNS`` below; ``fmt=scanner`` exports project
# back down to exactly this set so a round trip stays byte-identical.
SCANNER_COLUMNS: tuple[str, ...] = (
    "surface",
    "accessModel",
    "collector",
    "assignmentState",
    "assignmentType",
    "principalId",
    "principalType",
    "principalDisplayName",
    "principalUserPrincipalName",
    "principalAppId",
    "effectivePrincipalId",
    "effectivePrincipalType",
    "effectivePrincipalName",
    "effectivePrincipalUserPrincipalName",
    "accessPath",
    "groupChain",
    "sourceGroupId",
    "sourceGroupName",
    "roleName",
    "roleDefinitionId",
    "roleCategory",
    "roleIsPrivileged",
    "roleHasDataActions",
    "scope",
    "scopeType",
    "scopeDisplayName",
    "tenantId",
    "managementGroupId",
    "managementGroupName",
    "subscriptionId",
    "subscriptionName",
    "resourceGroup",
    "resourceType",
    "resourceName",
    "childResourceType",
    "childResourceName",
    "assignmentId",
    "assignmentCreatedOn",
    "assignmentUpdatedOn",
    "condition",
    "conditionVersion",
    "isInherited",
    "sourceApi",
    "collectionStatus",
    "errorCode",
    "errorMessage",
)

# Columns this product adds beyond the scanner. APPEND ONLY — never insert, never reorder.
EXTRA_COLUMNS: tuple[str, ...] = (
    # Allow | Deny. Deny assignments (Blueprints / Managed Apps) are evaluated BEFORE role
    # assignments and cannot be overridden, so an access report that omits them is wrong.
    "effect",
    # Provenance: True when the row came from an imported scanner run rather than a live scan.
    "imported",
    # --- PIM / JIT ---------------------------------------------------------------------
    # True when this grant is governed by PIM at all (eligible, or an active JIT elevation).
    # Distinguishes "standing privilege" from "privilege someone has to ask for", which is the
    # single most important distinction in privileged access.
    "pimManaged",
    # Eligibility window. Azure states the window as a DURATION with no end date, so the end is
    # DERIVED (start + duration) — see collectors._derive_end.
    "eligibilityStartDateTime",
    "eligibilityEndDateTime",
    # True when the eligibility never expires ("permanently eligible").
    "isPermanentEligible",
    # When a currently-active JIT elevation lapses. Empty on permanent assignments.
    "activationExpiresOn",
    # Direct | Group | Inherited — how the principal holds the eligibility.
    "memberType",
    # Activation controls from the role management policy. A tenant that bought PIM but requires
    # neither approval nor MFA has bought very little.
    "requiresApproval",
    "requiresMfa",
    "requiresJustification",
    "activationMaxHours",
    # --- principal resolution ----------------------------------------------------------
    # "true" | "false" | "unknown" (a STRING, deliberately). When a deleted principal's
    # assignment survives in ARM, the object id no longer resolves — but "we could not read the
    # directory" and "the principal does not exist" are the same picture and opposite facts, so
    # a boolean here would force one of them to be a lie.
    "principalExists",
    # --- deny scoping ------------------------------------------------------------------
    # A deny assignment with this set applies at its OWN scope only, not to anything beneath it.
    # Without the flag the effective-permission engine has to assume every deny cascades, which
    # reports principals as blocked on resources the deny never touched.
    "doNotApplyToChildScopes",
    # --- cross-tenant delegation -------------------------------------------------------
    # The Azure Lighthouse MANAGING tenant — whose directory the principal actually lives in.
    # Distinct from `tenantId`, which is the tenant being scanned. Without its own column the
    # delegation signal groups every managing tenant into one bucket labelled "unknown", which
    # is precisely the fact a reader needs: *which* outside organisation holds this access.
    "managingTenantId",
    "managingTenantName",
)

# principalExists values. Only ``false`` is an orphan; ``unknown`` means we could not look.
EXISTS_TRUE = "true"
EXISTS_FALSE = "false"
EXISTS_UNKNOWN = "unknown"

COLUMNS: tuple[str, ...] = (*SCANNER_COLUMNS, *EXTRA_COLUMNS)

# Surfaces (the "what kind of access" axis the Insights pivots group by).
SURFACE_AZURE_RBAC = "Azure RBAC"
SURFACE_ENTRA = "Entra ID RBAC"
SURFACE_KEY_VAULT = "Key Vault Access Policy"
SURFACE_CLASSIC = "Classic Admin"
SURFACE_DENY = "Deny Assignment"
# Azure Lighthouse: a managing tenant's principals holding roles in this one. Its own surface
# because these grants do NOT appear in the portal's Access control (IAM) blade, so folding
# them into Azure RBAC would make the grid disagree with the portal for the one kind of access
# an operator is least likely to already know about.
SURFACE_LIGHTHOUSE = "Lighthouse Delegation"

# Access models (finer-grained than surface; used for the data-plane split).
ACCESS_CONTROL_PLANE = "AzureRBAC"
ACCESS_DATA_PLANE = "AzureDataPlaneRBAC"
ACCESS_ENTRA = "EntraDirectoryRole"
ACCESS_KV_POLICY = "KeyVaultAccessPolicy"
ACCESS_CLASSIC = "ClassicAzureAdmin"
ACCESS_DENY = "AzureDenyAssignment"
ACCESS_LIGHTHOUSE = "LighthouseDelegation"

# Effects. Every pre-existing row is an Allow; deny assignments are evaluated first and win.
EFFECT_ALLOW = "Allow"
EFFECT_DENY = "Deny"

# Assignment states (PIM distinguishes Active from Eligible/JIT).
STATE_ACTIVE = "Active"
STATE_ELIGIBLE = "Eligible"

# Access paths (how the principal effectively receives the access).
PATH_DIRECT = "Direct"
PATH_GROUP = "GroupTransitive"
PATH_OWNER = "Owner"

# Scope types (the resource-hierarchy level an assignment lands on).
SCOPE_TENANT = "tenantRoot"
SCOPE_MANAGEMENT_GROUP = "managementGroup"
SCOPE_SUBSCRIPTION = "subscription"
SCOPE_RESOURCE_GROUP = "resourceGroup"
SCOPE_RESOURCE = "resource"
SCOPE_DIRECTORY = "directory"

# Collector statuses (every collector reports one; the run continues on any non-fatal value).
STATUS_SUCCEEDED = "Succeeded"
STATUS_SUCCEEDED_WARN = "SucceededWithWarnings"
STATUS_PARTIAL = "PartiallyCollected"
STATUS_SKIPPED = "Skipped"
STATUS_UNAUTHORIZED = "Unauthorized"
STATUS_THROTTLED = "Throttled"
STATUS_FAILED = "Failed"

# A status is "needs attention" (surfaced in Diagnostics) when it isn't a clean success/skip.
ATTENTION_STATUSES = frozenset(
    {STATUS_PARTIAL, STATUS_UNAUTHORIZED, STATUS_THROTTLED, STATUS_FAILED}
)

# The strictly narrower set meaning "this scope produced NO trustworthy rows".
#
# ``PartiallyCollected`` deliberately is NOT here. A tenant without an Entra ID P2 licence gets a
# 400 from every PIM endpoint, which makes every scope Partial forever — treating Partial as
# untrustworthy made delta refresh re-collect the entire estate on exactly the tenants it was
# meant to help, while still reporting that it had done a delta. Partial means "we got the rows,
# something alongside them was degraded"; these three mean "we did not get the rows".
UNTRUSTWORTHY_STATUSES = frozenset(
    {STATUS_UNAUTHORIZED, STATUS_THROTTLED, STATUS_FAILED}
)

# Azure RBAC roles that grant privileged (write/assign/delete) control-plane access by name.
PRIVILEGED_AZURE_ROLES = frozenset(
    {
        "owner",
        "contributor",
        "user access administrator",
        "role based access control administrator",
        "co-administrator",
        "account administrator",
        "service administrator",
    }
)

# Entra directory roles considered privileged (tenant-wide blast radius).
PRIVILEGED_ENTRA_ROLES = frozenset(
    {
        "global administrator",
        "company administrator",
        "privileged role administrator",
        "privileged authentication administrator",
        "user administrator",
        "application administrator",
        "cloud application administrator",
        "authentication administrator",
        "groups administrator",
        "security administrator",
        "conditional access administrator",
        "exchange administrator",
        "sharepoint administrator",
        "teams administrator",
        "intune administrator",
        "global reader",
    }
)

# Severity tiers (reused by the UI badges; mirrors the identity dashboard's vocabulary).
SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3, "ok": 4}

# Columns coerced to bool by :func:`make_row`.
_BOOL_COLUMNS = frozenset({
    "roleIsPrivileged", "roleHasDataActions", "isInherited", "imported",
    "pimManaged", "isPermanentEligible", "requiresApproval", "requiresMfa", "requiresJustification",
    "doNotApplyToChildScopes",
})


def is_standing_privilege(row: dict[str, Any]) -> bool:
    """True when the row is *permanent* privileged access — the thing PIM exists to eliminate.

    An eligible assignment is not standing (it must be activated first), and an active JIT
    elevation is not standing either (it expires). Everything else privileged is."""
    if not row.get("roleIsPrivileged"):
        return False
    if row.get("effect") == EFFECT_DENY:
        return False
    if row.get("assignmentState") == STATE_ELIGIBLE:
        return False
    # An active row that PIM governs is a time-boxed elevation, not standing privilege.
    return not (row.get("pimManaged") and row.get("activationExpiresOn"))


def make_row(**values: Any) -> dict[str, Any]:
    """Build a normalized access row: every one of the ``COLUMNS`` present with a default.

    String columns default to ``""``; boolean flags to ``False``; ``effect`` to ``Allow`` (the
    overwhelming majority of rows, and the safe reading if a collector forgets to set it — a row
    silently defaulting to Deny would hide access); ``principalExists`` to ``unknown`` (claiming
    a principal exists, or does not, on no evidence is worse than admitting we have not looked).
    Unknown keys are ignored so a collector can pass a superset without leaking non-schema
    fields."""
    row: dict[str, Any] = {}
    for col in COLUMNS:
        if col in _BOOL_COLUMNS:
            row[col] = bool(values.get(col, False))
        elif col == "effect":
            row[col] = values.get(col) or EFFECT_ALLOW
        elif col == "principalExists":
            row[col] = values.get(col) or EXISTS_UNKNOWN
        else:
            val = values.get(col, "")
            row[col] = "" if val is None else val
    return row


def role_is_privileged(
    role_name: str,
    *,
    surface: str = SURFACE_AZURE_RBAC,
    has_data_actions: bool = False,
    data_actions: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Heuristic: does this role grant privileged access?

    Azure RBAC: the canonical write/assign roles by name, plus any data-plane role that can
    modify data or reach a credential. Entra: the tenant-admin role set.

    Pass ``data_actions`` whenever the role definition is at hand. The data-plane judgement is
    then also made on what the role can actually DO, via :mod:`app.iam.dataplane`. The older test
    — "has dataActions and the name contains owner or contributor" — missed `Key Vault
    Administrator`, `Key Vault Secrets Officer`, `Azure Kubernetes Service RBAC Cluster Admin`
    and `Storage File Data SMB Admin` on a real 981-role catalogue: 118 genuinely dangerous roles
    in all.

    The two tests are UNIONED, never swapped. The name test also produces false positives
    (`Avere Contributor`, `AgFood Platform Sensor Partner Contributor` — flagged purely because
    the word appears in the name), and dropping them would have been tempting. But this flag
    drives the privileged counts, the standing-privilege signals and the PIM screens, so a false
    positive costs noise while a false negative hides real access — and quietly demoting
    `Log Analytics Contributor` and eight others would have been a REDUCTION in coverage
    disguised as a precision fix. The exact tier is available from
    :func:`app.iam.dataplane.role_tier` where precision matters."""
    name = (role_name or "").strip().lower()
    if surface == SURFACE_ENTRA:
        return name in PRIVILEGED_ENTRA_ROLES
    if name in PRIVILEGED_AZURE_ROLES:
        return True
    if has_data_actions and ("owner" in name or "contributor" in name):
        return True
    if data_actions is not None:
        from app.iam import dataplane

        return dataplane.is_privileged_data_role(role_name, data_actions)
    return False


def role_has_data_actions(actions: list[str] | None, data_actions: list[str] | None) -> bool:
    """True when the role definition declares any dataActions (data-plane reach)."""
    return bool(data_actions)


def role_category(has_data_actions: bool, *, surface: str = SURFACE_AZURE_RBAC) -> str:
    """ControlPlane / DataPlane / Mixed classification for a role definition."""
    if surface == SURFACE_ENTRA:
        return "Directory"
    return "DataPlane" if has_data_actions else "ControlPlane"


def parse_scope(scope: str) -> dict[str, str]:
    """Decompose an ARM scope id into its hierarchy parts.

    Returns a dict with ``scopeType`` and any of ``managementGroupId`` / ``subscriptionId`` /
    ``resourceGroup`` / ``resourceType`` / ``resourceName`` that the scope encodes. Robust to
    the tenant-root ("/") and management-group scopes."""
    s = (scope or "").strip()
    out: dict[str, str] = {}
    if not s or s == "/":
        out["scopeType"] = SCOPE_TENANT
        return out
    low = s.lower()
    if "/providers/microsoft.management/managementgroups/" in low:
        out["scopeType"] = SCOPE_MANAGEMENT_GROUP
        out["managementGroupId"] = s.rstrip("/").split("/")[-1]
        return out
    parts = [p for p in s.split("/") if p]
    # parts like: subscriptions, <sub>, resourceGroups, <rg>, providers, <ns>, <type>, <name>...
    lparts = [p.lower() for p in parts]
    if "subscriptions" in lparts:
        i = lparts.index("subscriptions")
        if i + 1 < len(parts):
            out["subscriptionId"] = parts[i + 1]
    if "resourcegroups" in lparts:
        i = lparts.index("resourcegroups")
        if i + 1 < len(parts):
            out["resourceGroup"] = parts[i + 1]
    if "providers" in lparts:
        i = lparts.index("providers")
        # provider/type/name (possibly with child types)
        if i + 3 < len(parts):
            out["resourceType"] = f"{parts[i + 1]}/{parts[i + 2]}"
            out["resourceName"] = parts[i + 3]
    # Decide the scope type from the deepest part present.
    if out.get("resourceName"):
        out["scopeType"] = SCOPE_RESOURCE
    elif out.get("resourceGroup"):
        out["scopeType"] = SCOPE_RESOURCE_GROUP
    elif out.get("subscriptionId"):
        out["scopeType"] = SCOPE_SUBSCRIPTION
    else:
        out["scopeType"] = SCOPE_TENANT
    return out
