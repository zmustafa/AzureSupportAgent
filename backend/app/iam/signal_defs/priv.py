"""Privileged-access signals — the heaviest pillar (22).

The question this pillar answers is not "who is privileged" (the grid already shows that) but
"how much of that privilege is held permanently, by whom, and with what standing in the way".
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import diff, schema
from app.iam.signals import Finding, SignalContext, SignalSpec

# Tier-0: holding these is equivalent to owning the estate.
_TIER0_AZURE = frozenset({"owner", "user access administrator", "role based access control administrator"})

# How many principals may hold tier-0 at one scope before concentration stops being the concern
# and sprawl starts being it.
_TIER0_SPRAWL = 5
# Above this share of privileged access being permanent, PIM is not meaningfully in use.
_STANDING_RATIO_BAD = 0.5


def _scope_label(row: dict[str, Any]) -> str:
    return str(row.get("scopeDisplayName") or row.get("subscriptionName") or row.get("scope") or "unknown scope")


def _who(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or row.get("effectivePrincipalUserPrincipalName")
        or row.get("effectivePrincipalId")
        or "unknown principal"
    )


# --------------------------------------------------------------------------- standing privilege
def _standing_privilege(ctx: SignalContext) -> list[Finding]:
    ctx.require(
        ctx.kpis.get("pim_collected"),
        "PIM eligibility was not collected, so permanent and JIT privilege cannot be told apart.",
    )
    ratio = ctx.kpis.get("standing_ratio")
    ctx.require(ratio is not None, "There is no privileged access in this scan to measure.")
    if ratio <= _STANDING_RATIO_BAD:
        return []
    standing = ctx.kpis.get("standing_privileged", 0)
    eligible = ctx.kpis.get("eligible_privileged", 0)
    return [
        Finding(
            signal_id="priv.standing_privilege",
            title="Most privileged access is permanent",
            severity="error" if ratio < 0.9 else "critical",
            pillar="priv",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                f"{round(ratio * 100)}% of privileged access is held permanently "
                f"({standing} standing versus {eligible} eligible). Standing privilege is held "
                f"whether or not it is being used."
            ),
            count=standing,
            evidence={"standing": standing, "eligible": eligible, "ratio": ratio},
            remediation="Convert standing privileged assignments to PIM-eligible (JIT) assignments.",
            frameworks=("CIS-Azure:1.23", "NIST:AC-6(5)", "MCSB:PA-2"),
        )
    ]


def _eligible_without_controls(ctx: SignalContext) -> list[Finding]:
    """Permanently eligible with neither approval nor MFA is JIT in name only."""
    ctx.require(
        ctx.collector_ran("AzurePimEligibility", "PimDirectoryAssignments"),
        "PIM eligibility was not collected.",
    )
    # Grouped by the grant's identity, then ONE finding per group.
    #
    # The subject feeds the fingerprint, which keys the suppression table and the scanner
    # delta, so two findings that share a subject are indistinguishable to both — suppressing
    # one silently suppresses the other, and the scanner's counts stop adding up. Two things
    # collide here on real data: `assignmentId` is inherited by every transitive member of a
    # group-granted eligibility (10 principals to one id), and the same principal can reach the
    # same eligibility by more than one path. `diff.row_key` fixes the first; aggregating with a
    # count fixes the second, and is what the rest of the registry already does.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if r.get("assignmentState") != schema.STATE_ELIGIBLE or not r.get("roleIsPrivileged"):
            continue
        weak = not r.get("requiresApproval") and not r.get("requiresMfa")
        if not (weak and r.get("isPermanentEligible")):
            continue
        groups[diff.row_key(r)].append(r)

    out: list[Finding] = []
    for key, rows in groups.items():
        r = rows[0]
        paths = sorted({str(x.get("accessPath") or "") for x in rows if x.get("accessPath")})
        extra = f" Reached by {len(rows)} paths ({', '.join(paths)})." if len(rows) > 1 else ""
        out.append(
            Finding(
                signal_id="priv.eligible_without_controls",
                title="Eligible for a privileged role with no approval and no MFA",
                severity="error",
                pillar="priv",
                object_kind="assignment",
                subject=key,
                subject_label=f"{_who(r)} → {r.get('roleName')}",
                count=len(rows),
                detail=(
                    f"{_who(r)} is permanently eligible for {r.get('roleName')} on "
                    f"{_scope_label(r)} and can activate it without approval or MFA. That is "
                    f"standing privilege with an extra click, not just-in-time access.{extra}"
                ),
                evidence={"role": r.get("roleName"), "scope": r.get("scope"),
                          "accessPaths": paths},
                remediation="Require approval and MFA on the role's activation policy, and give the eligibility an expiry.",
                frameworks=("NIST:AC-6(5)", "MCSB:PA-2"),
            )
        )
    return out


def _tier0_sprawl(ctx: SignalContext) -> list[Finding]:
    """Tier-0 held by many principals at one scope."""
    by_scope: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    for r in ctx.grants:
        if str(r.get("roleName", "")).strip().lower() not in _TIER0_AZURE:
            continue
        if r.get("assignmentState") == schema.STATE_ELIGIBLE:
            continue  # eligible tier-0 is the good case; sprawl is about who HOLDS it
        scope = str(r.get("scope") or "")
        if not scope:
            continue
        by_scope[scope].add(str(r.get("effectivePrincipalId") or _who(r)))
        labels.setdefault(scope, _scope_label(r))
    out: list[Finding] = []
    for scope, principals in by_scope.items():
        if len(principals) < _TIER0_SPRAWL:
            continue
        out.append(
            Finding(
                signal_id="priv.tier0_sprawl",
                title="Many principals hold tier-0 access at one scope",
                severity="warning",
                pillar="priv",
                object_kind="scope",
                subject=scope,
                subject_label=labels.get(scope, scope),
                detail=(
                    f"{len(principals)} principals hold Owner or User Access Administrator on "
                    f"{labels.get(scope, scope)}. Each one can grant themselves anything else."
                ),
                count=len(principals),
                evidence={"principals": sorted(principals)[:10], "total": len(principals)},
                remediation="Reduce to the smallest set that can operate the scope; make the rest eligible.",
                frameworks=("CIS-Azure:1.21", "NIST:AC-6"),
            )
        )
    return out


def _classic_administrators(ctx: SignalContext) -> list[Finding]:
    """Co-Administrator is effectively Owner and is invisible in the portal's IAM blade."""
    ctx.require(ctx.collector_ran("ClassicAdministrators"), "Classic administrators were not collected.")
    by_sub: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if r.get("surface") == schema.SURFACE_CLASSIC:
            by_sub[str(r.get("subscriptionId") or r.get("scope") or "")].append(r)
    out: list[Finding] = []
    for sub, rows in by_sub.items():
        out.append(
            Finding(
                signal_id="priv.classic_administrators",
                title="Classic administrators still exist",
                severity="error",
                pillar="priv",
                object_kind="scope",
                subject=sub,
                subject_label=str(rows[0].get("subscriptionName") or sub),
                detail=(
                    f"{len(rows)} classic administrator assignment(s) on this subscription. "
                    f"Co-Administrator is equivalent to Owner and does not appear in the portal's "
                    f"Access control (IAM) blade, so these survive reviews that only look there."
                ),
                count=len(rows),
                evidence={"admins": [str(r.get("principalDisplayName")) for r in rows][:10]},
                remediation="Remove classic administrators and grant the equivalent Azure RBAC role instead.",
                frameworks=("CIS-Azure:1.20", "MCSB:PA-1"),
            )
        )
    return out


