"""External-access signals (pillar weight 14).

Access held by someone who is not part of this organization. Guest and multi-tenant detection is
possible today from the composed rows; Lighthouse delegations arrive with their collector, and
until then the pillar says so rather than reporting zero.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import schema
from app.iam.signals import Finding, SignalContext, SignalSpec


def _who(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or row.get("effectivePrincipalUserPrincipalName")
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


def _upn(row: dict[str, Any]) -> str:
    return str(row.get("effectivePrincipalUserPrincipalName") or row.get("principalUserPrincipalName") or "")


def _guest_label(row: dict[str, Any]) -> str:
    """Display name plus UPN, because the display name alone does not identify a guest."""
    name, upn = _who(row), _upn(row)
    return f"{name} ({upn})" if upn and upn != name else name


def _is_guest(row: dict[str, Any]) -> bool:
    """Guest detection from the UPN.

    B2B guests carry ``#EXT#`` in their user principal name — the home tenant's address is
    mangled into the local part. This is a *sufficient* test, not a complete one: a guest whose
    UPN we never resolved will be missed, which is why the signals below say what they counted
    rather than claiming a total."""
    return "#ext#" in _upn(row).lower()


def _guest_access(ctx: SignalContext) -> list[Finding]:
    ctx.require(
        any(_upn(r) for r in ctx.grants),
        "No principal names were resolved, so guests cannot be identified.",
    )
    guests: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if _is_guest(r):
            guests[str(r.get("effectivePrincipalId") or _upn(r))].append(r)
    out: list[Finding] = []
    for pid, rows in guests.items():
        privileged = [r for r in rows if r.get("roleIsPrivileged")]
        out.append(
            Finding(
                # One signal id, varying severity. Switching the id when a guest happens to be
                # privileged would change the fingerprint, so a suppression recorded against
                # the guest would silently evaporate the day their role changed -- and the
                # finding would come back looking new.
                signal_id="ext.guest_access",
                title="Guest holds privileged access" if privileged else "Guest holds access",
                severity="critical" if privileged else "warning",
                pillar="ext",
                object_kind="principal",
                subject=pid,
                # The UPN, not just the display name. Two guest objects for the same human
                # (a second B2B invite, or the same person invited from two home tenants) are
                # separate subjects with separate access — labeling both "Jane Doe" leaves a
                # reviewer looking at two identical-looking rows with no way to act on either.
                subject_label=_guest_label(rows[0]),
                detail=(
                    f"{_who(rows[0])} is an external (B2B) account with "
                    f"{len(rows)} assignment(s)"
                    + (f", including {privileged[0].get('roleName')}." if privileged else ".")
                    + " Their credential and its lifecycle belong to another organization."
                ),
                count=len(rows),
                evidence={
                    "upn": _upn(rows[0]),
                    "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                },
                remediation="Confirm the guest still needs this access; prefer time-bound eligible assignments for external parties.",
                frameworks=("CIS-Azure:1.3", "NIST:AC-2", "MCSB:PA-4"),
            )
        )
    return out


def _external_privileged_summary(ctx: SignalContext) -> list[Finding]:
    """Tenant-level roll-up so a single number can be tracked over time."""
    ctx.require(
        any(_upn(r) for r in ctx.grants),
        "No principal names were resolved, so external access cannot be measured.",
    )
    guests = {str(r.get("effectivePrincipalId") or _upn(r)) for r in ctx.grants if _is_guest(r)}
    if not guests:
        return []
    return [
        Finding(
            signal_id="ext.external_footprint",
            title="External identities hold access to this estate",
            severity="info",
            pillar="ext",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(guests)} external (B2B) identit(ies) hold access. Detection is by "
                f"#EXT# in the user principal name, so a guest whose name was never resolved "
                f"is not counted here."
            ),
            count=len(guests),
            evidence={"guests": sorted(guests)[:10]},
            remediation="Review external access on a schedule; it rarely gets removed on its own.",
            frameworks=("NIST:AC-2",),
        )
    ]


def _lighthouse_delegations(ctx: SignalContext) -> list[Finding]:
    """Azure Lighthouse — another tenant's principals holding roles in yours.

    Not collected yet (the collector lands with the external-access work), so this reports
    *not measured* rather than zero. A tenant with delegations it does not know about is exactly
    the case where a confident "none found" would be most damaging."""
    ctx.require(
        ctx.collector_ran("AzureLighthouseDelegations"),
        "Lighthouse delegations are not collected yet, so cross-tenant delegated access is unmeasured.",
    )
    rows = [r for r in ctx.grants if r.get("surface") == schema.SURFACE_LIGHTHOUSE]
    if not rows:
        return []
    by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_tenant[str(r.get("managingTenantId") or r.get("tenantId") or "unknown")].append(r)
    return [
        Finding(
            signal_id="ext.lighthouse_delegation",
            title="Another tenant holds delegated access",
            severity="error" if any(r.get("roleIsPrivileged") for r in rs) else "warning",
            pillar="ext",
            object_kind="delegation",
            subject=tid,
            subject_label=f"Managing tenant {tid}",
            detail=(
                f"{len(rs)} delegated authorization(s) from tenant {tid}. These do not appear in "
                f"the portal's Access control (IAM) blade."
            ),
            count=len(rs),
            evidence={"roles": sorted({str(r.get("roleName")) for r in rs})[:10]},
            remediation="Confirm the managing tenant is an intended partner and the delegated roles are minimal.",
            frameworks=("MCSB:PA-4",),
        )
        for tid, rs in by_tenant.items()
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="ext.guest_access",
        title="Guests hold access to this estate",
        pillar="ext", severity="warning", weight=6, object_kind="principal",
        why="A guest's credential and its lifecycle belong to another organization.",
        remediation="Confirm the access is still needed; prefer time-bound eligible assignments.",
        frameworks=("CIS-Azure:1.3", "NIST:AC-2", "MCSB:PA-4"),
        evaluate=_guest_access,
    ),
    SignalSpec(
        id="ext.external_footprint",
        title="Size of the external-identity footprint",
        pillar="ext", severity="info", weight=3, object_kind="tenant",
        why="External access rarely gets removed on its own, so the trend matters more than the snapshot.",
        remediation="Review external access on a schedule.",
        frameworks=("NIST:AC-2",),
        evaluate=_external_privileged_summary,
    ),
    SignalSpec(
        id="ext.lighthouse_delegation",
        title="Cross-tenant Lighthouse delegations",
        pillar="ext", severity="error", weight=8, object_kind="delegation",
        why="Delegated authorizations grant another tenant real RBAC and are invisible in the IAM blade.",
        remediation="Confirm the managing tenant is intended and the delegated roles are minimal.",
        frameworks=("MCSB:PA-4",),
        evaluate=_lighthouse_delegations,
    ),
]
