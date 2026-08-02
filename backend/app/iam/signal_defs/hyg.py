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
]
