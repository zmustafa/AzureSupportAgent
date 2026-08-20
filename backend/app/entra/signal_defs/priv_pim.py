"""Privileged Identity Management signals (P3) — configuration health, activation quality,
PIM for Groups and the Entra ⇄ Azure cross-plane join.

Split out of ``priv.py`` to keep each module readable; both export into the same ``priv``
pillar. Everything here is P2-gated except the cross-plane checks, which work on free-tier
directory data joined to the Azure RBAC cache another feature already maintains.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.entra import model
from app.entra.collectors.apps import TIER_CRITICAL
from app.entra.collectors.pim import last_activation, policy_for_role, privileged_policies
from app.entra.collectors.roles import (
    effective_role_names,
    principal_names,
    privileged_principal_ids,
)
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    principal_label,
    user_index,
)

PIM_DOC = "https://learn.microsoft.com/entra/id-governance/privileged-identity-management/pim-configure"
PIM_SETTINGS_DOC = (
    "https://learn.microsoft.com/entra/id-governance/privileged-identity-management/"
    "pim-how-to-change-default-settings"
)


def _pim(data: dict[str, Any]) -> dict[str, Any]:
    return domain(data, "pim")


def _require_policies(data: dict[str, Any]) -> list[dict[str, Any]]:
    pim = _pim(data)
    caps = pim.get("capabilities") or {}
    if not caps.get("policies"):
        raise SignalUnavailable(
            "Role management policies were not collected — this needs "
            "RoleManagementPolicy.Read.Directory and an Entra ID P2 license."
        )
    rows = privileged_policies(pim, domain(data, "roles"))
    if not rows:
        raise SignalUnavailable("No privileged role has a PIM policy to evaluate.")
    return rows


def _require_activations(data: dict[str, Any]) -> list[dict[str, Any]]:
    caps = _pim(data).get("capabilities") or {}
    if not caps.get("activations"):
        raise SignalUnavailable(
            "PIM activation history was not collected — this needs Entra ID P2."
        )
    return _pim(data).get("activations") or []


def _label(data: dict[str, Any], principal_id: str, fallback: str = "") -> str:
    return principal_label(data, principal_id, fallback)


# ------------------------------------------------------- PIM configuration health
def _policy_control(control: str, signal_id: str, severity: str, title: str, detail: str):
    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        out = []
        for row in _require_policies(data):
            if control == "mfa_on_activation":
                ok = bool(row.get("mfa_on_activation") or row.get("auth_context_required"))
            elif control == "duration":
                hours = row.get("max_activation_hours")
                ok = hours is not None and hours <= ctx.max_activation_hours
            elif control == "notifications":
                ok = int(row.get("notification_recipients") or 0) > 0
            else:
                ok = bool(row.get(control))
            if ok:
                continue
            out.append(model.finding(
                signal_id=signal_id, severity=severity, pillar="priv",
                object_kind="role", object_id=row["role_id"], object_name=row["role_name"],
                title=title.format(role=row["role_name"], hours=row.get("max_activation_hours")),
                detail=detail,
                evidence={
                    "role": row["role_name"], "tier": row["role_tier"],
                    "config_score": row["score"], "failed_controls": row["failed_controls"],
                    "approval_required": row["approval_required"],
                    "mfa_on_activation": row["mfa_on_activation"],
                    "auth_context_required": row["auth_context_required"],
                    "justification_required": row["justification_required"],
                    "max_activation_hours": row["max_activation_hours"],
                    "notification_recipients": row["notification_recipients"],
                },
                portal_link=model.portal_roles(),
            ))
        return out
    return _inner


# ------------------------------------------------------------- activation quality
def _stale_eligible(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Eligibility that has never been activated.

    Deliberately scoped to ELIGIBLE assignments only. "Never activated" is meaningless for a
    permanent assignment — the holder never needs to activate it — so applying this to active
    assignments would report every standing admin as stale. Dormant standing privilege is a
    real problem, but it is measured by last sign-in in ``priv.dormant_privileged_user``.
    """
    pim = _pim(data)
    _require_activations(data)
    roles = domain(data, "roles")
    out = []
    for row in roles.get("eligible") or []:
        if not row.get("role_privileged"):
            continue
        pid = str(row.get("principal_id") or "")
        role_id = str(row.get("role_id") or "")
        last = last_activation(pim, pid, role_id)
        days = ctx.days_since(last) if last else None
        if last and days is not None and days < ctx.stale_days:
            continue
        label = _label(data, pid, str(row.get("principal_name") or ""))
        out.append(model.finding(
            signal_id="priv.stale_eligible", severity="medium", pillar="priv",
            object_kind="user", object_id=pid, object_name=label,
            title=(f"{label} has never activated {row.get('role_name')}" if not last
                   else f"{label} last activated {row.get('role_name')} {days} days ago"),
            detail="Eligibility that is never used is privilege nobody needs — and it survives "
                   "reviews because it looks harmless while it is switched off.",
            evidence={"role": row.get("role_name"), "tier": row.get("role_tier"),
                      "last_activation": last or "never", "days_since": days,
                      "permanent_eligibility": row.get("permanent")},
            discriminator=role_id,
            portal_link=model.portal_roles(),
        ))
    return out


