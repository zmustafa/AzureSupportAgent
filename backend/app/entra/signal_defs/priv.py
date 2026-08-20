"""Privileged access pillar — standing roles, privileged guests and service principals,
group-derived privilege, separation of duties and dormancy.

Where PIM schedule data is unavailable (no P2, or the scope was not consented) the standing
signals still fire — an active assignment *is* standing access right now — but every finding
says so in its evidence rather than quietly asserting permanence. Overstating certainty is
how a posture tool loses an administrator's trust.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors.roles import (
    GLOBAL_ADMIN,
    SOD_RULES,
    effective_role_names,
    global_admin_ids,
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
ROLE_DOC = "https://learn.microsoft.com/entra/identity/role-based-access-control/best-practices"


def _roles(data: dict[str, Any]) -> dict[str, Any]:
    return domain(data, "roles")


def _standing(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Active assignments that are not a live PIM activation."""
    return [a for a in _roles(data).get("assignments") or [] if not a.get("activated")]


def _permanence_note(data: dict[str, Any]) -> dict[str, Any]:
    caps = _roles(data).get("capabilities") or {}
    known = bool(caps.get("permanence_known"))
    return {
        "pim_schedule_data": known,
        "caveat": "" if known else (
            "PIM assignment schedules were unavailable, so this may be an activated eligible "
            "assignment rather than a permanent one."
        ),
    }


def _principal_label(data: dict[str, Any], row: dict[str, Any]) -> str:
    if row.get("principal_name"):
        return str(row["principal_name"])
    if row.get("principal_upn"):
        return str(row["principal_upn"])
    u = user_index(data).get(str(row.get("principal_id"))) or {}
    return u.get("upn") or u.get("display_name") or str(row.get("principal_id"))