def _keyvault_dual_grant_model(ctx: SignalContext) -> list[Finding]:
    """A vault with both access policies and RBAC data roles has two independent front doors."""
    ctx.require(ctx.collector_ran("KeyVaultAccessPolicies"), "Key Vault access policies were not collected.")
    policy_vaults: dict[str, str] = {}
    rbac_vaults: set[str] = set()
    for r in ctx.grants:
        if r.get("surface") == schema.SURFACE_KEY_VAULT:
            policy_vaults[str(r.get("scope"))] = str(r.get("resourceName") or r.get("scope"))
        elif r.get("roleHasDataActions") and "keyvault" in str(r.get("resourceType", "")).lower():
            rbac_vaults.add(str(r.get("scope")))
    out: list[Finding] = []
    for scope, name in policy_vaults.items():
        if scope not in rbac_vaults:
            continue
        out.append(
            Finding(
                signal_id="priv.keyvault_dual_grant_model",
                title="Key Vault grants access through two independent models",
                severity="warning",
                pillar="priv",
                object_kind="scope",
                subject=scope,
                subject_label=name,
                detail=(
                    f"{name} has both legacy access policies and RBAC data-plane assignments. "
                    f"Revoking one does not revoke the other, so an access review that looks at "
                    f"only one model will believe access was removed when it was not."
                ),
                evidence={"vault": name},
                remediation="Migrate the vault to RBAC authorization and delete the access policies.",
                frameworks=("CIS-Azure:8.5", "MCSB:PA-7"),
            )
        )
    return out


