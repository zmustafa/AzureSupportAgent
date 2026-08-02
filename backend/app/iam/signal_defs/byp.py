"""RBAC-bypass signals — the last unbuilt pillar (weight 12).

Every other pillar asks "who has a role?". This one asks whether the role even matters. A
perfect RBAC posture on an estate where `allowSharedKeyAccess` is true everywhere is a report
about a door standing next to an open window.

Each signal is gated on the bypass sweep having actually run. A findings screen that shows no
shared-key findings because the sweep failed is indistinguishable from one where every account
is locked down — and the reader will assume the second.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import schema
from app.iam.bypass import specs as sp
from app.iam.signals import Finding, SignalContext, SignalSpec

# Above this many principals holding the credential action, the door is not a door.
_WIDE_REACH = 10


def _rows(ctx: SignalContext) -> list[dict[str, Any]]:
    ctx.require(
        bool(ctx.bypass_rows) or ctx.bypass_assessed > 0,
        "The RBAC-bypass sweep has not run, so non-RBAC access paths (shared keys, local auth, "
        "admin users, SQL logins) have NOT been checked. Absence here does not mean absence.",
    )
    return ctx.bypass_rows


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["key"] == key:
            out[r["family"]].append(r)
    return out


def _worst(rows: list[dict[str, Any]]) -> str:
    order = ("critical", "error", "warning", "info")
    for s in order:
        if any(r["severity"] == s for r in rows):
            return s
    return "info"


def _finding_for(
    ctx: SignalContext,
    signal_id: str,
    spec_key: str,
    title: str,
    *,
    pillar_kind: str = "resource",
) -> list[Finding]:
    """One finding per (signal, resource). Aggregated by resource rather than by tenant so the
    remediation command — which is per resource — can be attached to something actionable."""
    rows = [r for r in _rows(ctx) if r["key"] == spec_key]
    out: list[Finding] = []
    for r in rows:
        reach = ""
        if r["reachabilityAvailable"]:
            reach = (
                f" {r['reachableCount']} principal(s) can call {r['credentialAction'].split('/')[-2]}"
                f"/{r['credentialAction'].split('/')[-1]}."
            )
        elif r["credentialAction"]:
            # An empty reachable list and an unavailable join look identical; say which it is.
            reach = " Who can obtain the credential could not be computed."
        out.append(
            Finding(
                signal_id=signal_id,
                title=f"{title}: {r['resourceName']}",
                severity=r["severity"],
                pillar="byp",
                object_kind=pillar_kind,
                subject=r["resourceId"],
                subject_label=r["resourceName"] or r["resourceId"],
                detail=r["detail"] + reach,
                evidence={
                    "resourceType": r["resourceType"],
                    "environment": r["environment"],
                    "reachableBy": [h["principalName"] for h in r["reachableBy"]][:10],
                    "reachableCount": r["reachableCount"],
                    "remediation": r["remediation"],
                    # Never published without the command it qualifies.
                    "breaksIf": r["breaksIf"],
                },
                count=max(1, r["reachableCount"]),
                remediation=f"{r['remediation']}  — WARNING: breaks {r['breaksIf']}.",
                frameworks=tuple(r["frameworks"]),
            )
        )
    return out


def _shared_key(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.shared_key", "storage.shared_key", "Shared key access")


def _key_never_expires(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.key_never_expires", "storage.key_never_expires", "Keys never expire")


def _public_blob(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.public_blob", "storage.public_blob", "Anonymous blob access")


def _acr_admin(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.acr_admin_user", "acr.admin_user", "Registry admin user")


def _aks_local(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.aks_local_accounts", "aks.local_accounts", "AKS local accounts")


def _aks_no_rbac(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.aks_no_azure_rbac", "aks.no_azure_rbac", "AKS without Azure RBAC")


def _sql_auth(ctx: SignalContext) -> list[Finding]:
    return _finding_for(ctx, "byp.sql_auth", "sql.entra_only_off", "SQL authentication")


def _local_auth(ctx: SignalContext) -> list[Finding]:
    """The `disableLocalAuth` family, rolled up per service.

    One finding per service rather than per resource: a tenant with 300 Service Bus namespaces
    all on the default produces one actionable sentence, not 300 identical ones."""
    rows = [r for r in _rows(ctx) if r["bypassKind"] == sp.KIND_LOCAL_AUTH]
    by_family = defaultdict(list)
    for r in rows:
        by_family[r["family"]].append(r)

    out: list[Finding] = []
    for family, frows in by_family.items():
        names = sorted({r["resourceName"] for r in frows})
        out.append(
            Finding(
                signal_id="byp.local_auth",
                title=f"{len(frows)} {family} resource(s) accept key-based authentication",
                severity=_worst(frows),
                pillar="byp",
                object_kind="resource",
                subject=f"{ctx.tenant_id}|{family}",
                subject_label=f"{family} local authentication",
                detail=(
                    f"{len(frows)} resource(s) can be reached with a key or connection string "
                    "rather than an Entra identity: " + ", ".join(names[:5])
                    + (f" and {len(names) - 5} more." if len(names) > 5 else ".")
                ),
                count=len(frows),
                evidence={
                    "resources": names[:20],
                    "remediation": frows[0]["remediation"],
                    "breaksIf": frows[0]["breaksIf"],
                },
                remediation=f"{frows[0]['remediation']}  — WARNING: breaks {frows[0]['breaksIf']}.",
                frameworks=tuple(frows[0]["frameworks"]),
            )
        )
    return out


def _rbac_not_the_only_door(ctx: SignalContext) -> list[Finding]:
    """The headline number: what share of the assessed estate can be reached without a role."""
    _rows(ctx)  # gate on the sweep having run
    summary = ctx.bypass_summary or {}
    assessed = summary.get("assessed") or 0
    ctx.require(assessed > 0, "No resources were assessed for RBAC bypass.")
    pct = summary.get("rbac_only_pct")
    ctx.require(pct is not None, "The RBAC-only share could not be computed.")
    if pct >= 80:
        return []
    return [
        Finding(
            signal_id="byp.rbac_not_only_door",
            title=f"RBAC is the only door for just {pct}% of assessed resources",
            severity="critical" if pct < 40 else "error",
            pillar="byp",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{summary.get('bypassed', 0)} of {assessed} assessed resource(s) can be reached "
                "without any Azure role assignment — with a shared key, a local account, an admin "
                "user or a SQL login. Every access review in this product describes the door; "
                "these resources have a window."
            ),
            count=summary.get("bypassed", 0),
            evidence={
                "assessed": assessed,
                "bypassed": summary.get("bypassed", 0),
                "by_family": summary.get("by_family", []),
            },
            remediation=(
                "Work down the family list: disabling shared-key and local authentication is the "
                "single highest-leverage change, but check each `breaks if` first."
            ),
            frameworks=("MCSB:IM-1", "NIST:AC-3"),
        )
    ]


def _blind_families(ctx: SignalContext) -> list[Finding]:
    """A service that could not be read must not read as a service with nothing wrong."""
    summary = ctx.bypass_summary or {}
    fams = summary.get("by_family") or []
    ctx.require(bool(fams), "The RBAC-bypass sweep has not run.")
    blind = [f for f in fams if f.get("status") in schema.UNTRUSTWORTHY_STATUSES]
    if not blind:
        return []
    return [
        Finding(
            signal_id="byp.family_unreadable",
            title=f"{len(blind)} service family/families could not be assessed for bypass",
            severity="warning",
            pillar="byp",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                "These services are absent from both the findings and the denominator, so the "
                "'RBAC is the only door' percentage is computed over less than the whole estate: "
                + ", ".join(sorted(f["family"] for f in blind))
            ),
            count=len(blind),
            evidence={"families": blind},
            remediation="Grant the connection Reader on the affected scopes and re-run the scan.",
        )
    ]


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="byp.rbac_not_only_door",
        title="RBAC is not the only door",
        pillar="byp", severity="critical", weight=10, object_kind="tenant",
        why="An access review describes role assignments. These resources can be reached without one.",
        remediation="Disable shared-key and local authentication, checking each `breaks if` first.",
        frameworks=("MCSB:IM-1", "NIST:AC-3"),
        evaluate=_rbac_not_the_only_door,
    ),
    SignalSpec(
        id="byp.shared_key",
        title="Storage shared key access enabled",
        pillar="byp", severity="error", weight=9, object_kind="resource",
        why="Account keys grant full data-plane access with no principal and no conditional access.",
        remediation="Set --allow-shared-key-access false once clients use Entra auth.",
        frameworks=("CIS-Azure:3.8", "MCSB:IM-1"),
        evaluate=_shared_key,
    ),
    SignalSpec(
        id="byp.key_never_expires",
        title="Storage keys have no expiry policy",
        pillar="byp", severity="warning", weight=4, object_kind="resource",
        why="A leaked key stays valid until somebody notices.",
        remediation="Set a key expiration period.",
        frameworks=("CIS-Azure:3.9",),
        evaluate=_key_never_expires,
    ),
    SignalSpec(
        id="byp.public_blob",
        title="Anonymous blob access permitted",
        pillar="byp", severity="error", weight=8, object_kind="resource",
        why="Access with no credential at all — not even a key.",
        remediation="Set --allow-blob-public-access false.",
        frameworks=("CIS-Azure:3.7",),
        evaluate=_public_blob,
    ),
    SignalSpec(
        id="byp.local_auth",
        title="Key-based authentication enabled",
        pillar="byp", severity="error", weight=8, object_kind="resource",
        why="A connection string is an identity nobody manages.",
        remediation="Set disableLocalAuth on the affected services.",
        frameworks=("MCSB:IM-1",),
        evaluate=_local_auth,
    ),
    SignalSpec(
        id="byp.aks_local_accounts",
        title="AKS local accounts enabled",
        pillar="byp", severity="error", weight=8, object_kind="resource",
        why="The admin kubeconfig is cluster-admin and bypasses Entra entirely.",
        remediation="az aks update --disable-local-accounts.",
        frameworks=("CIS-Azure:8.6",),
        evaluate=_aks_local,
    ),
    SignalSpec(
        id="byp.aks_no_azure_rbac",
        title="AKS not using Azure RBAC for Kubernetes",
        pillar="byp", severity="warning", weight=5, object_kind="resource",
        why="Authorization inside the cluster is not described by any Azure role assignment.",
        remediation="az aks update --enable-azure-rbac.",
        frameworks=("CIS-Azure:8.5",),
        evaluate=_aks_no_rbac,
    ),
    SignalSpec(
        id="byp.sql_auth",
        title="SQL authentication permitted",
        pillar="byp", severity="error", weight=7, object_kind="resource",
        why="SQL logins live outside the directory: no conditional access, no MFA, no leaver process.",
        remediation="Enable Entra-only authentication.",
        frameworks=("CIS-Azure:4.1.3",),
        evaluate=_sql_auth,
    ),
    SignalSpec(
        id="byp.acr_admin_user",
        title="Container registry admin user enabled",
        pillar="byp", severity="error", weight=6, object_kind="resource",
        why="A shared username and password with push rights that belongs to no one.",
        remediation="az acr update --admin-enabled false.",
        frameworks=("CIS-Azure:9.4",),
        evaluate=_acr_admin,
    ),
    SignalSpec(
        id="byp.family_unreadable",
        title="A service family could not be assessed",
        pillar="byp", severity="warning", weight=3, object_kind="tenant",
        why="An unreadable service must not render as a service with nothing wrong.",
        remediation="Grant the connection Reader on the affected scopes.",
        evaluate=_blind_families,
    ),
]
