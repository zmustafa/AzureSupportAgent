"""Users and guests pillar — dormancy, half-finished offboarding, guest sprawl,
ownerless groups and external collaboration settings.

Dormancy signals depend on ``signInActivity``. When that was not collected they raise
:class:`SignalUnavailable`: reporting an account as *stale* because we could not read its
sign-in activity is a support ticket, not an insight.
"""
from __future__ import annotations

from typing import Any

from app.entra import guests, model
from app.entra.collectors.roles import privileged_principal_ids
from app.entra.collectors.tenant import (
    GUEST_ROLE_SAME_AS_MEMBER,
    guest_access_label,
)
from app.entra.signals import (
    IMPACT_BINARY,
    IMPACT_RATIO,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    enabled_guests,
    enabled_members,
    pop_enabled_guests,
    pop_enabled_members,
    pop_groups,
)

GUEST_DOC = "https://learn.microsoft.com/entra/external-id/what-is-b2b"
LIFECYCLE_DOC = "https://learn.microsoft.com/entra/id-governance/what-is-provisioning"


def _require_signin(data: dict[str, Any]) -> None:
    caps = domain(data, "people").get("capabilities") or {}
    if not caps.get("signin_activity"):
        raise SignalUnavailable(
            "Last sign-in activity was not collected (needs AuditLog.Read.All and Entra ID P1), "
            "so dormancy cannot be determined."
        )


