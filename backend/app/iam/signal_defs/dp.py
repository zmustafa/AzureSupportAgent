"""Data-plane access signals (pillar weight 8).

Azure RBAC is not the authorization system for most of the data in a tenant, and the two
questions this pillar answers are the ones a control-plane review cannot:

* **Who can reach the data itself** — and in particular who can reach a CREDENTIAL. Reading a
  Key Vault secret is not a read; it is becoming the identity that secret authenticates, with
  everything that identity can reach. `Key Vault Secrets User` looks unremarkable in a role list
  and is one of the most powerful grants in a tenant.

* **Where we cannot see at all.** SQL GRANTs, Kubernetes RoleBindings, Cosmos native role
  assignments, Databricks/Unity Catalog, Managed HSM local RBAC, Redis ACLs, Kusto database
  principals — these decide who reads the data and none of them are visible from an ARM/Graph
  connection. A tenant running twelve SQL databases and an AKS cluster has a large fraction of
  its data governed by systems this product has not read, and an access review that does not
  SAY so is worse than no review: it produces a clean page that a reader will believe.

The tiers come from :mod:`app.iam.dataplane`, derived from each role's actual ``dataActions``
rather than from its name.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import dataplane as dp, schema
from app.iam.signals import Finding, SignalContext, SignalSpec

#: Scopes at which a data-plane grant reaches resources that do not exist yet.
_WIDE_SCOPES = frozenset({
    schema.SCOPE_SUBSCRIPTION, schema.SCOPE_MANAGEMENT_GROUP, schema.SCOPE_TENANT,
})


def _who(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


def _scope_label(row: dict[str, Any]) -> str:
    return str(
        row.get("scopeDisplayName") or row.get("subscriptionName") or row.get("scope")
        or "unknown scope"
    )


def _role_defs(ctx: SignalContext) -> dict[str, list[str]]:
    """role name (lowercased) -> its dataActions, from the collected catalog."""
    out: dict[str, list[str]] = {}
    for rd in (ctx.directory.get("role_defs") or []):
        name = str(rd.get("roleName", "")).strip().lower()
        if not name:
            continue
        actions = rd.get("dataActions")
        if actions is None:
            actions = []
            for perm in (rd.get("permissions") or []):
                actions += list(perm.get("dataActions") or [])
        out[name] = [str(a) for a in actions]
    return out


def _tiered_grants(ctx: SignalContext) -> list[tuple[dict[str, Any], str]]:
    """Every granting row paired with the tier of the role it carries.

    Requires the role catalog: without dataActions there is no way to tell a data-plane role
    from a control-plane one, and guessing from the name is exactly what this pillar replaces."""
    defs = _role_defs(ctx)
    ctx.require(
        bool(defs),
        "The role catalogue was not collected, so no role can be resolved to the data it "
        "reaches. Data-plane exposure has NOT been assessed.",
    )
    out: list[tuple[dict[str, Any], str]] = []
    for row in ctx.grants:
        name = str(row.get("roleName", "")).strip().lower()
        tier = dp.role_tier(name, defs.get(name))
        if tier in dp.TIER_SENSITIVE:
            out.append((row, tier))
    return out


def _credential_access(ctx: SignalContext) -> list[Finding]:
    """Standing access to secrets, keys and certificates.

    Separated from every other data role because the consequence is different in kind. Reading a
    secret does not disclose data — it hands over an identity, and from there everything that
    identity can reach. This is the shortest escalation path in most tenants and it never shows
    up in a review that looks at control-plane roles."""
    rows = [(r, t) for r, t in _tiered_grants(ctx) if t == dp.TIER_CREDENTIAL]
    if not rows:
        return []
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r, _t in rows:
        by_scope[str(r.get("scope"))].append(r)

    out: list[Finding] = []
    for scope, rs in by_scope.items():
        wide = rs[0].get("scopeType") in _WIDE_SCOPES
        principals = sorted({_who(r) for r in rs})
        out.append(Finding(
            signal_id="dp.credential_store_access",
            title="Standing access to secrets, keys or certificates",
            # A credential grant that spans a whole subscription reaches every vault in it,
            # including vaults created after the grant was made.
            severity="critical" if wide else "error",
            pillar="dp",
            object_kind="scope",
            subject=scope,
            subject_label=_scope_label(rs[0]),
            detail=(
                f"{len(principals)} principal(s) hold a role that can read or manage secrets, "
                f"keys or certificates at {_scope_label(rs[0])}"
                + (
                    " — a scope that covers every vault beneath it, including ones created "
                    "later." if wide else "."
                )
                + " Reading a secret is not a read of data: it grants the identity that secret "
                "authenticates, and everything that identity can reach."
            ),
            count=len(rs),
            evidence={
                "roles": sorted({str(r.get("roleName")) for r in rs})[:10],
                "principals": principals[:10],
                "scopeType": str(rs[0].get("scopeType", "")),
            },
            remediation=(
                "Scope credential roles to the individual vault, and make them eligible rather "
                "than permanent. Key Vault Reader grants metadata only and is often enough."
            ),
            frameworks=("NIST:AC-6", "MCSB:PA-7", "MCSB:IM-8"),
        ))
    return out


def _write_at_wide_scope(ctx: SignalContext) -> list[Finding]:
    """Data-modifying roles at subscription scope or wider."""
    rows = [
        (r, t) for r, t in _tiered_grants(ctx)
        if t == dp.TIER_WRITE and r.get("scopeType") in _WIDE_SCOPES
    ]
    if not rows:
        return []
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r, _t in rows:
        by_scope[str(r.get("scope"))].append(r)
    return [
        Finding(
            signal_id="dp.write_wide_scope",
            title="Data can be modified or destroyed across a whole subscription",
            severity="error",
            pillar="dp",
            object_kind="scope",
            subject=scope,
            subject_label=_scope_label(rs[0]),
            detail=(
                f"{len(rs)} assignment(s) at {_scope_label(rs[0])} can write or delete the "
                f"CONTENTS of matching resources — blobs, messages, indexes, images — not just "
                f"their configuration. Deleting data is not covered by a resource lock."
            ),
            count=len(rs),
            evidence={
                "roles": sorted({str(r.get("roleName")) for r in rs})[:10],
                "principals": sorted({_who(r) for r in rs})[:10],
            },
            remediation=(
                "Scope data-plane write roles to the specific account or container. Where the "
                "work is read-only, the matching Data Reader role exists for every service."
            ),
            frameworks=("NIST:AC-6", "MCSB:PA-7", "MCSB:DP-8"),
        )
        for scope, rs in by_scope.items()
    ]


def _unreadable_authorization(ctx: SignalContext) -> list[Finding]:
    """Services in this estate whose data authorization this product cannot read.

    Not a vulnerability — a COVERAGE statement, and the most important thing on this screen. A
    review that lists Azure role assignments for a tenant running SQL databases, an AKS cluster
    and a Cosmos account has described a minority of the access to that data. Reporting the gap
    is the difference between an access review and a reassuring page.

    Driven by resources actually observed in the estate, so it never lectures about services the
    tenant does not run."""
    seen = ctx.bypass_rows or []
    ctx.require(
        bool(seen) or ctx.bypass_assessed > 0,
        "The resource sweep has not run, so the services whose data authorization lives outside "
        "Azure RBAC could not be identified.",
    )

    # The sweep emits one row per CHECK, not per resource — four per storage account — so
    # resources must be deduplicated by id or every count here is a count of checks.
    by_service: dict[str, dict[str, str]] = defaultdict(dict)
    for res in seen:
        rid = str(res.get("resourceId") or "")
        name = str(res.get("resourceName") or rid)
        for svc in dp.services_for_type(str(res.get("resourceType", ""))):
            if not svc.rbac_is_complete:
                by_service[svc.key][rid or name] = name

    out: list[Finding] = []
    for key, resources in by_service.items():
        svc = dp.SERVICE_BY_KEY[key]
        names = sorted({n for n in resources.values() if n})
        count = len(resources)
        out.append(Finding(
            signal_id="dp.authorization_not_readable",
            title=f"{svc.label}: data access is not decided by Azure RBAC",
            # Deliberately a warning, not an error. Nothing is known to be WRONG; what is known
            # is that this product cannot tell, and a reader must not mistake silence for a pass.
            severity="warning",
            pillar="dp",
            object_kind="resource",
            subject=f"dataplane:{svc.key}",
            subject_label=svc.label,
            detail=(
                f"{count} {svc.label} resource(s) are present. {svc.blind_reason} "
                f"Nothing on this screen describes who can read that data, and an empty result "
                f"for this service means only that we did not look."
            ),
            count=count,
            evidence={
                "service": svc.key,
                "resources": names[:10],
                "other_doors": list(svc.doors)[:6],
            },
            remediation=(
                f"Review {svc.label} access in its own authorization system and record the "
                f"result outside this tool, or connect a credential that can read it."
            ),
            frameworks=("NIST:AC-2", "MCSB:PA-1"),
        ))
    return sorted(out, key=lambda f: dp.SERVICE_BY_KEY[str(f.evidence["service"])].priority)


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="dp.credential_store_access",
        title="Standing access to secrets, keys or certificates",
        pillar="dp", severity="critical", weight=10, object_kind="scope",
        why=(
            "Reading a secret is an identity takeover, not a read. It is the shortest escalation "
            "path in most tenants and it is invisible to a control-plane review."
        ),
        remediation="Scope credential roles to one vault and make them eligible, not permanent.",
        frameworks=("NIST:AC-6", "MCSB:PA-7", "MCSB:IM-8"),
        evaluate=_credential_access,
    ),
    SignalSpec(
        id="dp.write_wide_scope",
        title="Data can be modified or destroyed across a whole subscription",
        pillar="dp", severity="error", weight=8, object_kind="scope",
        why="Data-plane write reaches resource CONTENTS, which no resource lock protects.",
        remediation="Scope data-plane write roles to the specific account or container.",
        frameworks=("NIST:AC-6", "MCSB:PA-7", "MCSB:DP-8"),
        evaluate=_write_at_wide_scope,
    ),
    SignalSpec(
        id="dp.authorization_not_readable",
        title="Data access decided outside Azure RBAC",
        pillar="dp", severity="warning", weight=9, object_kind="resource",
        why=(
            "SQL, Kubernetes, Cosmos, Databricks, Redis and Kusto each decide data access in "
            "their own system. Listing Azure role assignments for them describes nothing, and a "
            "blank result reads as a pass."
        ),
        remediation="Review each service in its own authorization system and record the result.",
        frameworks=("NIST:AC-2", "MCSB:PA-1"),
        evaluate=_unreadable_authorization,
    ),
]
