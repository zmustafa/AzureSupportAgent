"""Cross-plane escalation — the directory plane reaching the Azure plane.

Every other escalation signal reasons inside one plane. These two cross the boundary, which is
where the most under-reported privilege in a tenant lives: the two control planes are
administered by different teams, shown on different screens, and each looks fine on its own.

Both are computed from data the IAM directory layer already collects — Entra directory-role
assignments and service-principal owners — so neither needs a second Graph call, and neither
can disagree with the Entra screen about who holds what.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import schema
from app.iam.signals import Finding, SignalContext, SignalSpec

# Both spellings appear in real tenants: `Company Administrator` is the original directory role
# name and Graph still returns it for tenants created before the rename. Matching only the
# modern name silently misses the most powerful role in the tenant on older directories.
GLOBAL_ADMIN_ROLES = {"global administrator", "company administrator"}

# The Azure-side role a Global Administrator can grant themselves. Not a coincidence of naming:
# the elevation toggle assigns exactly this at the root scope.
ELEVATION_TARGET_ROLE = "user access administrator"
ROOT_SCOPE = "/"


def _who(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


def _entra_rows(ctx: SignalContext) -> list[dict[str, Any]]:
    return [r for r in ctx.grants if r.get("surface") == schema.SURFACE_ENTRA]


def _global_admin_elevation(ctx: SignalContext) -> list[Finding]:
    """A Global Administrator is one toggle away from owning every subscription.

    "Access management for Azure resources" in Entra grants the caller **User Access
    Administrator at the tenant root** — above every management group and subscription, and
    inherited by all of them. It is self-service, needs no Azure permission at all, and is not
    visible anywhere in the Azure RBAC screens because until it is used the assignment does not
    exist.

    This is the single most common reason an access review is wrong: the reviewer reads the
    Azure plane, sees a short list of Owners, and certifies it — while a directory role nobody
    on that screen can see confers strictly more power than everything on it."""
    entra = _entra_rows(ctx)
    ctx.require(
        ctx.collector_ran("EntraRoleAssignments"),
        "Entra directory roles were not collected, so cross-plane elevation cannot be assessed.",
    )

    # Who already holds tenant-root Azure privilege — reported, but it does not suppress the
    # finding: the elevation remains available whether or not it has been used.
    already: set[str] = {
        str(r.get("effectivePrincipalId") or "").lower()
        for r in ctx.grants
        if r.get("surface") != schema.SURFACE_ENTRA
        and str(r.get("scope") or "") == ROOT_SCOPE
        and str(r.get("roleName") or "").strip().lower() == ELEVATION_TARGET_ROLE
    }

    by_principal: dict[str, dict[str, Any]] = {}
    for r in entra:
        if str(r.get("roleName") or "").strip().lower() not in GLOBAL_ADMIN_ROLES:
            continue
        pid = str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower()
        if pid:
            by_principal.setdefault(pid, r)

    out: list[Finding] = []
    for pid, row in by_principal.items():
        holds = pid in already
        out.append(
            Finding(
                signal_id="esc.global_admin_azure_elevation",
                title="Global Administrator can take ownership of every subscription",
                severity="critical",
                pillar="esc",
                object_kind="principal",
                subject=pid,
                subject_label=_who(row),
                detail=(
                    f"{_who(row)} holds Global Administrator. That role can self-assign User "
                    f"Access Administrator at the tenant root through the 'Access management for "
                    f"Azure resources' toggle — above every management group and subscription, "
                    f"needing no Azure permission and no approval. "
                    + (
                        "They already hold that root assignment."
                        if holds
                        else "The assignment does not exist yet, which is why it appears nowhere "
                             "in the Azure access screens."
                    )
                ),
                evidence={
                    "directoryRole": row.get("roleName"),
                    "principalType": row.get("effectivePrincipalType"),
                    "alreadyElevated": holds,
                    "assignmentState": row.get("assignmentState"),
                },
                remediation=(
                    "Reduce the number of permanent Global Administrators and make the role "
                    "PIM-eligible with approval. Audit the elevation toggle in Entra > Properties."
                ),
                frameworks=("NIST:AC-6(5)", "CIS-Azure:1.1", "MCSB:PA-1"),
            )
        )
    return out


def _sp_owner_to_directory_role(ctx: SignalContext) -> list[Finding]:
    """Owning a service principal is equivalent to being it.

    An owner can add a client secret or certificate to the application and then authenticate as
    it. So a user who owns a service principal that holds a privileged directory role holds that
    role in practice — through a relationship that appears on no role-assignment screen, needs
    no approval, and generates no privileged-access alert.

    Reported per service principal rather than per owner: the object to fix is the application,
    and the count of people who can reach it is the measure of how bad it is."""
    entra = _entra_rows(ctx)
    ctx.require(
        ctx.collector_ran("EntraRoleAssignments"),
        "Entra directory roles were not collected, so directory privilege cannot be assessed.",
    )
    ctx.require(
        ctx.collector_ran("ServicePrincipalOwners"),
        "Service-principal owners were not collected, so who can add a credential is unknown.",
    )

    # Service principals that hold a privileged directory role.
    privileged_sps: dict[str, dict[str, Any]] = {}
    for r in entra:
        if r.get("accessPath") == schema.PATH_OWNER:
            continue  # an ownership row, not a role grant
        if str(r.get("effectivePrincipalType") or "") != "ServicePrincipal":
            continue
        if not r.get("roleIsPrivileged"):
            continue
        pid = str(r.get("effectivePrincipalId") or "").lower()
        if pid:
            privileged_sps.setdefault(pid, r)
    if not privileged_sps:
        return []

    # Owners, keyed by the service principal they own. On an ownership row `principalId` is the
    # OWNED service principal and `effectivePrincipalId` is the human who owns it.
    owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if r.get("accessPath") != schema.PATH_OWNER:
            continue
        owned = str(r.get("principalId") or "").lower()
        if owned in privileged_sps:
            owners[owned].append(r)

    out: list[Finding] = []
    for sp_id, holders in owners.items():
        role_row = privileged_sps[sp_id]
        names = sorted({_who(h) for h in holders})
        out.append(
            Finding(
                signal_id="esc.sp_owner_to_directory_role",
                title="Owning this application confers its privileged directory role",
                severity="error",
                pillar="esc",
                object_kind="principal",
                subject=sp_id,
                subject_label=str(role_row.get("effectivePrincipalName") or sp_id),
                count=len(names),
                detail=(
                    f"{role_row.get('effectivePrincipalName') or sp_id} holds "
                    f"{role_row.get('roleName')}, and {len(names)} principal(s) own it. An owner "
                    f"can add a client secret or certificate and then authenticate as the "
                    f"application, which makes owning it equivalent to holding the role — "
                    f"through a relationship that appears on no role-assignment screen: "
                    f"{', '.join(names[:5])}."
                ),
                evidence={
                    "directoryRole": role_row.get("roleName"),
                    "owners": names[:10],
                    "ownerCount": len(names),
                },
                remediation=(
                    "Remove human owners from applications holding privileged directory roles, "
                    "and manage their credentials through a controlled process instead."
                ),
                frameworks=("NIST:AC-6", "MCSB:IM-3", "MCSB:PA-1"),
            )
        )
    return out


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="esc.global_admin_azure_elevation",
        title="Global Administrator can take ownership of every subscription",
        pillar="esc", severity="critical", weight=10, object_kind="principal",
        why=(
            "The Azure and directory planes are reviewed separately, so the most powerful path "
            "in the tenant is invisible on both screens: a Global Administrator can grant "
            "themselves User Access Administrator at the tenant root at any time."
        ),
        remediation="Minimise permanent Global Administrators; make the role PIM-eligible with approval.",
        frameworks=("NIST:AC-6(5)", "CIS-Azure:1.1", "MCSB:PA-1"),
        tags=("cross-plane", "tier0"),
        evaluate=_global_admin_elevation,
    ),
    SignalSpec(
        id="esc.sp_owner_to_directory_role",
        title="Application owners inherit its privileged directory role",
        pillar="esc", severity="error", weight=8, object_kind="principal",
        why=(
            "An owner can add a credential to the application and authenticate as it, so "
            "ownership of a privileged application is privilege — with no role assignment "
            "anywhere to show for it."
        ),
        remediation="Remove human owners from privileged applications.",
        frameworks=("NIST:AC-6", "MCSB:IM-3"),
        tags=("cross-plane",),
        evaluate=_sp_owner_to_directory_role,
    ),
]