# NOTE: the activation-quality checks that used to live here (no justification, out of
# hours) moved to ``signal_defs/activations.py``. They kept their signal ids so existing
# suppressions still apply, but they now read the ``activations`` domain instead of
# ``pim.activations``. That matters: this module's source needs
# RoleAssignmentSchedule.Read.Directory and 403s on most tenants, and it only ever saw
# Entra ID — the new domain also reads Azure subscription activations and falls back to
# schedule instances when the rich source is forbidden.


# ------------------------------------------------------------------ PIM for Groups
def _pim_group_unmanaged(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    caps = _pim(data).get("capabilities") or {}
    if not caps.get("group_pim"):
        raise SignalUnavailable(
            "PIM for Groups was not collected — this needs PrivilegedAccess.Read.AzureADGroup "
            "and an Entra ID P2 license."
        )
    managed = {g.get("group_id") for g in _pim(data).get("group_eligibilities") or []}
    out = []
    for g in domain(data, "people").get("groups") or []:
        if not g.get("is_assignable_to_role") or g.get("id") in managed:
            continue
        out.append(model.finding(
            signal_id="priv.pim_group_unmanaged", severity="medium", pillar="priv",
            object_kind="group", object_id=str(g["id"]), object_name=g.get("display_name") or str(g["id"]),
            title=f"Role-assignable group '{g.get('display_name')}' is not managed by PIM for Groups",
            detail="Membership of this group confers a directory role permanently. Under PIM for "
                   "Groups the membership itself becomes eligible, time-bound and approved.",
            evidence={"is_assignable_to_role": True, "dynamic": g.get("dynamic"),
                      "owner_ids": g.get("owner_ids"), "managed_group_count": len(managed)},
            portal_link=model.portal_group(str(g["id"])),
        ))
    return out


# --------------------------------------------------------------------- cross-plane
def _require_link(data: dict[str, Any]) -> dict[str, Any]:
    link = data.get("_azure_link") or {}
    if not link.get("available"):
        raise SignalUnavailable(
            link.get("reason") or "No Azure RBAC scan is available to correlate against."
        )
    return link


def entra_power(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Principals holding Entra-side power, with why. Shared by the signal and the API view."""
    roles = domain(data, "roles")
    apps = domain(data, "apps")
    out: dict[str, dict[str, Any]] = {}
    names = principal_names(roles)

    for pid in privileged_principal_ids(roles):
        out.setdefault(pid, {"roles": [], "permissions": [], "kind": "user"})
        out[pid]["roles"] = sorted(effective_role_names(roles, pid))
        if names.get(pid):
            out[pid]["name"] = names[pid]

    for sp in apps.get("service_principals") or []:
        if sp.get("is_first_party"):
            continue
        critical = sorted({
            p["permission"] for p in sp.get("granted_app_permissions") or []
            if p.get("tier") == TIER_CRITICAL
        })
        if not critical:
            continue
        pid = str(sp.get("object_id") or "")
        entry = out.setdefault(pid, {"roles": [], "permissions": [], "kind": "sp"})
        entry["kind"] = "sp"
        entry["permissions"] = critical
        entry["name"] = sp.get("display_name", "")
        entry["app_id"] = sp.get("app_id", "")
    return out


def _cross_plane_power(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    link = _require_link(data)
    powers = entra_power(data)
    out = []
    for pid, entra in powers.items():
        azure = (link.get("principals") or {}).get(pid)
        if not azure or not azure.get("powerful_roles"):
            continue
        label = _label(data, pid, entra.get("name") or azure.get("name", ""))
        out.append(model.finding(
            signal_id="priv.cross_plane_power", severity="critical", pillar="priv",
            object_kind="sp" if entra.get("kind") == "sp" else "user",
            object_id=pid, object_name=label,
            title=f"{label} holds both Entra directory power and Azure control-plane power",
            detail="A principal with privileged directory access AND a powerful Azure role is a "
                   "single point of total compromise across both planes. No Microsoft surface "
                   "shows this correlation in one place.",
            evidence={
                "entra_roles": entra.get("roles") or [],
                "entra_permissions": entra.get("permissions") or [],
                "azure_roles": azure.get("powerful_roles"),
                "azure_broad_scopes": azure.get("broad_scopes"),
                "azure_subscriptions": azure.get("subscriptions"),
                "azure_rbac_generated_at": link.get("generated_at"),
                "azure_rbac_stale": link.get("stale"),
            },
            portal_link=model.portal_sp(pid) if entra.get("kind") == "sp" else model.portal_user(pid),
        ))
    return out


def _sync_account_azure_rbac(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    link = _require_link(data)
    sync_ids = set(domain(data, "roles").get("sync_account_ids") or [])
    out = []
    for pid in sorted(sync_ids):
        azure = (link.get("principals") or {}).get(pid)
        if not azure or not azure.get("roles"):
            continue
        label = _label(data, pid, azure.get("name", ""))
        out.append(model.finding(
            signal_id="priv.sync_account_azure_rbac", severity="critical", pillar="priv",
            object_kind="user", object_id=pid, object_name=label,
            title=f"Directory synchronisation account {label} also holds Azure RBAC",
            detail="Its credential lives on an on-premises server. Extending it into the Azure "
                   "control plane turns a domain-server compromise into a cloud compromise.",
            evidence={"azure_roles": azure.get("powerful_roles") or [r["role"] for r in azure["roles"][:10]],
                      "azure_subscriptions": azure.get("subscriptions"),
                      "azure_rbac_generated_at": link.get("generated_at")},
            portal_link=model.portal_user(pid),
        ))
    return out


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="priv.pim_no_mfa_on_activation", title="Privileged roles activatable without MFA",
        question="Can an administrator turn a privileged role on with just a password?",
        why="PIM only adds security if activation is itself protected. Without MFA or an "
            "authentication context, a stolen session activates the role as easily as the owner.",
        pillar="priv", severity="critical", weight=9, object_kind="role",
        domains=("roles", "pim"), requires=("RoleManagementPolicy.Read.Directory",), licence="p2",
        benchmarks=("MCSB PA-2",), impact=IMPACT_SATURATING, saturation=3,
        remediation="Require MFA (or an authentication context) on activation for every privileged role.",
        remediation_steps=(
            "Entra admin center > Identity Governance > PIM > Microsoft Entra roles > Settings.",
            "Select the role > Edit > Activation > tick 'Require Microsoft Entra multifactor authentication'.",
            "Prefer 'Require Microsoft Entra Conditional Access authentication context' for tier-0 roles.",
        ),
        doc_link=PIM_SETTINGS_DOC,
        evaluate=_policy_control(
            "mfa_on_activation", "priv.pim_no_mfa_on_activation", "critical",
            "Activating {role} does not require MFA",
            "Activation is the moment privilege becomes real; it should be the most strongly "
            "protected action in the tenant.",
        ),
        tags=("zero-trust",),
    ),
    SignalSpec(
        id="priv.pim_no_approval", title="Privileged roles activatable without approval",
        question="Can an administrator grant themselves a privileged role unilaterally?",
        why="Approval is what turns PIM from a delay into a control — without it, a compromised "
            "eligible account is equivalent to a permanent assignment.",
        pillar="priv", severity="high", weight=7, object_kind="role",
        domains=("roles", "pim"), requires=("RoleManagementPolicy.Read.Directory",), licence="p2",
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Require approval on activation for tier-0 roles, with named approvers.",
        remediation_steps=(
            "PIM > Microsoft Entra roles > Settings > select the role > Edit > Activation.",
            "Tick 'Require approval to activate' and nominate at least two approvers.",
            "Ensure no approver is also eligible for the same role (self-approval).",
        ),
        doc_link=PIM_SETTINGS_DOC,
        evaluate=_policy_control(
            "approval_required", "priv.pim_no_approval", "high",
            "Activating {role} does not require approval",
            "Without approval, eligibility is only a speed bump: the holder can grant themselves "
            "the role at any moment.",
        ),
    ),
    SignalSpec(
        id="priv.pim_no_justification", title="Privileged roles activatable without justification",
        question="Is there any record of why a role was activated?",
        why="Justification is what makes an after-the-fact review possible at all.",
        pillar="priv", severity="medium", weight=4, object_kind="role",
        domains=("roles", "pim"), requires=("RoleManagementPolicy.Read.Directory",), licence="p2",
        impact=IMPACT_SATURATING, saturation=8,
        remediation="Require justification (and a ticket reference where you have ticketing) on activation.",
        doc_link=PIM_SETTINGS_DOC,
        evaluate=_policy_control(
            "justification_required", "priv.pim_no_justification", "medium",
            "Activating {role} does not require justification",
            "An activation with no recorded reason cannot be reviewed later.",
        ),
    ),
    SignalSpec(
        id="priv.pim_long_duration", title="Activation windows longer than policy",
        question="How long does privilege stay on once activated?",
        why="A long activation window is standing access with extra steps — the whole point is "
            "that the role is off again quickly.",
        pillar="priv", severity="medium", weight=5, object_kind="role",
        domains=("roles", "pim"), requires=("RoleManagementPolicy.Read.Directory",), licence="p2",
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Reduce the maximum activation duration; a few hours is usually enough.",
        doc_link=PIM_SETTINGS_DOC,
        evaluate=_policy_control(
            "duration", "priv.pim_long_duration", "medium",
            "{role} can stay activated for up to {hours} hours",
            "A long activation window leaves privilege switched on well past the task that needed it.",
        ),
    ),
    SignalSpec(
        id="priv.pim_no_notifications", title="Activations nobody is told about",
        question="Does anyone find out when a privileged role is activated?",
        why="Silent activation removes the last chance to notice misuse in real time.",
        pillar="priv", severity="medium", weight=4, object_kind="role",
        domains=("roles", "pim"), requires=("RoleManagementPolicy.Read.Directory",), licence="p2",
        impact=IMPACT_SATURATING, saturation=8,
        remediation="Add notification recipients for activation events on every privileged role.",
        doc_link=PIM_SETTINGS_DOC,
        evaluate=_policy_control(
            "notifications", "priv.pim_no_notifications", "medium",
            "Activating {role} notifies nobody",
            "Nobody is alerted when this role is switched on.",
        ),
    ),
    SignalSpec(
        id="priv.stale_eligible", title="Eligibility that is never activated",
        question="Who is eligible for a privileged role but never uses it?",
        why="Unused eligibility is privilege nobody needs — and it survives reviews because it "
            "looks harmless while it is switched off.",
        pillar="priv", severity="medium", weight=5, object_kind="user",
        domains=("roles", "pim"), requires=("PrivilegedAccess.Read.AzureAD",), licence="p2",
        impact=IMPACT_SATURATING, saturation=10,
        remediation="Remove the eligibility, or attach a recurring access review to it.",
        doc_link=PIM_DOC, evaluate=_stale_eligible,
    ),
    SignalSpec(
        id="priv.pim_group_unmanaged", title="Role-assignable groups outside PIM for Groups",
        question="Which groups grant a role permanently rather than on request?",
        why="Membership of a role-assignable group is the role. Under PIM for Groups that "
            "membership itself becomes eligible, time-bound and approved.",
        pillar="priv", severity="medium", weight=4, object_kind="group",
        domains=("people", "pim"), requires=("PrivilegedAccess.Read.AzureADGroup",), licence="p2",
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Onboard the group to PIM for Groups and make its membership eligible.",
        doc_link="https://learn.microsoft.com/entra/id-governance/privileged-identity-management/concept-pim-for-groups",
        evaluate=_pim_group_unmanaged,
    ),
    SignalSpec(
        id="priv.cross_plane_power", title="Principals with both Entra and Azure power",
        question="Who can compromise the directory AND the Azure estate?",
        why="A principal holding privileged directory access and a powerful Azure role is a single "
            "point of total compromise. No Microsoft surface correlates the two planes.",
        pillar="priv", severity="critical", weight=10, object_kind="user",
        domains=("roles", "apps"), requires=("RoleManagement.Read.Directory",),
        benchmarks=("MCSB PA-1",), impact=IMPACT_SATURATING, saturation=2,
        remediation="Split the duties: use separate identities for directory administration and "
                    "Azure resource administration.",
        remediation_steps=(
            "Identify which plane the principal genuinely needs.",
            "Move the other plane's access to a separate, separately-governed identity.",
            "Where a workload needs both, make at least one side PIM-eligible.",
        ),
        doc_link="https://learn.microsoft.com/entra/identity/role-based-access-control/best-practices",
        evaluate=_cross_plane_power, tags=("zero-trust", "cross-plane"),
    ),
    SignalSpec(
        id="priv.sync_account_azure_rbac", title="Sync account with Azure RBAC",
        question="Does the on-premises sync account reach into Azure?",
        why="Its credential sits on a domain-joined server; extending it into the Azure control "
            "plane turns a server compromise into a cloud compromise.",
        pillar="priv", severity="critical", weight=8, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_BINARY,
        remediation="Remove all Azure role assignments from the directory synchronisation account.",
        doc_link="https://learn.microsoft.com/entra/identity/hybrid/connect/reference-connect-accounts-permissions",
        evaluate=_sync_account_azure_rbac, tags=("cross-plane",),
    ),
]
