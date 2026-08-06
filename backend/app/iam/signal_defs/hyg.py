"""Hygiene signals (pillar weight 12) — access that should simply not exist any more.

This is the cheapest pillar to act on: almost every finding here is a deletion with no
operational risk, which is why it is the fastest way to make the screen useful on day one.
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
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


def _scope_label(row: dict[str, Any]) -> str:
    return str(row.get("scopeDisplayName") or row.get("subscriptionName") or row.get("scope") or "unknown scope")


def _orphaned_assignments(ctx: SignalContext) -> list[Finding]:
    """Assignments whose principal no longer exists.

    ARM keeps the role assignment when a principal is deleted; the portal renders it as
    "Identity not found". Aggregated per scope — one finding per row produced 1,262 "patterns"
    in the Entra work before aggregation."""
    ctx.require(
        any(r.get("principalExists") != schema.EXISTS_UNKNOWN for r in ctx.rows),
        "The directory could not be read, so a principal that does not resolve cannot be called deleted.",
    )
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if r.get("principalExists") == schema.EXISTS_FALSE:
            by_scope[str(r.get("scope") or "")].append(r)
    out: list[Finding] = []
    for scope, rows in by_scope.items():
        ids = sorted({str(r.get("effectivePrincipalId") or r.get("principalId")) for r in rows})
        out.append(
            Finding(
                signal_id="hyg.orphaned_assignment",
                title="Role assignments for principals that no longer exist",
                severity="warning",
                pillar="hyg",
                object_kind="scope",
                subject=scope,
                subject_label=_scope_label(rows[0]),
                detail=(
                    f"{len(rows)} assignment(s) on {_scope_label(rows[0])} reference principals "
                    f"that no longer resolve in the directory. Azure keeps the assignment when the "
                    f"principal is deleted, so these inflate every count and every export."
                ),
                count=len(rows),
                evidence={"principal_ids": ids[:10], "total": len(ids)},
                remediation="Delete the assignments. There is no principal left to lose access.",
                frameworks=("NIST:AC-2", "CIS-Azure:1.3"),
            )
        )
    return out


def _privileged_orphans(ctx: SignalContext) -> list[Finding]:
    """The same thing, but the dead principal held privilege — worth its own severity."""
    ctx.require(
        any(r.get("principalExists") != schema.EXISTS_UNKNOWN for r in ctx.rows),
        "The directory could not be read, so orphaned principals cannot be identified.",
    )
    rows = [
        r for r in ctx.grants
        if r.get("principalExists") == schema.EXISTS_FALSE and r.get("roleIsPrivileged")
    ]
    if not rows:
        return []
    return [
        Finding(
            signal_id="hyg.privileged_orphan",
            title="Privileged assignments held by deleted principals",
            severity="error",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(rows)} privileged assignment(s) reference principals that no longer exist. "
                f"An object id can be re-created against a recycled application, which would hand "
                f"the new object the old privilege."
            ),
            count=len(rows),
            evidence={"roles": sorted({str(r.get("roleName")) for r in rows})[:10]},
            remediation="Delete these assignments before anything can inherit them.",
            frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3"),
        )
    ]


def _unresolved_principals(ctx: SignalContext) -> list[Finding]:
    """Rows the product could not name.

    A raw GUID where a name belongs is a product/permission problem, not a finding about the
    tenant — so this is `info`, and it exists to stop the grid quietly rendering GUIDs with no
    explanation."""
    unknown = [
        r for r in ctx.grants
        if r.get("principalExists") == schema.EXISTS_UNKNOWN
        and (r.get("effectivePrincipalId") or r.get("principalId"))
        and not (r.get("effectivePrincipalName") or r.get("principalDisplayName"))
    ]
    if not unknown:
        return []
    return [
        Finding(
            signal_id="hyg.unresolved_principals",
            title="Some principals could not be resolved to a name",
            severity="info",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(unknown)} row(s) show a bare object id because the directory lookup did not "
                f"cover them. They are NOT necessarily orphaned — until the directory can be read, "
                f"whether the principal exists is unknown."
            ),
            count=len(unknown),
            evidence={"sample": sorted({str(r.get("effectivePrincipalId") or r.get("principalId")) for r in unknown})[:10]},
            remediation="Grant the connection Microsoft Graph directory read access and refresh the directory layer.",
        )
    ]


def _stale_scopes(ctx: SignalContext) -> list[Finding]:
    """Scopes whose cached slice is old enough that decisions made on it are questionable."""
    stale = [s for s in ctx.scopes if s.get("stale")]
    if not stale:
        return []
    return [
        Finding(
            signal_id="hyg.stale_scan",
            title="Some scopes have not been rescanned recently",
            severity="info",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(stale)} scope(s) are past their cache TTL. Findings for those scopes "
                f"describe the estate as it was, not as it is."
            ),
            count=len(stale),
            evidence={"scopes": [str(s.get("displayName") or s.get("scope")) for s in stale][:10]},
            remediation="Refresh the affected scopes from the Overview or Scopes tab.",
        )
    ]


def _collectors_needing_attention(ctx: SignalContext) -> list[Finding]:
    """A blocked collector must say why, never silently report zero.

    This is the signal that keeps every other number on the screen honest: if a surface could
    not be read, the reader has to know before they trust an "all clear"."""
    blocked: dict[str, list[str]] = defaultdict(list)
    for meta in ctx.scopes:
        for c in meta.get("collectors", []) or []:
            if c.get("status") in schema.ATTENTION_STATUSES:
                blocked[str(c.get("collector"))].append(str(meta.get("displayName") or meta.get("scope")))
    out: list[Finding] = []
    for collector, scopes in blocked.items():
        out.append(
            Finding(
                signal_id="hyg.collector_blocked",
                title="A collector could not read part of the estate",
                severity="warning",
                pillar="hyg",
                object_kind="tenant",
                subject=collector,
                subject_label=collector,
                detail=(
                    f"{collector} could not complete on {len(scopes)} scope(s). Anything that "
                    f"surface would have reported is missing from this screen — absence of a "
                    f"finding there is not evidence of absence."
                ),
                count=len(scopes),
                evidence={"scopes": scopes[:10]},
                remediation="Check Diagnostics for the exact error, then grant the missing permission and refresh.",
            )
        )
    return out


# --------------------------------------------------------------------------- disabled access
def _state_measured(ctx: SignalContext) -> bool:
    """Was Entra account state actually collected for this tenant?

    The gate on every check below. A cache written before the account-state collector existed
    contains no disabled principals at all, and "no disabled principal holds access" is the most
    reassuring sentence in this whole feature — so it must never be produced by not having
    looked. Same rule that stopped `standing_ratio` reporting 100% standing privilege on tenants
    where PIM had simply never been collected."""
    return bool(ctx.directory.get("principal_state"))


_DISABLED_REASON = (
    "Entra account state was not collected for this tenant, so a principal cannot be called "
    "disabled. Refresh the directory layer with a connection that can read Microsoft Graph."
)


def _disabled_grants(ctx: SignalContext) -> list[dict[str, Any]]:
    return [r for r in ctx.grants if schema.is_disabled(r)]


def _disabled_principal_access(ctx: SignalContext) -> list[Finding]:
    """Access still held by principals whose Entra account is disabled.

    Disabling an account does not revoke a single role assignment. Azure keeps every one of
    them, so the access is not gone — it is dormant, and re-enabling the account (a helpdesk
    action, with no approval and no access review) restores all of it instantly.

    Aggregated per PRINCIPAL rather than per scope, because the unit of remediation here is a
    person, not a scope: you offboard Mallory once, not once per subscription."""
    ctx.require(_state_measured(ctx), _DISABLED_REASON)
    by_principal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _disabled_grants(ctx):
        pid = str(r.get("effectivePrincipalId") or r.get("principalId") or "")
        if pid:
            by_principal[pid].append(r)
    out: list[Finding] = []
    for pid, rows in by_principal.items():
        privileged = sum(1 for r in rows if r.get("roleIsPrivileged"))
        on_prem = any(str(r.get("principalOnPremSynced")) == schema.ENABLED_TRUE for r in rows)
        scopes = sorted({str(r.get("scopeDisplayName") or r.get("scope") or "") for r in rows})
        out.append(
            Finding(
                signal_id="hyg.disabled_principal_access",
                title="A disabled account still holds access",
                severity="error" if privileged else "warning",
                pillar="hyg",
                object_kind="principal",
                subject=pid,
                subject_label=_who(rows[0]),
                detail=(
                    f"{_who(rows[0])} is disabled in Entra ID but still holds {len(rows)} "
                    f"assignment(s)"
                    + (f", {privileged} of them privileged" if privileged else "")
                    + ". Azure does not revoke role assignments when an account is disabled, so "
                    "re-enabling the account restores all of this access with no approval and "
                    "no access review."
                    + (
                        " This account is synchronised from on-premises Active Directory: it "
                        "must be remediated there, or the next sync cycle reverts the change."
                        if on_prem
                        else ""
                    )
                ),
                count=len(rows),
                evidence={
                    "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                    "scopes": scopes[:10],
                    "privileged": privileged,
                    "on_prem_synced": on_prem,
                    "upn": str(
                        rows[0].get("effectivePrincipalUserPrincipalName")
                        or rows[0].get("principalUserPrincipalName")
                        or ""
                    ),
                },
                remediation=(
                    "Remove the assignments as part of offboarding. If the account was disabled "
                    "on purpose and is expected back, the access should be re-granted on return, "
                    "not left standing."
                ),
                frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3", "ISO:A.5.18"),
            )
        )
    return out


def _disabled_privileged(ctx: SignalContext) -> list[Finding]:
    """The same population, narrowed to privileged roles and raised to its own severity.

    A disabled account holding Reader is untidy. A disabled account holding Owner is a dormant
    administrator that any helpdesk password reset re-arms."""
    ctx.require(_state_measured(ctx), _DISABLED_REASON)
    rows = [r for r in _disabled_grants(ctx) if r.get("roleIsPrivileged")]
    if not rows:
        return []
    people = sorted({str(r.get("effectivePrincipalId") or r.get("principalId")) for r in rows})
    return [
        Finding(
            signal_id="hyg.disabled_privileged_access",
            title="Disabled accounts still hold privileged roles",
            severity="error",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(people)} disabled principal(s) hold {len(rows)} privileged assignment(s). "
                f"Each one is a dormant administrator: enabling the account is a single helpdesk "
                f"action and reinstates the privilege in full."
            ),
            count=len(rows),
            evidence={
                "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                "principals": sorted({_who(r) for r in rows})[:10],
                "total_principals": len(people),
            },
            remediation="Remove these first — they are the highest-value dormant access in the tenant.",
            frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3"),
        )
    ]


def _disabled_via_group(ctx: SignalContext) -> list[Finding]:
    """Disabled members sitting inside groups that grant access.

    Its own signal because the REMEDIATION is different and getting it wrong breaks other
    people: the assignment belongs to the group and serves every other member, so the fix is to
    remove the disabled member from the group, never to delete the assignment. This is also the
    least visible case in the product — an assignment-centric view shows a healthy group holding
    a role, and nobody opens it."""
    ctx.require(_state_measured(ctx), _DISABLED_REASON)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _disabled_grants(ctx):
        if r.get("accessPath") == schema.PATH_GROUP and r.get("sourceGroupId"):
            by_group[str(r["sourceGroupId"])].append(r)
    out: list[Finding] = []
    for gid, rows in by_group.items():
        members = sorted({_who(r) for r in rows})
        privileged = sum(1 for r in rows if r.get("roleIsPrivileged"))
        gname = str(rows[0].get("sourceGroupName") or gid)
        out.append(
            Finding(
                signal_id="hyg.disabled_via_group",
                title="A group grants access to disabled accounts",
                severity="error" if privileged else "warning",
                pillar="hyg",
                object_kind="principal",
                subject=gid,
                subject_label=gname,
                detail=(
                    f"{len(members)} disabled account(s) are members of {gname}, which holds "
                    f"{len(rows)} grant(s). The group itself is healthy, so this access does not "
                    f"appear anywhere an assignment is listed — only the membership shows it."
                ),
                count=len(rows),
                evidence={
                    "members": members[:10],
                    "member_count": len(members),
                    "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                },
                remediation=(
                    "Remove the disabled members from the group. Do NOT delete the group's role "
                    "assignment — it serves every other member."
                ),
                frameworks=("NIST:AC-2(3)", "ISO:A.5.18"),
            )
        )
    return out


def _disabled_owns_credential(ctx: SignalContext) -> list[Finding]:
    """Disabled accounts that own a service principal.

    The only case in this family that is exploitable **right now**. Everything else here is
    dormant: a disabled user cannot obtain a token, so their own grants are one re-enable away
    from being live but are not live today. An owned service principal is different — it
    authenticates with its own secret or certificate, which disabling the owner's account does
    nothing to. If the owner left under a cloud, or their credentials were the reason the
    account was disabled, that secret is still working."""
    ctx.require(_state_measured(ctx), _DISABLED_REASON)
    rows = [
        r for r in _disabled_grants(ctx)
        if r.get("accessPath") == schema.PATH_OWNER
    ]
    if not rows:
        return []
    owners = sorted({_who(r) for r in rows})
    return [
        Finding(
            signal_id="hyg.disabled_owns_credential",
            title="Disabled accounts own service principals",
            severity="error",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(owners)} disabled account(s) own {len(rows)} service principal(s). A "
                f"service principal signs in with its own credential, so disabling the owner's "
                f"user account does not stop it. Unlike the rest of this pillar, this access is "
                f"live now rather than one re-enable away."
            ),
            count=len(rows),
            evidence={
                "owners": owners[:10],
                "service_principals": sorted({str(r.get("principalDisplayName")) for r in rows})[:10],
            },
            remediation=(
                "Reassign ownership to a current employee, then roll the service principal's "
                "credentials. Removing the owner alone leaves the existing secret valid."
            ),
            frameworks=("NIST:AC-2(3)", "NIST:IA-5"),
        )
    ]


def _soft_deleted_restorable(ctx: SignalContext) -> list[Finding]:
    """Principals in the Entra ID recycle bin that still hold access.

    Its own signal because the ADVICE elsewhere is wrong for these. ``hyg.orphaned_assignment``
    tells an operator to delete the assignment because "there is no principal left to lose
    access" — true for a hard deletion, false for the 30 days a soft-deleted object can be
    restored by any administrator, which brings every one of these grants back at once. That
    window is also exactly when an offboarding is most likely to be reversed."""
    state = ctx.directory.get("principal_state") or {}
    ctx.require(bool(state), _DISABLED_REASON)
    bin_ids = {
        pid for pid, s in state.items()
        if isinstance(s, dict) and s.get("deletedDateTime")
    }
    if not bin_ids:
        return []
    rows = [
        r for r in ctx.grants
        if str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() in bin_ids
    ]
    if not rows:
        return []
    privileged = sum(1 for r in rows if r.get("roleIsPrivileged"))
    people = sorted({
        str(r.get("effectivePrincipalId") or r.get("principalId")).lower() for r in rows
    })
    return [
        Finding(
            signal_id="hyg.deleted_principal_restorable",
            title="Deleted accounts in the recycle bin still hold access",
            severity="error" if privileged else "warning",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(people)} principal(s) holding {len(rows)} grant(s)"
                + (f", {privileged} of them privileged" if privileged else "")
                + " are in the Entra ID recycle bin. They are recoverable for 30 days, and "
                "restoring the object restores all of this access at once — so these must not "
                "be treated as harmless orphans."
            ),
            count=len(rows),
            evidence={
                "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                "principals": sorted({_who(r) for r in rows})[:10],
            },
            remediation=(
                "Remove the assignments now rather than waiting for the retention window to "
                "expire. If the account is restored, the access returns with it."
            ),
            frameworks=("NIST:AC-2(3)", "ISO:A.5.18"),
        )
    ]


def _disabled_eligible(ctx: SignalContext) -> list[Finding]:
    """Disabled accounts that are still PIM-eligible.

    An eligibility is not access, which is why it gets its own, lower severity — but it is a
    standing invitation that survives offboarding entirely, and a permanent eligibility survives
    it forever."""
    ctx.require(_state_measured(ctx), _DISABLED_REASON)
    rows = [
        r for r in _disabled_grants(ctx)
        if r.get("assignmentState") == schema.STATE_ELIGIBLE
    ]
    if not rows:
        return []
    permanent = sum(1 for r in rows if r.get("isPermanentEligible"))
    return [
        Finding(
            signal_id="hyg.disabled_pim_eligible",
            title="Disabled accounts are still eligible to activate roles",
            severity="warning",
            pillar="hyg",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(rows)} PIM eligibility/eligibilities belong to disabled accounts"
                + (f", {permanent} of them permanent" if permanent else "")
                + ". Eligibility is not access — but it outlives offboarding, and re-enabling "
                "the account makes it activatable again."
            ),
            count=len(rows),
            evidence={
                "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                "principals": sorted({_who(r) for r in rows})[:10],
                "permanent": permanent,
            },
            remediation="Remove the eligibility as part of offboarding, alongside active assignments.",
            frameworks=("NIST:AC-2(3)",),
        )
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="hyg.orphaned_assignment",
        title="Assignments for principals that no longer exist",
        pillar="hyg", severity="warning", weight=7, object_kind="scope",
        why="Azure keeps role assignments when the principal is deleted; they inflate every count and every export.",
        remediation="Delete them — there is no principal left to lose access.",
        frameworks=("NIST:AC-2", "CIS-Azure:1.3"),
        evaluate=_orphaned_assignments,
    ),
    SignalSpec(
        id="hyg.privileged_orphan",
        title="Privileged assignments held by deleted principals",
        pillar="hyg", severity="error", weight=9, object_kind="tenant",
        why="An object id can be re-created against a recycled application and inherit the old privilege.",
        remediation="Delete these first.",
        frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3"),
        evaluate=_privileged_orphans,
    ),
    SignalSpec(
        id="hyg.unresolved_principals",
        title="Principals that could not be resolved to a name",
        pillar="hyg", severity="info", weight=2, object_kind="tenant",
        why="A bare GUID in the grid is a permissions gap, not a finding about the tenant — and must not be mistaken for one.",
        remediation="Grant Microsoft Graph directory read and refresh the directory layer.",
        evaluate=_unresolved_principals,
    ),
    SignalSpec(
        id="hyg.stale_scan",
        title="Scopes not rescanned recently",
        pillar="hyg", severity="info", weight=2, object_kind="tenant",
        why="Findings from a stale slice describe the estate as it was.",
        remediation="Refresh the affected scopes.",
        evaluate=_stale_scopes,
    ),
    SignalSpec(
        id="hyg.collector_blocked",
        title="A collector could not read part of the estate",
        pillar="hyg", severity="warning", weight=5, object_kind="tenant",
        why="Absence of a finding on an unread surface is not evidence of absence.",
        remediation="Check Diagnostics, grant the missing permission, refresh.",
        evaluate=_collectors_needing_attention,
    ),
    SignalSpec(
        id="hyg.disabled_principal_access",
        title="Disabled accounts that still hold access",
        pillar="hyg", severity="warning", weight=8, object_kind="principal",
        why="Azure keeps every role assignment when an account is disabled; re-enabling restores all of it with no approval.",
        remediation="Remove the assignments as part of offboarding.",
        frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3", "ISO:A.5.18"),
        evaluate=_disabled_principal_access,
    ),
    SignalSpec(
        id="hyg.disabled_privileged_access",
        title="Disabled accounts holding privileged roles",
        pillar="hyg", severity="error", weight=10, object_kind="tenant",
        why="A dormant administrator that a single helpdesk action re-arms.",
        remediation="Remove these first.",
        frameworks=("NIST:AC-2(3)", "CIS-Azure:1.3"),
        evaluate=_disabled_privileged,
    ),
    SignalSpec(
        id="hyg.disabled_via_group",
        title="Groups granting access to disabled accounts",
        pillar="hyg", severity="warning", weight=6, object_kind="principal",
        why="The assignment belongs to a healthy group, so this access is invisible in every assignment-centric view.",
        remediation="Remove the disabled members from the group; never delete the group's assignment.",
        frameworks=("NIST:AC-2(3)", "ISO:A.5.18"),
        evaluate=_disabled_via_group,
    ),
    SignalSpec(
        id="hyg.disabled_owns_credential",
        title="Disabled accounts owning service principals",
        pillar="hyg", severity="error", weight=9, object_kind="tenant",
        why="A service principal has its own credential; disabling its owner does not stop it. This access is live now.",
        remediation="Reassign ownership, then roll the credential.",
        frameworks=("NIST:AC-2(3)", "NIST:IA-5"),
        evaluate=_disabled_owns_credential,
    ),
    SignalSpec(
        id="hyg.disabled_pim_eligible",
        title="Disabled accounts still eligible to activate roles",
        pillar="hyg", severity="warning", weight=4, object_kind="tenant",
        why="Eligibility outlives offboarding and becomes activatable the moment the account is re-enabled.",
        remediation="Remove the eligibility alongside active assignments.",
        frameworks=("NIST:AC-2(3)",),
        evaluate=_disabled_eligible,
    ),
    SignalSpec(
        id="hyg.deleted_principal_restorable",
        title="Recycle-bin accounts that still hold access",
        pillar="hyg", severity="warning", weight=6, object_kind="tenant",
        why="A soft-deleted object is restorable for 30 days, and restoring it restores every grant it held.",
        remediation="Remove the assignments now; do not wait for the retention window.",
        frameworks=("NIST:AC-2(3)", "ISO:A.5.18"),
        evaluate=_soft_deleted_restorable,
    ),
]
