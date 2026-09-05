"""Structure signals (pillar weight 4) — the limits and shapes that break deployments.

Small pillar, but when it fires it fires hard: an Azure assignment limit is not a warning, it is
a deployment that fails at 2am with an opaque error.
"""
from __future__ import annotations

from collections import defaultdict

from app.iam import schema
from app.iam.signals import Finding, SignalContext, SignalSpec

# Documented Azure limits.
MAX_ASSIGNMENTS_PER_SUBSCRIPTION = 4000
MAX_ASSIGNMENTS_PER_MANAGEMENT_GROUP = 500

_WARN_AT = 0.80
_ERROR_AT = 0.95


def _headroom(ctx: SignalContext) -> list[Finding]:
    """Role-assignment count against the documented per-scope ceiling.

    Nobody watches this until a deployment fails. Counting only DIRECT assignments is deliberate:
    group-expanded rows are effective access, not stored assignments, and would inflate the
    figure against a limit that counts stored objects."""
    per_sub: dict[str, set[str]] = defaultdict(set)
    per_mg: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    for r in ctx.rows:
        if r.get("accessPath") != schema.PATH_DIRECT:
            continue
        aid = str(r.get("assignmentId") or "")
        if not aid or r.get("surface") != schema.SURFACE_AZURE_RBAC:
            continue
        sub = str(r.get("subscriptionId") or "")
        mg = str(r.get("managementGroupId") or "")
        if r.get("scopeType") == schema.SCOPE_MANAGEMENT_GROUP and mg:
            per_mg[mg].add(aid)
            labels.setdefault(mg, str(r.get("managementGroupName") or mg))
        elif sub:
            per_sub[sub].add(aid)
            labels.setdefault(sub, str(r.get("subscriptionName") or sub))

    out: list[Finding] = []
    for bucket, limit, kind in ((per_sub, MAX_ASSIGNMENTS_PER_SUBSCRIPTION, "subscription"),
                                (per_mg, MAX_ASSIGNMENTS_PER_MANAGEMENT_GROUP, "management group")):
        for key, ids in bucket.items():
            used = len(ids)
            pct = used / limit
            if pct < _WARN_AT:
                continue
            out.append(
                Finding(
                    signal_id="str.assignment_limit_headroom",
                    title=f"Role-assignment limit nearly reached on a {kind}",
                    severity="error" if pct >= _ERROR_AT else "warning",
                    pillar="str",
                    object_kind="scope",
                    subject=key,
                    subject_label=labels.get(key, key),
                    detail=(
                        f"{used} of {limit} role assignments used ({round(pct * 100)}%) on this "
                        f"{kind}. At the ceiling, new assignments fail — including the ones a "
                        f"deployment makes for itself."
                    ),
                    count=used,
                    evidence={"used": used, "limit": limit, "percent": round(pct * 100)},
                    remediation="Replace clusters of identical direct assignments with a single group assignment.",
                    frameworks=("MCSB:PA-7",),
                )
            )
    return out


def _scope_hierarchy_flat(ctx: SignalContext) -> list[Finding]:
    """Every subscription hanging directly off the tenant root.

    A flat hierarchy means access cannot be granted to an environment — only to the whole estate
    or to one subscription at a time, which is what drives the wide-scope assignments the
    least-privilege pillar keeps finding."""
    ctx.require(bool(ctx.scopes), "No scopes have been scanned.")
    mgs = {s.get("managementGroupId") for s in ctx.scopes if s.get("managementGroupId")}
    subs = [s for s in ctx.scopes if s.get("scopeType") == schema.SCOPE_SUBSCRIPTION]
    if len(subs) < 3 or len(mgs) > 1:
        return []
    return [
        Finding(
            signal_id="str.flat_scope_hierarchy",
            title="Subscriptions are not organized under management groups",
            severity="info",
            pillar="str",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(subs)} subscriptions with {len(mgs) or 'no'} management group(s). Without a "
                f"hierarchy, access can only be granted per subscription or across everything, "
                f"which is what pushes teams towards over-broad assignments."
            ),
            count=len(subs),
            evidence={"subscriptions": len(subs), "management_groups": len(mgs)},
            remediation="Group subscriptions by environment or business unit and assign access at that level.",
            frameworks=("MCSB:PA-7",),
        )
    ]


def _demo_data_present(ctx: SignalContext) -> list[Finding]:
    """Synthetic data in the cache.

    Not a security finding — a *trust* one. Every number on the screen is partly fictional while
    this is true, and a reader who does not know that will act on it."""
    demo_scopes = [s for s in ctx.scopes if s.get("demo")]
    if not demo_scopes:
        return []
    return [
        Finding(
            signal_id="str.demo_data_present",
            title="Demo data is mixed into this tenant's access review",
            severity="info",
            pillar="str",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{len(demo_scopes)} scope(s) contain synthetic demo data. Counts, findings and "
                f"exports on this screen include rows that do not exist in Azure."
            ),
            count=len(demo_scopes),
            evidence={"scopes": [str(s.get("displayName")) for s in demo_scopes][:10]},
            remediation="Remove the demo dataset from the Overview tab before reviewing real access.",
        )
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="str.assignment_limit_headroom",
        title="Role-assignment limit headroom",
        pillar="str", severity="warning", weight=8, object_kind="scope",
        why="At the ceiling new assignments fail, including the ones deployments make for themselves.",
        remediation="Replace identical direct assignments with a group assignment.",
        frameworks=("MCSB:PA-7",),
        evaluate=_headroom,
    ),
    SignalSpec(
        id="str.flat_scope_hierarchy",
        title="Subscriptions not organised under management groups",
        pillar="str", severity="info", weight=3, object_kind="tenant",
        why="Without a hierarchy, access can only be granted per subscription or across everything.",
        remediation="Group subscriptions by environment and assign access at that level.",
        frameworks=("MCSB:PA-7",),
        evaluate=_scope_hierarchy_flat,
    ),
    SignalSpec(
        id="str.demo_data_present",
        title="Demo data present in the review",
        pillar="str", severity="info", weight=2, object_kind="tenant",
        why="Every number on the screen is partly fictional while this is true.",
        remediation="Remove the demo dataset before reviewing real access.",
        evaluate=_demo_data_present,
    ),
]