def _privileged_service_principals(ctx: SignalContext) -> list[Finding]:
    """A non-human principal with tier-0 has no MFA, no conditional access and no sign-in story."""
    by_sp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        ptype = str(r.get("effectivePrincipalType") or r.get("principalType") or "").lower()
        if ptype != "serviceprincipal":
            continue
        if str(r.get("roleName", "")).strip().lower() not in _TIER0_AZURE:
            continue
        by_sp[str(r.get("effectivePrincipalId") or _who(r))].append(r)
    out: list[Finding] = []
    for pid, rows in by_sp.items():
        out.append(
            Finding(
                signal_id="priv.privileged_service_principal",
                title="Service principal holds tier-0 access",
                severity="warning",
                pillar="priv",
                object_kind="principal",
                subject=pid,
                subject_label=_who(rows[0]),
                detail=(
                    f"{_who(rows[0])} holds {rows[0].get('roleName')} on "
                    f"{len({r.get('scope') for r in rows})} scope(s). A service principal has no "
                    f"MFA and no Conditional Access — its credential is the whole control."
                ),
                count=len(rows),
                evidence={"scopes": sorted({str(r.get("scope")) for r in rows})[:10]},
                remediation="Scope the identity to what it actually deploys, and prefer a managed identity over a secret.",
                frameworks=("NIST:AC-6", "MCSB:PA-1"),
            )
        )
    return out


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="priv.standing_privilege",
        title="Most privileged access is permanent",
        pillar="priv", severity="error", weight=10, object_kind="tenant",
        why="Standing privilege is available to an attacker at all times, not only when the owner is working.",
        remediation="Convert standing privileged assignments to PIM-eligible.",
        frameworks=("CIS-Azure:1.23", "NIST:AC-6(5)", "MCSB:PA-2"),
        evaluate=_standing_privilege,
    ),
    SignalSpec(
        id="priv.eligible_without_controls",
        title="Eligible for privilege with no approval and no MFA",
        pillar="priv", severity="error", weight=8, object_kind="assignment",
        why="PIM without activation controls is standing privilege with an extra click.",
        remediation="Require approval and MFA, and give the eligibility an expiry.",
        frameworks=("NIST:AC-6(5)", "MCSB:PA-2"),
        evaluate=_eligible_without_controls,
    ),
    SignalSpec(
        id="priv.tier0_sprawl",
        title="Many principals hold tier-0 at one scope",
        pillar="priv", severity="warning", weight=6, object_kind="scope",
        why="Every tier-0 holder can grant themselves everything else, so the blast radius is the union.",
        remediation="Reduce to the smallest operating set; make the rest eligible.",
        frameworks=("CIS-Azure:1.21", "NIST:AC-6"),
        evaluate=_tier0_sprawl,
    ),
    SignalSpec(
        id="priv.classic_administrators",
        title="Classic administrators still exist",
        pillar="priv", severity="error", weight=7, object_kind="scope",
        why="Co-Administrator equals Owner and is invisible in the portal's IAM blade.",
        remediation="Remove classic administrators; grant the equivalent RBAC role instead.",
        frameworks=("CIS-Azure:1.20", "MCSB:PA-1"),
        evaluate=_classic_administrators,
    ),
    SignalSpec(
        id="priv.keyvault_dual_grant_model",
        title="Key Vault grants access two different ways",
        pillar="priv", severity="warning", weight=5, object_kind="scope",
        why="Revoking one model does not revoke the other, so a review can believe access was removed when it was not.",
        remediation="Migrate the vault to RBAC authorization and delete the access policies.",
        frameworks=("CIS-Azure:8.5", "MCSB:PA-7"),
        evaluate=_keyvault_dual_grant_model,
    ),
    SignalSpec(
        id="priv.privileged_service_principal",
        title="Service principal holds tier-0 access",
        pillar="priv", severity="warning", weight=6, object_kind="principal",
        why="A non-human principal has no MFA and no Conditional Access; its credential is the whole control.",
        remediation="Scope the identity to what it deploys; prefer a managed identity.",
        frameworks=("NIST:AC-6", "MCSB:PA-1"),
        evaluate=_privileged_service_principals,
    ),
]
