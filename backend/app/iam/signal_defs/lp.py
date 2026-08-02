"""Least-privilege signals (pillar weight 10).

Usage-based right-sizing needs the Activity Log and arrives in a later phase; what is decidable
from a snapshot alone is *shape*: how broad the scope is, how blunt the role is, and how many
identical direct assignments should have been one group.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import schema
from app.iam.signals import Finding, SignalContext, SignalSpec

# Roles that grant far more than most workloads need.
_BLUNT_ROLES = frozenset({"owner", "contributor"})
# N identical direct assignments at one scope that should have been one group.
_GROUP_CLUSTER = 5


def _who(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


def _scope_label(row: dict[str, Any]) -> str:
    return str(row.get("scopeDisplayName") or row.get("subscriptionName") or row.get("scope") or "unknown scope")


def _blunt_role_high_scope(ctx: SignalContext) -> list[Finding]:
    """Owner/Contributor at management-group or tenant-root scope.

    The scope multiplies the role: Contributor on one resource group is ordinary; Contributor
    across every subscription under a management group is a different fact entirely."""
    wide = {schema.SCOPE_MANAGEMENT_GROUP, schema.SCOPE_TENANT}
    rows = [
        r for r in ctx.grants
        if r.get("scopeType") in wide
        and str(r.get("roleName", "")).strip().lower() in _BLUNT_ROLES
        and r.get("assignmentState") != schema.STATE_ELIGIBLE
    ]
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_scope[str(r.get("scope"))].append(r)
    return [
        Finding(
            signal_id="lp.blunt_role_wide_scope",
            title="Broad role assigned at a very wide scope",
            severity="error",
            pillar="lp",
            object_kind="scope",
            subject=scope,
            subject_label=_scope_label(rs[0]),
            detail=(
                f"{len(rs)} permanent Owner/Contributor assignment(s) at "
                f"{_scope_label(rs[0])}. Everything beneath this scope inherits them, so the "
                f"grant is as wide as the hierarchy, not as wide as the job."
            ),
            count=len(rs),
            evidence={"principals": sorted({_who(r) for r in rs})[:10]},
            remediation="Move the assignment down to the narrowest scope that covers the work.",
            frameworks=("NIST:AC-6", "CIS-Azure:1.21", "MCSB:PA-7"),
        )
        for scope, rs in by_scope.items()
    ]


def _direct_assignment_clusters(ctx: SignalContext) -> list[Finding]:
    """Many principals holding the same role at the same scope by direct assignment.

    N assignments where 1 would do, and N revocations to perform when someone leaves. Also the
    main lever for assignment-limit headroom."""
    clusters: dict[tuple[str, str], set[str]] = defaultdict(set)
    labels: dict[tuple[str, str], dict[str, Any]] = {}
    for r in ctx.grants:
        if r.get("accessPath") != schema.PATH_DIRECT:
            continue
        ptype = str(r.get("effectivePrincipalType") or r.get("principalType") or "").lower()
        if ptype not in ("user", ""):
            continue  # grouping service principals for unrelated workloads couples them
        key = (str(r.get("scope")), str(r.get("roleName")))
        clusters[key].add(str(r.get("effectivePrincipalId") or _who(r)))
        labels.setdefault(key, r)
    out: list[Finding] = []
    for (scope, role), principals in clusters.items():
        if len(principals) < _GROUP_CLUSTER:
            continue
        row = labels[(scope, role)]
        out.append(
            Finding(
                signal_id="lp.direct_assignment_cluster",
                title="Many identical direct assignments that should be one group",
                severity="info",
                pillar="lp",
                object_kind="scope",
                subject=f"{scope}|{role}",
                subject_label=f"{role} on {_scope_label(row)}",
                detail=(
                    f"{len(principals)} users hold {role} on {_scope_label(row)} by direct "
                    f"assignment. One group assignment would replace all of them, and would mean "
                    f"one revocation when somebody leaves instead of {len(principals)}."
                ),
                count=len(principals),
                evidence={"principals": sorted(principals)[:10], "role": role},
                remediation=f"Create a group, assign {role} once, and move the members into it.",
                frameworks=("NIST:AC-2", "MCSB:PA-7"),
            )
        )
    return out


def _data_plane_breadth(ctx: SignalContext) -> list[Finding]:
    """Data-plane roles at subscription scope or wider.

    Control-plane breadth is visible; data-plane breadth is not, because the portal shows it in
    the same list. A Storage Blob Data Contributor at subscription scope reads every blob in
    every account in that subscription, present and future."""
    wide = {schema.SCOPE_SUBSCRIPTION, schema.SCOPE_MANAGEMENT_GROUP, schema.SCOPE_TENANT}
    rows = [r for r in ctx.grants if r.get("roleHasDataActions") and r.get("scopeType") in wide]
    if not rows:
        return []
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_scope[str(r.get("scope"))].append(r)
    return [
        Finding(
            signal_id="lp.data_plane_breadth",
            title="Data-plane role granted across a whole subscription or wider",
            severity="warning",
            pillar="lp",
            object_kind="scope",
            subject=scope,
            subject_label=_scope_label(rs[0]),
            detail=(
                f"{len(rs)} data-plane assignment(s) at {_scope_label(rs[0])}. These reach the "
                f"contents of every matching resource in scope — including resources created "
                f"after the grant was made."
            ),
            count=len(rs),
            evidence={"roles": sorted({str(r.get("roleName")) for r in rs})[:10]},
            remediation="Scope data-plane roles to the specific account, container or vault.",
            frameworks=("NIST:AC-6", "MCSB:PA-7"),
        )
        for scope, rs in by_scope.items()
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="lp.blunt_role_wide_scope",
        title="Broad role at a very wide scope",
        pillar="lp", severity="error", weight=8, object_kind="scope",
        why="The scope multiplies the role: everything beneath a management group inherits it.",
        remediation="Move the assignment down to the narrowest scope that covers the work.",
        frameworks=("NIST:AC-6", "CIS-Azure:1.21", "MCSB:PA-7"),
        evaluate=_blunt_role_high_scope,
    ),
    SignalSpec(
        id="lp.direct_assignment_cluster",
        title="Identical direct assignments that should be a group",
        pillar="lp", severity="info", weight=4, object_kind="scope",
        why="N assignments where 1 would do, and N revocations when somebody leaves.",
        remediation="Create a group, assign the role once, move the members into it.",
        frameworks=("NIST:AC-2", "MCSB:PA-7"),
        evaluate=_direct_assignment_clusters,
    ),
    SignalSpec(
        id="lp.data_plane_breadth",
        title="Data-plane role across a whole subscription",
        pillar="lp", severity="warning", weight=7, object_kind="scope",
        why="Data-plane breadth is invisible in the portal's list and reaches resources created after the grant.",
        remediation="Scope data-plane roles to the specific account, container or vault.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_data_plane_breadth,
    ),
]
