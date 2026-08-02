"""Governance signals (pillar weight 8).

Governance coverage is computed from the ACCESS inventory, not from governance data — so it
still renders on a tenant that has no review process at all. That was the single best property
of the equivalent Entra pillar: on a tenant where access reviews were unreadable it still
reported how many objects were governed by nothing.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import schema
from app.iam.signals import Finding, SignalContext, SignalSpec


def _scope_label(row: dict[str, Any]) -> str:
    return str(row.get("scopeDisplayName") or row.get("subscriptionName") or row.get("scope") or "unknown scope")


def _privileged_scope_unowned(ctx: SignalContext) -> list[Finding]:
    """Scopes with privileged access and no recorded owner.

    Nobody can certify access to a scope nobody owns — the review has no addressee. Ownership is
    an optional feature, so this reports *not measured* rather than flagging the whole estate
    when the ownership registry has never been populated."""
    try:
        from app.ownership import registry as ownership_registry
    except Exception as exc:  # noqa: BLE001 - ownership is optional
        raise Exception(f"ownership unavailable: {exc}") from exc

    owned: set[str] = set()
    try:
        for entry in ownership_registry.list_assignments(ctx.tenant_id):  # type: ignore[attr-defined]
            rid = str(entry.get("resource_id") or entry.get("scope") or "").lower()
            if rid:
                owned.add(rid)
    except Exception:  # noqa: BLE001
        owned = set()

    ctx.require(bool(owned), "No ownership has been recorded, so unowned scopes cannot be distinguished.")

    priv_scopes: dict[str, dict[str, Any]] = {}
    for r in ctx.grants:
        if not r.get("roleIsPrivileged"):
            continue
        scope = str(r.get("scope") or "")
        if scope and scope.lower() not in owned:
            priv_scopes.setdefault(scope, r)
    return [
        Finding(
            signal_id="gov.privileged_scope_unowned",
            title="Privileged access to a scope nobody owns",
            severity="warning",
            pillar="gov",
            object_kind="scope",
            subject=scope,
            subject_label=_scope_label(row),
            detail=(
                f"{_scope_label(row)} has privileged assignments but no recorded owner, so an "
                f"access review of it has no addressee."
            ),
            evidence={"scope": scope},
            remediation="Record an owner for the scope so its access can be certified.",
            frameworks=("NIST:AC-2", "ISO:A.5.18"),
        )
        for scope, row in priv_scopes.items()
    ]


def _ungoverned_privileged_principals(ctx: SignalContext) -> list[Finding]:
    """Privileged principals whose access is neither PIM-governed nor group-managed.

    This is computed entirely from the access inventory, so it renders on a tenant with no
    governance tooling at all — which is exactly the tenant that needs the number."""
    direct_standing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if not r.get("roleIsPrivileged"):
            continue
        if r.get("assignmentState") == schema.STATE_ELIGIBLE:
            continue          # governed by PIM
        if r.get("accessPath") == schema.PATH_GROUP:
            continue          # governed by group membership
        if r.get("pimManaged"):
            continue
        direct_standing[str(r.get("effectivePrincipalId") or "")].append(r)
    direct_standing.pop("", None)
    if not direct_standing:
        return []
    total_priv = len({str(r.get("effectivePrincipalId")) for r in ctx.grants if r.get("roleIsPrivileged")})
    return [
        Finding(
            signal_id="gov.ungoverned_privileged_access",
            title="Privileged access governed by nothing",
            severity="warning",
            pillar="gov",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(direct_standing)} of {total_priv} privileged principals hold their access "
                f"as a permanent, direct assignment — not through PIM and not through a group. "
                f"Nothing about that access expires, and nothing reviews it on a cadence."
            ),
            count=len(direct_standing),
            evidence={"principals": sorted(direct_standing)[:10], "total_privileged": total_priv},
            remediation="Move these onto PIM eligibility or group-based assignment so the access has a lifecycle.",
            frameworks=("NIST:AC-2", "ISO:A.5.18", "MCSB:PA-2"),
        )
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="gov.privileged_scope_unowned",
        title="Privileged access to an unowned scope",
        pillar="gov", severity="warning", weight=6, object_kind="scope",
        why="A review of a scope nobody owns has no addressee.",
        remediation="Record an owner so the scope's access can be certified.",
        frameworks=("NIST:AC-2", "ISO:A.5.18"),
        evaluate=_privileged_scope_unowned,
    ),
    SignalSpec(
        id="gov.ungoverned_privileged_access",
        title="Privileged access governed by nothing",
        pillar="gov", severity="warning", weight=8, object_kind="tenant",
        why="Permanent, direct privilege has no expiry and no review cadence attached to it.",
        remediation="Move onto PIM eligibility or group-based assignment.",
        frameworks=("NIST:AC-2", "ISO:A.5.18", "MCSB:PA-2"),
        evaluate=_ungoverned_privileged_principals,
    ),
]