def _stale_user(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_signin(data)
    out = []
    for u in enabled_members(data):
        if not u.get("signin_known") or not u.get("last_signin"):
            continue
        days = ctx.days_since(str(u.get("last_signin")))
        if days is None or days < ctx.stale_days:
            continue
        out.append(model.finding(
            signal_id="ppl.stale_user", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"{u.get('upn')} has not signed in for {days} days",
            detail="A dormant enabled account still holds its group memberships, licenses and access.",
            evidence={"last_signin": u.get("last_signin"), "days_since": days,
                      "department": u.get("department", ""), "licences": u.get("licence_count")},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _never_signed_in(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_signin(data)
    out = []
    for u in enabled_members(data):
        if not u.get("signin_known") or u.get("last_signin"):
            continue
        age = ctx.days_since(str(u.get("created_at") or ""))
        if age is None or age < 30:
            continue
        out.append(model.finding(
            signal_id="ppl.never_signed_in", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"{u.get('upn')} was created {age} days ago and has never signed in",
            detail="Either the joiner process did not complete, or this account was never needed.",
            evidence={"created_at": u.get("created_at"), "age_days": age,
                      "licences": u.get("licence_count")},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _disabled_with_access(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    people = domain(data, "people")
    roles = domain(data, "roles")
    users = {str(u["id"]): u for u in people.get("users") or [] if u.get("id")}
    role_holders: dict[str, list[str]] = {}
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles.get(bucket) or []:
            pid = str(row.get("principal_id") or "")
            if pid:
                role_holders.setdefault(pid, []).append(str(row.get("role_name") or ""))

    out = []
    for uid, u in users.items():
        if u.get("enabled"):
            continue
        retained: dict[str, Any] = {}
        if role_holders.get(uid):
            retained["directory_roles"] = sorted(set(role_holders[uid]))
        if u.get("licence_count"):
            retained["licences"] = u["licence_count"]
        if not retained:
            continue
        out.append(model.finding(
            signal_id="ppl.disabled_with_access", severity="high", pillar="ppl",
            object_kind="user", object_id=uid, object_name=u.get("upn") or uid,
            title=f"Disabled account {u.get('upn')} still holds access",
            detail="Offboarding stopped at 'disable'. A disabled account can be re-enabled by anyone "
                   "with User Administrator, and it keeps everything it had.",
            evidence={"retained": retained, "last_signin": u.get("last_signin", ""),
                      "on_prem_synced": u.get("on_prem_synced")},
            portal_link=model.portal_user(uid),
        ))
    return out


def _guest_stale(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    _require_signin(data)
    out = []
    for u in enabled_guests(data):
        if not u.get("signin_known"):
            continue
        days = ctx.days_since(str(u.get("last_signin") or ""))
        if u.get("last_signin") and (days is None or days < ctx.stale_days):
            continue
        out.append(model.finding(
            signal_id="ppl.guest_stale", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"Guest {u.get('upn')} has not signed in for "
                  f"{days if days is not None else 'over ' + str(ctx.stale_days)} days",
            detail="Guest access accumulates and is rarely revoked. Each stale guest is an external "
                   "identity with standing access to something.",
            evidence={"last_signin": u.get("last_signin") or "never", "days_since": days,
                      "invited_state": u.get("external_user_state"),
                      "created_at": u.get("created_at")},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _guest_pending(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for u in enabled_guests(data):
        if u.get("external_user_state") != "PendingAcceptance":
            continue
        age = ctx.days_since(str(u.get("external_state_changed_at") or u.get("created_at") or ""))
        if age is None or age < 30:
            continue
        out.append(model.finding(
            signal_id="ppl.guest_pending_invite", severity="low", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"Guest invitation to {u.get('mail') or u.get('upn')} has been pending for {age} days",
            detail="An unaccepted invitation is a directory object nobody needs.",
            evidence={"invited_at": u.get("external_state_changed_at") or u.get("created_at"),
                      "age_days": age},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _guest_sprawl(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    guests = len(enabled_guests(data))
    members = len(enabled_members(data))
    total = guests + members
    if not total or not guests:
        return []
    ratio = guests / total
    if ratio < ctx.guest_ratio_threshold:
        return []
    return [model.finding(
        signal_id="ppl.guest_sprawl", severity="medium", pillar="ppl",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"Guests are {ratio:.0%} of enabled accounts ({guests:,} of {total:,})",
        detail="A high guest ratio usually means invitations are ungoverned and nothing expires.",
        evidence={"guests": guests, "members": members, "ratio": round(ratio, 3),
                  "threshold": ctx.guest_ratio_threshold},
        discriminator=f"{ratio:.2f}",
    )]


def _guest_no_sponsor(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for u in enabled_guests(data):
        if u.get("department") or u.get("company_name") or u.get("job_title"):
            continue
        out.append(model.finding(
            signal_id="ppl.guest_no_sponsor", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"Guest {u.get('upn')} has no recorded sponsor or organization",
            detail="With no internal owner recorded, there is nobody to ask whether this access is "
                   "still needed at review time.",
            evidence={"company_name": u.get("company_name", ""), "department": u.get("department", ""),
                      "created_at": u.get("created_at")},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _ownerless_group(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    people = domain(data, "people")
    if not (people.get("capabilities") or {}).get("group_owners"):
        raise SignalUnavailable("Group owners were not collected.")
    out = []
    for g in people.get("groups") or []:
        if not g.get("owners_known") or g.get("owner_ids"):
            continue
        if g.get("on_prem_synced"):
            continue          # ownership is authoritative on-premises; fix it upstream
        out.append(model.finding(
            signal_id="ppl.ownerless_group", severity="medium", pillar="ppl",
            object_kind="group", object_id=str(g["id"]), object_name=g.get("display_name") or str(g["id"]),
            title=f"Group '{g.get('display_name')}' has no owner",
            detail="Ownerless groups accumulate members forever because nobody is asked to review them.",
            evidence={"is_assignable_to_role": g.get("is_assignable_to_role"),
                      "dynamic": g.get("dynamic"), "created_at": g.get("created_at"),
                      "security_enabled": g.get("security_enabled")},
            portal_link=model.portal_group(str(g["id"])),
        ))
    return out


def _dynamic_group_error(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    for g in domain(data, "people").get("groups") or []:
        if not g.get("dynamic"):
            continue
        state = str(g.get("membership_rule_state") or "")
        if state.lower() in ("on", ""):
            continue
        out.append(model.finding(
            signal_id="ppl.dynamic_group_error", severity="medium", pillar="ppl",
            object_kind="group", object_id=str(g["id"]), object_name=g.get("display_name") or str(g["id"]),
            title=f"Dynamic group '{g.get('display_name')}' is not processing membership ({state})",
            detail="Membership is frozen. If this group is used by a Conditional Access policy or "
                   "grants a role, the freeze silently changes who is protected or privileged.",
            evidence={"membership_rule_state": state, "membership_rule": g.get("membership_rule", ""),
                      "is_assignable_to_role": g.get("is_assignable_to_role")},
            portal_link=model.portal_group(str(g["id"])),
        ))
    return out


def _external_collab_open(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policy = domain(data, "tenant").get("cross_tenant_default") or {}
    if not policy.get("present"):
        raise SignalUnavailable("The cross-tenant access policy was not collected (needs Policy.Read.All).")
    if str(policy.get("b2b_inbound_users") or "").lower() != "allowed":
        return []
    return [model.finding(
        signal_id="ppl.external_collab_open", severity="high", pillar="ppl",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="Inbound B2B collaboration is allowed from any external tenant",
        detail="Any user in any Entra tenant can be invited into this directory with no partner-level "
               "restriction.",
        evidence={"b2b_inbound_users": policy.get("b2b_inbound_users"),
                  "b2b_inbound_apps": policy.get("b2b_inbound_apps"),
                  "inbound_trust_mfa": policy.get("inbound_trust_mfa")},
    )]


def _guest_invite_anyone(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policy = domain(data, "tenant").get("authorization_policy") or {}
    if not policy.get("present"):
        raise SignalUnavailable("The tenant authorization policy was not collected (needs Policy.Read.All).")
    setting = str(policy.get("allow_invites_from") or "")
    if setting not in ("everyone", "adminsGuestInvitersAndAllMembers"):
        return []
    return [model.finding(
        signal_id="ppl.guest_invite_anyone", severity="medium", pillar="ppl",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title=f"Guest invitations can be sent by '{setting}'",
        detail="When everyone (including guests) can invite guests, external access grows with no "
               "review and no owner.",
        evidence={"allow_invites_from": setting},
        discriminator=setting,
    )]


def _guest_full_directory_read(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    policy = domain(data, "tenant").get("authorization_policy") or {}
    if not policy.get("present"):
        raise SignalUnavailable("The tenant authorization policy was not collected (needs Policy.Read.All).")
    role_id = str(policy.get("guest_user_role_id") or "")
    if role_id != GUEST_ROLE_SAME_AS_MEMBER:
        return []
    return [model.finding(
        signal_id="ppl.guest_full_directory_read", severity="medium", pillar="ppl",
        object_kind="tenant", object_id=ctx.tenant_id or "tenant", object_name="Tenant",
        title="Guests have the same directory access as members",
        detail="Guests can enumerate every user, group and application in the directory — free "
               "reconnaissance for anyone who obtains one guest account.",
        evidence={"guest_user_role_id": role_id, "access_level": guest_access_label(role_id)},
    )]


# ------------------------------------------------------------------ guest hygiene (B2B)
def _guest_accepted_never_used(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Accepted the invitation, then never actually signed in.

    Distinct from `ppl.guest_pending_invite` (never accepted) and from `ppl.guest_stale`
    (used it once, long ago). This one is the clearest possible "this access was never
    needed" — somebody went to the trouble of accepting and then had no use for it.
    """
    _require_signin(data)
    out = []
    for u in guests.guests_of(domain(data, "people")):
        if not u.get("signin_known"):
            continue
        # Asked directly rather than via the lifecycle string: a guest that was disabled AFTER
        # never using its access still holds that access, and displaying it as "Disabled"
        # must not delete the finding.
        if not guests.never_used(u):
            continue
        invited = guests.invited_at(u)
        out.append(model.finding(
            signal_id="ppl.guest_accepted_never_used", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"Guest {u.get('mail') or u.get('upn')} accepted the invitation but has never signed in",
            detail="The invitation was accepted, so the identity is live and carries whatever it "
                   "was granted — but nobody has ever used it. This is standing external access "
                   "that was never needed.",
            evidence={"invited_at": invited, "invited_days_ago": ctx.days_since(invited),
                      "accepted_at": guests.accepted_at(u),
                      "domain": guests.guest_domain(u)},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _guest_human_dormant(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """No human sign-in for the guest window, but the token is still being refreshed.

    `lastNonInteractiveSignInDateTime` moves on token refresh, so a guest who left the
    partner months ago keeps looking active on any dashboard that reads "last sign-in"
    without asking WHICH KIND. That is precisely the account an attacker inherits when a
    departed contractor's device or session is not revoked.
    """
    _require_signin(data)
    out = []
    for u in guests.guests_of(domain(data, "people")):
        if not u.get("enabled") or not u.get("signin_known"):
            continue
        human = guests.last_human_signin(u)
        machine = str(u.get("last_noninteractive_signin") or "")
        if not machine:
            continue
        human_age = ctx.days_since(human) if human else None
        machine_age = ctx.days_since(machine)
        # Token still warm, human long gone.
        if machine_age is None or machine_age >= ctx.guest_stale_days:
            continue
        if human_age is not None and human_age < ctx.guest_stale_days:
            continue
        out.append(model.finding(
            signal_id="ppl.guest_human_dormant", severity="medium", pillar="ppl",
            object_kind="user", object_id=str(u["id"]), object_name=u.get("upn") or str(u["id"]),
            title=f"Guest {u.get('mail') or u.get('upn')} has a live token but no human sign-in for "
                  f"{human_age if human_age is not None else 'ever'}"
                  + (" days" if human_age is not None else ""),
            detail="Non-interactive activity is a refresh token, not a person. This identity looks "
                   "active on any report that does not separate the two, while nobody has "
                   "interactively signed in.",
            evidence={"last_human_signin": human or "never", "last_human_days_ago": human_age,
                      "last_noninteractive_signin": machine, "last_noninteractive_days_ago": machine_age,
                      "domain": guests.guest_domain(u)},
            portal_link=model.portal_user(str(u["id"])),
        ))
    return out


def _guest_consumer_domain(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Guests on free consumer mailboxes, aggregated per domain.

    One row per person would be 72 near-identical findings on a real tenant. The decision is
    per domain anyway: nobody can de-provision a Gmail address when an engagement ends,
    because there is no counterparty organization to ask.
    """
    rows = [u for u in guests.guests_of(domain(data, "people")) if u.get("enabled")]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for u in rows:
        dom = guests.guest_domain(u)
        if guests.classify_domain(dom) != guests.CLASS_CONSUMER:
            continue
        buckets.setdefault(dom, []).append(u)
    out = []
    for dom, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        out.append(model.finding(
            signal_id="ppl.guest_consumer_domain", severity="medium", pillar="ppl",
            object_kind="domain", object_id=dom, object_name=dom,
            title=f"{len(items):,} enabled guest(s) use the consumer mailbox domain {dom}",
            detail="A consumer address has no owning organization, so there is nobody to notify "
                   "when the engagement ends and no partner admin who can disable it. Access "
                   "outlives the relationship by default.",
            evidence={"domain": dom, "guests": len(items),
                      "sample": [str(i.get("upn") or i.get("mail") or "") for i in items[:10]]},
            discriminator=dom,
        ))
    return out


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="ppl.guest_accepted_never_used", title="Guests who accepted but never signed in",
        question="Which external identities were stood up and never used?",
        why="The invitation was accepted, so the identity is live and carries whatever it was "
            "granted — but nobody has ever used it.",
        pillar="ppl", severity="medium", weight=5, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_guests,
        remediation="Remove the guest. If the access is still wanted, re-invite when it is needed.",
        doc_link=GUEST_DOC, evaluate=_guest_accepted_never_used,
    ),
    SignalSpec(
        id="ppl.guest_human_dormant", title="Guests kept alive by a token, not a person",
        question="Which guests look active only because a refresh token keeps cycling?",
        why="Non-interactive activity is not a human. These accounts pass every 'last sign-in' "
            "report while nobody has actually signed in.",
        pillar="ppl", severity="medium", weight=6, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_guests,
        remediation="Revoke the sign-in sessions, then disable or remove the guest.",
        doc_link=GUEST_DOC, evaluate=_guest_human_dormant,
    ),
    SignalSpec(
        id="ppl.guest_consumer_domain", title="Guests on consumer mailbox domains",
        question="Which external access has no owning organization behind it?",
        why="Nobody can de-provision a personal Gmail address when an engagement ends — there is "
            "no partner admin to ask and no leaver process to inherit.",
        pillar="ppl", severity="medium", weight=5, object_kind="domain",
        domains=("people",), requires=("User.Read.All",),
        impact=IMPACT_BINARY,
        remediation="Require a corporate address for external collaboration, or time-box these "
                    "guests with an access review.",
        doc_link=GUEST_DOC, evaluate=_guest_consumer_domain,
    ),
    SignalSpec(
        id="ppl.stale_user", title="Dormant member accounts",
        question="Which enabled accounts nobody uses?",
        why="A dormant enabled account keeps every group, licence and permission it ever had, and "
            "nobody would notice it being used.",
        pillar="ppl", severity="medium", weight=5, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Disable, then delete after the retention window; reclaim the licence.",
        doc_link=LIFECYCLE_DOC, evaluate=_stale_user,
    ),
    SignalSpec(
        id="ppl.never_signed_in", title="Accounts that never signed in",
        question="Which accounts were created and then abandoned?",
        why="Either onboarding did not complete, or the account was never needed — both leave a "
            "credentialed identity lying around.",
        pillar="ppl", severity="medium", weight=4, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Complete onboarding or remove the account and reclaim its licence.",
        doc_link=LIFECYCLE_DOC, evaluate=_never_signed_in,
    ),
    SignalSpec(
        id="ppl.disabled_with_access", title="Disabled accounts that retain access",
        question="Whose offboarding stopped halfway?",
        why="A disabled account keeps its roles, groups and licences, and anyone with User "
            "Administrator can re-enable it in one click.",
        pillar="ppl", severity="high", weight=7, object_kind="user",
        domains=("people", "roles"), requires=("User.Read.All", "RoleManagement.Read.Directory"),
        benchmarks=("MCSB IM-3",), impact=IMPACT_RATIO, population=pop_enabled_members,
        remediation="Remove roles, group memberships and licences as part of offboarding, not only the disable.",
        remediation_steps=(
            "Remove all directory role assignments for the account.",
            "Remove security-group memberships that grant access.",
            "Reclaim licences, then delete after the retention window.",
        ),
        doc_link=LIFECYCLE_DOC, evaluate=_disabled_with_access, tags=("quick-win",),
    ),
    SignalSpec(
        id="ppl.guest_stale", title="Dormant guest accounts",
        question="Which external identities stopped using their access but kept it?",
        why="Guest access accumulates and is almost never revoked.",
        pillar="ppl", severity="medium", weight=5, object_kind="user",
        domains=("people",), requires=("AuditLog.Read.All",), licence="p1",
        impact=IMPACT_RATIO, population=pop_enabled_guests,
        remediation="Run a guest access review and remove the accounts nobody claims.",
        doc_link=GUEST_DOC, evaluate=_guest_stale,
    ),
    SignalSpec(
        id="ppl.guest_pending_invite", title="Long-pending guest invitations",
        question="Which invitations were never accepted?",
        why="An unaccepted invitation is a directory object with no purpose.",
        pillar="ppl", severity="low", weight=2, object_kind="user",
        domains=("people",), requires=("User.Read.All",),
        impact=IMPACT_RATIO, population=pop_enabled_guests,
        remediation="Delete guest objects whose invitation was never accepted.",
        doc_link=GUEST_DOC, evaluate=_guest_pending,
    ),
    SignalSpec(
        id="ppl.guest_sprawl", title="Guest accounts are a large share of the directory",
        question="How much of this directory is external?",
        why="A high guest ratio is a reliable sign that invitations are ungoverned.",
        pillar="ppl", severity="medium", weight=4, object_kind="tenant",
        domains=("people",), requires=("User.Read.All",), impact=IMPACT_BINARY,
        remediation="Introduce guest access reviews and expiry through entitlement management.",
        doc_link=GUEST_DOC, evaluate=_guest_sprawl,
    ),
    SignalSpec(
        id="ppl.guest_no_sponsor", title="Guests with no sponsor recorded",
        question="Who invited this external user, and who owns them now?",
        why="Without an internal owner there is nobody to ask at review time, so the access is renewed by default.",
        pillar="ppl", severity="medium", weight=4, object_kind="user",
        domains=("people",), requires=("User.Read.All",),
        impact=IMPACT_RATIO, population=pop_enabled_guests,
        remediation="Record a sponsor on every guest, or provision guests through access packages that require one.",
        doc_link=GUEST_DOC, evaluate=_guest_no_sponsor,
    ),
    SignalSpec(
        id="ppl.ownerless_group", title="Groups with no owner",
        question="Which groups has nobody accepted responsibility for?",
        why="Ownerless groups accumulate members indefinitely because no one is ever asked to review them.",
        pillar="ppl", severity="medium", weight=4, object_kind="group",
        domains=("people",), requires=("Group.Read.All",),
        impact=IMPACT_RATIO, population=pop_groups,
        remediation="Assign at least one active owner; prefer two so a departure does not orphan the group.",
        doc_link="https://learn.microsoft.com/entra/identity/users/groups-self-service-management",
        evaluate=_ownerless_group,
    ),
    SignalSpec(
        id="ppl.dynamic_group_error", title="Dynamic groups not processing membership",
        question="Which dynamic groups are frozen?",
        why="A paused rule silently freezes membership. If the group drives Conditional Access or a "
            "role assignment, it silently freezes who is protected or privileged too.",
        pillar="ppl", severity="medium", weight=4, object_kind="group",
        domains=("people",), requires=("Group.Read.All",),
        impact=IMPACT_RATIO, population=pop_groups,
        remediation="Fix the membership rule and resume processing.",
        doc_link="https://learn.microsoft.com/entra/identity/users/groups-dynamic-membership",
        evaluate=_dynamic_group_error,
    ),
    SignalSpec(
        id="ppl.external_collab_open", title="Inbound collaboration open to any tenant",
        question="Which organizations can be invited into this directory?",
        why="Unrestricted inbound B2B means any Entra user anywhere can be given access.",
        pillar="ppl", severity="high", weight=6, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",), impact=IMPACT_BINARY,
        remediation="Configure cross-tenant access settings with an allow-list of partner tenants.",
        doc_link="https://learn.microsoft.com/entra/external-id/cross-tenant-access-overview",
        evaluate=_external_collab_open,
    ),
    SignalSpec(
        id="ppl.guest_invite_anyone", title="Anyone can invite guests",
        question="Who can add an external identity to this directory?",
        why="When members — or guests — can invite guests, external access grows with no review.",
        pillar="ppl", severity="medium", weight=4, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",),
        benchmarks=("CIS 5.1.6",), impact=IMPACT_BINARY,
        remediation="Restrict invitations to administrators and the Guest Inviter role.",
        doc_link=GUEST_DOC, evaluate=_guest_invite_anyone,
    ),
    SignalSpec(
        id="ppl.guest_full_directory_read", title="Guests can read the whole directory",
        question="How much can a guest enumerate?",
        why="Member-equivalent guest access hands any external identity a complete map of your "
            "users, groups and applications.",
        pillar="ppl", severity="medium", weight=5, object_kind="tenant",
        domains=("tenant",), requires=("Policy.Read.All",),
        benchmarks=("CIS 5.1.6",), impact=IMPACT_BINARY,
        remediation="Set guest access to 'Limited' or 'Restricted' in External collaboration settings.",
        doc_link="https://learn.microsoft.com/entra/identity/users/users-restrict-guest-permissions",
        evaluate=_guest_full_directory_read, tags=("quick-win",),
    ),
]