# ------------------------------------------------------------------------- evaluators
def _standing_role(tier: str, signal_id: str, severity: str, only_ga: bool = False, exclude_ga: bool = False):
    def _inner(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
        note = _permanence_note(data)
        out = []
        for a in _standing(data):
            name = (a.get("role_name") or "").strip().lower()
            if only_ga and name != GLOBAL_ADMIN:
                continue
            if exclude_ga and name == GLOBAL_ADMIN:
                continue
            if not only_ga and a.get("role_tier") != tier:
                continue
            if a.get("permanent") is False:
                continue  # a genuinely time-bound assignment is not standing access
            label = _principal_label(data, a)
            out.append(model.finding(
                signal_id=signal_id, severity=severity, pillar="priv",
                object_kind="user" if a.get("principal_type") == "User" else "sp",
                object_id=str(a.get("principal_id")), object_name=label,
                title=f"{label} holds {a.get('role_name')} permanently",
                detail="Standing privilege is available to an attacker at every moment, not only "
                       "when it is being used. Make it eligible through PIM instead.",
                evidence={"role": a.get("role_name"), "tier": a.get("role_tier"),
                          "principal_type": a.get("principal_type"), "scope": a.get("scope"),
                          "source": a.get("source"), **note},
                discriminator=str(a.get("role_id")),
                portal_link=model.portal_roles(),
            ))
        return out
    return _inner


def _too_many_global_admins(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    gas = global_admin_ids(_roles(data))
    if len(gas) <= ctx.max_global_admins:
        return []
    return [model.finding(
        signal_id="priv.too_many_global_admins", severity="high", pillar="priv",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"{len(gas)} principals hold Global Administrator",
        detail=f"Microsoft recommends fewer than {ctx.max_global_admins}. Each one is a full "
               "tenant-takeover target.",
        evidence={"count": len(gas), "recommended_max": ctx.max_global_admins,
                  "principal_ids": sorted(gas)[:50]},
        discriminator=str(len(gas) > ctx.max_global_admins),
        portal_link=model.portal_roles(),
    )]


def _too_few_global_admins(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    gas = global_admin_ids(_roles(data))
    if len(gas) >= ctx.min_global_admins:
        return []
    return [model.finding(
        signal_id="priv.too_few_global_admins", severity="high", pillar="priv",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"Only {len(gas)} principal(s) hold Global Administrator",
        detail="Fewer than two Global Administrators is an availability risk: losing the single "
               "account means losing the tenant. This is the one place where reducing privilege "
               "further makes things worse.",
        evidence={"count": len(gas), "recommended_min": ctx.min_global_admins},
        discriminator=str(len(gas)),
        portal_link=model.portal_roles(),
    )]


def _privileged_guest(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    users = user_index(data)
    out = []
    for pid in sorted(privileged_principal_ids(_roles(data))):
        u = users.get(pid)
        if not u or u.get("user_type") != "Guest":
            continue
        roles = sorted(effective_role_names(_roles(data), pid))
        out.append(model.finding(
            signal_id="priv.privileged_guest", severity="critical", pillar="priv",
            object_kind="user", object_id=pid, object_name=u.get("upn") or pid,
            title=f"Guest {u.get('upn')} holds a privileged directory role",
            detail="An external identity you do not control holds administrative power. Their home "
                   "tenant's security posture is not yours to manage.",
            evidence={"roles": roles, "enabled": u.get("enabled"),
                      "external_state": u.get("external_user_state"), "last_signin": u.get("last_signin")},
            portal_link=model.portal_user(pid),
        ))
    return out


def _privileged_sp(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    sps = {s.get("object_id"): s for s in domain(data, "apps").get("service_principals") or []}
    seen: set[str] = set()
    out = []
    for bucket in ("assignments", "eligible"):
        for row in roles.get(bucket) or []:
            if row.get("principal_type") != "ServicePrincipal" or not row.get("role_privileged"):
                continue
            pid = str(row.get("principal_id"))
            key = f"{pid}|{row.get('role_id')}"
            if key in seen:
                continue
            seen.add(key)
            sp = sps.get(pid) or {}
            label = row.get("principal_name") or sp.get("display_name") or pid
            out.append(model.finding(
                signal_id="priv.privileged_sp", severity="critical", pillar="priv",
                object_kind="sp", object_id=pid, object_name=label,
                title=f"Service principal '{label}' holds {row.get('role_name')}",
                detail="A workload identity with a privileged directory role has no MFA, no "
                       "Conditional Access by default and a credential that can be exfiltrated.",
                evidence={"role": row.get("role_name"), "tier": row.get("role_tier"),
                          "assignment_kind": row.get("assignment_kind", bucket),
                          "app_id": sp.get("app_id", ""), "sp_type": sp.get("sp_type", "")},
                discriminator=str(row.get("role_id")),
                portal_link=model.portal_sp(pid),
            ))
    return out


def _group_derived(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    out = []
    for a in roles.get("assignments") or []:
        if a.get("principal_type") != "Group" or not a.get("role_privileged"):
            continue
        members = (roles.get("group_members") or {}).get(str(a.get("principal_id"))) or []
        out.append(model.finding(
            signal_id="priv.group_derived_privileged", severity="medium", pillar="priv",
            object_kind="group", object_id=str(a.get("principal_id")),
            object_name=a.get("principal_name") or str(a.get("principal_id")),
            title=f"Group '{a.get('principal_name')}' confers {a.get('role_name')} on {len(members)} member(s)",
            detail="Privilege arriving through group membership is invisible on the user's own role "
                   "list, and anyone who can change the membership can grant the role.",
            evidence={"role": a.get("role_name"), "member_count": len(members),
                      "member_sample": members[:25]},
            discriminator=str(a.get("role_id")),
            portal_link=model.portal_group(str(a.get("principal_id"))),
        ))
    return out


def _role_assignable_group_unprotected(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    groups = domain(data, "people").get("groups") or []
    caps = domain(data, "people").get("capabilities") or {}
    out = []
    for g in groups:
        if not g.get("is_assignable_to_role"):
            continue
        problems = []
        if g.get("owners_known") and not g.get("owner_ids"):
            problems.append("no owner")
        if g.get("dynamic"):
            problems.append("dynamic membership rule (a rule change grants the role)")
        if not problems:
            continue
        out.append(model.finding(
            signal_id="priv.role_assignable_group_unprotected", severity="high", pillar="priv",
            object_kind="group", object_id=str(g.get("id")), object_name=g.get("display_name") or str(g.get("id")),
            title=f"Role-assignable group '{g.get('display_name')}' is unprotected: {', '.join(problems)}",
            detail="Whoever can change this group's membership effectively holds every role it confers.",
            evidence={"problems": problems, "membership_rule": g.get("membership_rule", ""),
                      "owners_known": g.get("owners_known"), "owner_ids": g.get("owner_ids"),
                      "owner_lookups_complete": caps.get("group_owners")},
            discriminator=",".join(problems),
            portal_link=model.portal_group(str(g.get("id"))),
        ))
    return out


def _sync_account_privileged(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    sync_ids = set(roles.get("sync_account_ids") or [])
    if not sync_ids:
        return []
    users = user_index(data)
    out = []
    for pid in sorted(sync_ids):
        extra = sorted(effective_role_names(roles, pid) - {"directory synchronization accounts"})
        if not extra:
            continue
        u = users.get(pid) or {}
        label = u.get("upn") or u.get("display_name") or pid
        out.append(model.finding(
            signal_id="priv.sync_account_privileged", severity="critical", pillar="priv",
            object_kind="user", object_id=pid, object_name=label,
            title=f"Directory synchronisation account {label} holds additional roles",
            detail="The sync account already has broad write access to the directory and its "
                   "credential lives on an on-premises server. Extra roles compound a very "
                   "attractive target.",
            evidence={"additional_roles": extra},
            portal_link=model.portal_user(pid),
        ))
    return out


def _dormant_privileged(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    caps = domain(data, "people").get("capabilities") or {}
    if not caps.get("signin_activity"):
        raise SignalUnavailable(
            "Last sign-in activity was not collected (needs AuditLog.Read.All and Entra ID P1), "
            "so dormancy cannot be determined."
        )
    users = user_index(data)
    out = []
    for pid in sorted(privileged_principal_ids(_roles(data))):
        u = users.get(pid)
        if not u or not u.get("enabled") or u.get("user_type") == "Guest":
            continue
        if not u.get("signin_known"):
            continue
        days = ctx.days_since(str(u.get("last_signin") or ""))
        if u.get("last_signin") and (days is None or days < ctx.stale_days):
            continue
        out.append(model.finding(
            signal_id="priv.dormant_privileged_user", severity="high", pillar="priv",
            object_kind="user", object_id=pid, object_name=u.get("upn") or pid,
            title=f"Privileged account {u.get('upn')} has not signed in for "
                  f"{days if days is not None else 'over ' + str(ctx.stale_days)} days",
            detail="Unused privilege is pure risk: nobody would notice it being abused.",
            evidence={"last_signin": u.get("last_signin") or "never",
                      "days_since": days, "roles": sorted(effective_role_names(_roles(data), pid))},
            portal_link=model.portal_user(pid),
        ))
    return out


def _sod_conflict(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    users = user_index(data)
    principals = privileged_principal_ids(roles)
    out = []
    for pid in sorted(principals):
        held = effective_role_names(roles, pid)
        for role_a, role_b, why in SOD_RULES:
            if role_a in held and role_b in held:
                u = users.get(pid) or {}
                label = principal_label(data, pid)
                out.append(model.finding(
                    signal_id="priv.sod_conflict", severity="high", pillar="priv",
                    object_kind="user" if u else "sp", object_id=pid, object_name=label,
                    title=f"{label} holds a conflicting role pair: {role_a} + {role_b}",
                    detail=why,
                    evidence={"role_a": role_a, "role_b": role_b, "all_roles": sorted(held)},
                    discriminator=f"{role_a}+{role_b}",
                    portal_link=model.portal_user(pid) if u else model.portal_sp(pid),
                ))
    return out


def _stale_eligible(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    if not (roles.get("capabilities") or {}).get("pim_eligibility"):
        raise SignalUnavailable(
            "PIM eligibility schedules were not collected (needs PrivilegedAccess.Read.AzureAD and Entra ID P2)."
        )
    out = []
    for e in roles.get("eligible") or []:
        if not e.get("permanent"):
            continue
        if not e.get("role_privileged"):
            continue
        label = e.get("principal_name") or e.get("principal_upn") or str(e.get("principal_id"))
        out.append(model.finding(
            signal_id="priv.pim_permanent_eligible", severity="medium", pillar="priv",
            object_kind="user", object_id=str(e.get("principal_id")), object_name=label,
            title=f"{label} is permanently eligible for {e.get('role_name')}",
            detail="Eligibility without an expiry never gets reviewed. Time-bound eligibility forces "
                   "a periodic decision.",
            evidence={"role": e.get("role_name"), "tier": e.get("role_tier"),
                      "member_type": e.get("member_type"), "status": e.get("status")},
            discriminator=str(e.get("role_id")),
            portal_link=model.portal_roles(),
        ))
    return out


def _pim_not_used(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    roles = _roles(data)
    caps = roles.get("capabilities") or {}
    if not caps.get("pim_eligibility"):
        raise SignalUnavailable(
            "PIM eligibility schedules were not collected (needs PrivilegedAccess.Read.AzureAD and Entra ID P2)."
        )
    if roles.get("eligible"):
        return []
    privileged_active = sum(1 for a in roles.get("assignments") or [] if a.get("role_privileged"))
    if not privileged_active:
        return []
    return [model.finding(
        signal_id="priv.pim_not_used", severity="medium", pillar="priv",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="Privileged Identity Management is available but unused",
        detail=f"All {privileged_active} privileged assignment(s) are standing. PIM would make them "
               "eligible, time-bound, approved and audited.",
        evidence={"privileged_active": privileged_active, "eligible": 0},
        portal_link=model.portal_roles(),
    )]


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="priv.standing_global_admin", title="Permanent Global Administrators",
        question="Who holds Global Administrator all the time rather than on request?",
        why="Standing tenant-takeover privilege is available to an attacker every second of every "
            "day, not only while it is being used.",
        pillar="priv", severity="critical", weight=10, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        benchmarks=("CIS 1.1.3", "MCSB PA-1"), impact=IMPACT_SATURATING, saturation=3,
        remediation="Convert to PIM-eligible assignments with approval and MFA on activation.",
        remediation_steps=(
            "Entra admin center > Roles and administrators > Global Administrator > Assignments.",
            "Change each permanent assignment to Eligible with an expiry.",
            "Keep only confirmed break-glass accounts permanent.",
        ),
        doc_link=PIM_DOC, evaluate=_standing_role("tier0", "priv.standing_global_admin", "critical", only_ga=True),
        tags=("zero-trust",),
    ),
    SignalSpec(
        id="priv.standing_tier0", title="Permanent tier-0 role assignments",
        question="Which other tenant-takeover roles are held permanently?",
        why="Privileged Role Administrator, Privileged Authentication Administrator and the hybrid "
            "identity roles can all reach Global Administrator.",
        pillar="priv", severity="critical", weight=9, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        benchmarks=("MCSB PA-1",), impact=IMPACT_SATURATING, saturation=5,
        remediation="Make tier-0 roles PIM-eligible with approval.",
        doc_link=PIM_DOC, evaluate=_standing_role("tier0", "priv.standing_tier0", "critical", exclude_ga=True),
    ),
    SignalSpec(
        id="priv.standing_tier1", title="Permanent tier-1 role assignments",
        question="Which broad administrative roles are held permanently?",
        why="Tier-1 roles can reset credentials, consent to applications or weaken policy — several "
            "have a documented path to tier 0.",
        pillar="priv", severity="high", weight=7, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=10,
        remediation="Make tier-1 roles PIM-eligible with justification and a short activation window.",
        doc_link=PIM_DOC, evaluate=_standing_role("tier1", "priv.standing_tier1", "high"),
    ),
    SignalSpec(
        id="priv.too_many_global_admins", title="Too many Global Administrators",
        question="How many people can do absolutely anything?",
        why="Every Global Administrator is a complete tenant-takeover target.",
        pillar="priv", severity="high", weight=7, object_kind="tenant",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        benchmarks=("CIS 1.1.3",), impact=IMPACT_BINARY,
        remediation="Move day-to-day work to least-privileged roles; keep Global Administrator for "
                    "break-glass and rare tenant-wide changes.",
        doc_link=ROLE_DOC, evaluate=_too_many_global_admins,
    ),
    SignalSpec(
        id="priv.too_few_global_admins", title="Too few Global Administrators",
        question="Could the loss of one account leave the tenant unmanageable?",
        why="This is the one place where reducing privilege further makes things worse — a single "
            "Global Administrator is an availability risk, not a security win.",
        pillar="priv", severity="high", weight=5, object_kind="tenant",
        domains=("roles",), requires=("RoleManagement.Read.Directory",), impact=IMPACT_BINARY,
        remediation="Maintain at least two Global Administrators, including emergency access accounts.",
        doc_link="https://learn.microsoft.com/entra/identity/role-based-access-control/security-emergency-access",
        evaluate=_too_few_global_admins,
    ),
    SignalSpec(
        id="priv.privileged_guest", title="Guests holding directory roles",
        question="Do external identities have administrative power here?",
        why="A guest's credentials, MFA posture and device hygiene are governed by a tenant you do "
            "not control.",
        pillar="priv", severity="critical", weight=9, object_kind="user",
        domains=("roles", "people"), requires=("RoleManagement.Read.Directory", "User.Read.All"),
        impact=IMPACT_SATURATING, saturation=2,
        remediation="Remove the role, or replace the guest with a managed internal account.",
        doc_link=ROLE_DOC, evaluate=_privileged_guest,
    ),
    SignalSpec(
        id="priv.privileged_sp", title="Service principals holding privileged directory roles",
        question="Which workload identities can administer the directory?",
        why="A service principal has no MFA, is excluded from Conditional Access by default, and "
            "authenticates with a secret that can be exfiltrated.",
        pillar="priv", severity="critical", weight=9, object_kind="sp",
        domains=("roles", "apps"), requires=("RoleManagement.Read.Directory", "Application.Read.All"),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Replace the directory role with the narrowest Graph application permission that works.",
        doc_link=ROLE_DOC, evaluate=_privileged_sp,
    ),
    SignalSpec(
        id="priv.group_derived_privileged", title="Privilege granted through group membership",
        question="Who becomes an administrator by joining a group?",
        why="Group-derived privilege does not appear on the user's own role list, and whoever "
            "controls the membership controls the role.",
        pillar="priv", severity="medium", weight=5, object_kind="group",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Put the group under PIM for Groups, or assign the role directly to reviewable identities.",
        doc_link=PIM_DOC, evaluate=_group_derived,
    ),
    SignalSpec(
        id="priv.role_assignable_group_unprotected", title="Unprotected role-assignable groups",
        question="Which groups can grant a directory role but have nobody accountable for them?",
        why="A role-assignable group without an owner, or with a dynamic rule, is a privilege "
            "escalation path hiding in plain sight.",
        pillar="priv", severity="high", weight=6, object_kind="group",
        domains=("people",), requires=("Group.Read.All",),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Assign owners, convert dynamic membership to assigned, and place the group under PIM for Groups.",
        doc_link="https://learn.microsoft.com/entra/identity/role-based-access-control/groups-concept",
        evaluate=_role_assignable_group_unprotected,
    ),
    SignalSpec(
        id="priv.sync_account_privileged", title="Directory synchronisation account with extra roles",
        question="Does the on-premises sync account hold more than it needs?",
        why="Its credential lives on a domain-joined server and it already has broad directory "
            "write access — a favourite lateral-movement target.",
        pillar="priv", severity="critical", weight=8, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_BINARY,
        remediation="Remove every role except Directory Synchronization Accounts.",
        doc_link=ROLE_DOC, evaluate=_sync_account_privileged,
    ),
    SignalSpec(
        id="priv.dormant_privileged_user", title="Dormant privileged accounts",
        question="Which administrators never actually sign in?",
        why="Unused privilege is pure risk — nobody would notice it being abused.",
        pillar="priv", severity="high", weight=6, object_kind="user",
        domains=("roles", "people"), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Remove the role, or convert it to PIM-eligible so it must be activated to be used.",
        doc_link=PIM_DOC, evaluate=_dormant_privileged,
    ),
    SignalSpec(
        id="priv.sod_conflict", title="Separation-of-duties conflicts",
        question="Does any single principal hold a dangerous combination of roles?",
        why="Some role pairs together allow an administrator to escalate, or to make a change and "
            "then hide it.",
        pillar="priv", severity="high", weight=6, object_kind="user",
        domains=("roles",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Split the duties across two principals, or make one of the roles PIM-eligible with approval.",
        doc_link=ROLE_DOC, evaluate=_sod_conflict,
    ),
    SignalSpec(
        id="priv.pim_permanent_eligible", title="Permanently eligible privileged assignments",
        question="Which eligibility never expires?",
        why="Eligibility without an expiry is never reviewed; a time-bound one forces a decision.",
        pillar="priv", severity="medium", weight=4, object_kind="user",
        domains=("roles",), requires=("PrivilegedAccess.Read.AzureAD",), licence="p2",
        impact=IMPACT_SATURATING, saturation=10,
        remediation="Set an expiry on eligible assignments and pair it with an access review.",
        doc_link=PIM_DOC, evaluate=_stale_eligible,
    ),
    SignalSpec(
        id="priv.pim_not_used", title="PIM available but unused",
        question="The tenant is licensed for PIM — is any privilege actually eligible?",
        why="Paying for PIM while every assignment stays permanent gets none of the benefit.",
        pillar="priv", severity="medium", weight=5, object_kind="tenant",
        domains=("roles",), requires=("PrivilegedAccess.Read.AzureAD",), licence="p2",
        impact=IMPACT_BINARY,
        remediation="Convert privileged assignments to eligible, with approval and MFA on activation.",
        doc_link=PIM_DOC, evaluate=_pim_not_used,
    ),
]
