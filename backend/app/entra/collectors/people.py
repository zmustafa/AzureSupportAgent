"""Users, guests and groups collector.

Two deliberate design choices:

1. **Two-pass user collection.** ``signInActivity`` cannot be freely combined with most
   ``$filter`` expressions and forces a slower query path, so the inventory is collected in
   a fast pass and last-sign-in is merged from a second, P1-gated pass. If the second pass
   fails, dormancy signals report *unknown* rather than falsely reporting *dormant* — an
   account wrongly marked stale is a support ticket, not an insight.

2. **MFA truth comes from the registration report**, not from a per-user
   ``/authentication/methods`` scan. ``/reports/authenticationMethods/userRegistrationDetails``
   returns ``isMfaRegistered`` / ``isMfaCapable`` / ``isPasswordlessCapable`` / ``methodsRegistered``
   for the entire tenant in one paged call. The existing identity collector's capped per-user
   scan is exactly the pattern this retires.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.entra import guests as guests_mod
from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, batch_collection, clip, guarded
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError, GraphRequest

DOMAIN = "people"

_USER_SELECT = [
    "id", "displayName", "userPrincipalName", "userType", "accountEnabled",
    "createdDateTime", "department", "companyName", "jobTitle", "employeeId",
    "onPremisesSyncEnabled", "onPremisesExtensionAttributes", "externalUserState",
    "externalUserStateChangeDateTime", "mail", "usageLocation", "assignedLicenses",
    # "Invitation" for a B2B guest, "LocalAccount"/"" otherwise. Distinguishes an invited
    # external identity from a guest-typed account that was created some other way, which
    # matters because only the former has an invitation to chase.
    "creationType",
]

_GROUP_SELECT = [
    "id", "displayName", "description", "groupTypes", "securityEnabled", "mailEnabled",
    "isAssignableToRole", "membershipRule", "membershipRuleProcessingState",
    "createdDateTime", "visibility", "onPremisesSyncEnabled",
]

# Weak (phishable) authentication methods, as reported by the registration report.
_WEAK_METHODS = {"mobilePhone", "alternateMobilePhone", "officePhone", "sms", "voice", "email"}
_PHISH_RESISTANT = {"fido2SecurityKey", "windowsHelloForBusiness", "passKeyDeviceBound",
                    "passKeyDeviceBoundAuthenticator", "x509Certificate",
                    "passKeyDeviceBoundWindowsHello", "microsoftAuthenticatorPasswordless"}


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        truncated = False

        # --- pass 1: inventory -------------------------------------------------
        # The four reads below cover the same population but are independent of each other,
        # and each is a full paged enumeration. Run serially they add up; issued together they
        # cost about as much as the slowest one. `return_exceptions` keeps the existing
        # fail-open contract: a pass that is unlicensed or unpermitted degrades on its own
        # rather than taking the domain with it.
        await ctx.say("info", "People: collecting users, sign-in activity, "
                              "registration details and sponsors…")
        inventory, activity_res, registration_res, sponsors_res = await asyncio.gather(
            client.get_all("/users", select=_USER_SELECT, top=999, max_items=ctx.max_users),
            client.get_all("/users", select=["id", "signInActivity"], top=999,
                           max_items=ctx.max_users),
            client.get_all("/reports/authenticationMethods/userRegistrationDetails", top=999),
            client.get_all("/users", select=["id"], top=999, max_items=ctx.max_users,
                           filter="userType eq 'Guest'",
                           expand="sponsors($select=id,displayName)"),
            return_exceptions=True,
        )
        if isinstance(inventory, BaseException):
            raise inventory
        users_raw, user_trunc = inventory
        truncated = truncated or user_trunc
        if user_trunc:
            notes.append(f"User collection capped at {ctx.max_users:,} — counts are a lower bound.")
        await ctx.say("ok", f"People: {len(users_raw):,} user(s)")

        users: dict[str, dict[str, Any]] = {}
        for u in users_raw:
            u = as_dict(u)
            uid = str(u.get("id") or "")
            if not uid:
                continue
            ext = as_dict(u.get("onPremisesExtensionAttributes"))
            users[uid] = {
                "id": uid,
                "upn": u.get("userPrincipalName", "") or "",
                "display_name": u.get("displayName", "") or "",
                "mail": u.get("mail", "") or "",
                "user_type": u.get("userType", "") or "Member",
                "enabled": bool(u.get("accountEnabled")),
                "created_at": u.get("createdDateTime", "") or "",
                "department": u.get("department", "") or "",
                "company_name": u.get("companyName", "") or "",
                "job_title": u.get("jobTitle", "") or "",
                "employee_id": u.get("employeeId", "") or "",
                "usage_location": u.get("usageLocation", "") or "",
                "on_prem_synced": bool(u.get("onPremisesSyncEnabled")),
                "extension_attributes": {k: v for k, v in ext.items() if v},
                "external_user_state": u.get("externalUserState", "") or "",
                "external_state_changed_at": u.get("externalUserStateChangeDateTime", "") or "",
                "creation_type": u.get("creationType", "") or "",
                "licence_count": len(as_list(u.get("assignedLicenses"))),
                # filled by later passes
                "last_signin": "",
                "last_noninteractive_signin": "",
                "last_successful_signin": "",
                "signin_known": False,
                "mfa_registered": None,
                "mfa_capable": None,
                "passwordless_capable": None,
                "sspr_registered": None,
                "methods": [],
                "phishing_resistant": None,
                "is_admin_reported": None,
                "manager_id": "",
                # Real sponsor relationship (guests only, filled by a later pass). Distinct
                # from the department/companyName proxy: an empty list here means "nobody is
                # accountable for this guest", which is a reviewable fact.
                "sponsors": [],
                "sponsors_known": False,
            }

        # --- pass 2: sign-in activity (P1) -------------------------------------
        signin_available = False
        try:
            if isinstance(activity_res, BaseException):
                raise activity_res
            activity, act_trunc = activity_res
            truncated = truncated or act_trunc
            merged = 0
            for row in activity:
                row = as_dict(row)
                uid = str(row.get("id") or "")
                act = as_dict(row.get("signInActivity"))
                if uid in users:
                    users[uid]["last_signin"] = act.get("lastSignInDateTime", "") or ""
                    users[uid]["last_noninteractive_signin"] = act.get("lastNonInteractiveSignInDateTime", "") or ""
                    # Verified present on v1.0. Distinguishes "last attempt" from "last time
                    # they actually got in" — a run of failures leaves lastSignInDateTime
                    # moving while nobody has successfully authenticated for months.
                    users[uid]["last_successful_signin"] = act.get("lastSuccessfulSignInDateTime", "") or ""
                    users[uid]["signin_known"] = True
                    merged += 1
            signin_available = merged > 0
            await ctx.say("ok", f"People: sign-in activity for {merged:,} user(s)")
        except GraphPermissionError as exc:
            notes.append(
                "Last sign-in activity unavailable (needs AuditLog.Read.All and Entra ID P1) — "
                f"dormancy is reported as unknown, not stale. {exc.message[:120]}"
            )
        except GraphError as exc:
            notes.append(f"Last sign-in activity unavailable: {clip(exc, 160)} — dormancy reported as unknown.")

        # --- pass 3: MFA registration report (P1) ------------------------------
        mfa_available = False
        try:
            if isinstance(registration_res, BaseException):
                raise registration_res
            reg, _ = registration_res
            for row in reg:
                row = as_dict(row)
                uid = str(row.get("id") or "")
                if uid not in users:
                    continue
                methods = [str(m) for m in as_list(row.get("methodsRegistered"))]
                users[uid].update({
                    "mfa_registered": bool(row.get("isMfaRegistered")),
                    "mfa_capable": bool(row.get("isMfaCapable")),
                    "passwordless_capable": bool(row.get("isPasswordlessCapable")),
                    "sspr_registered": bool(row.get("isSsprRegistered")),
                    "methods": methods,
                    "phishing_resistant": any(m in _PHISH_RESISTANT for m in methods),
                    "is_admin_reported": bool(row.get("isAdmin")),
                    # An empty method list and an absent report row look identical once
                    # merged. Without this flag a user the report has not caught up with
                    # is indistinguishable from one who has genuinely registered nothing,
                    # and the registration gap silently inflates.
                    "registration_reported": True,
                })
            mfa_available = bool(reg)
            await ctx.say("ok", f"People: registration details for {len(reg):,} user(s)")
        except GraphPermissionError as exc:
            notes.append(
                "MFA registration report unavailable (needs AuditLog.Read.All / "
                f"UserAuthenticationMethod.Read.All and Entra ID P1). {exc.message[:120]}"
            )
        except GraphError as exc:
            notes.append(f"MFA registration report unavailable: {clip(exc, 160)}")

        # --- pass 4: guest sponsors --------------------------------------------
        # ONE paged query for the whole guest population, not a per-guest lookup: on a tenant
        # with 1,700 guests the per-object shape would be 1,700 round trips for a field most
        # screens only aggregate. `$expand=sponsors` on a `userType eq 'Guest'` filter is
        # accepted by v1.0 without ConsistencyLevel (verified live).
        #
        # `sponsors_known` exists for the usual reason: an empty sponsor list and a pass that
        # never ran look identical once merged, and "nobody is accountable for this guest" is
        # a finding while "we did not ask" is not.
        try:
            if isinstance(sponsors_res, BaseException):
                raise sponsors_res
            sponsored, sp_trunc = sponsors_res
            truncated = truncated or sp_trunc
            seen = 0
            for row in sponsored:
                row = as_dict(row)
                uid = str(row.get("id") or "")
                if uid not in users:
                    continue
                users[uid]["sponsors"] = [
                    {"id": str(as_dict(s).get("id") or ""),
                     "display_name": str(as_dict(s).get("displayName") or "")}
                    for s in as_list(row.get("sponsors"))
                ]
                users[uid]["sponsors_known"] = True
                seen += 1
            await ctx.say("ok", f"People: sponsor relationships for {seen:,} guest(s)")
        except GraphPermissionError as exc:
            notes.append(
                "Guest sponsors unavailable (needs User.Read.All) — accountability is reported "
                f"as unknown, not absent. {exc.message[:120]}"
            )
        except GraphError as exc:
            notes.append(f"Guest sponsors unavailable: {clip(exc, 160)} — reported as unknown.")

        # --- pass 5: resolve guest domains to partner tenants -------------------
        # The guest population is keyed by EMAIL DOMAIN; the cross-tenant access policy is
        # keyed by TENANT ID. Without this join nobody can answer "we have N guests from
        # this company and no policy governing them", which is the whole point of the
        # partner view.
        #
        # One `$batch` per 20 domains rather than a call each: ~400 distinct domains at
        # production scale is 20 round trips, not 400. The function works on v1.0 with the
        # scopes already held, and it returns the partner's own display name
        # ("Fabrikam"), which beats showing a bare domain.
        guest_domains = sorted({
            guests_mod.guest_domain(u) for u in users.values()
            if str(u.get("user_type") or "").lower() == "guest"
        } - {""})
        tenant_by_domain: dict[str, dict[str, str]] = {}
        if guest_domains:
            await ctx.say("info", f"People: resolving {len(guest_domains):,} guest domain(s) to partner tenants…")
            resolved = 0
            try:
                for start in range(0, len(guest_domains), 20):
                    window = guest_domains[start:start + 20]
                    reqs = [
                        GraphRequest(
                            id=str(n),
                            url="/tenantRelationships/findTenantInformationByDomainName"
                                f"(domainName='{d}')",
                        )
                        for n, d in enumerate(window)
                    ]
                    for req, resp in zip(reqs, await client.batch(reqs)):
                        dom = window[int(req.id)]
                        body = resp.body if isinstance(resp.body, dict) else {}
                        tid = str(body.get("tenantId") or "")
                        if not resp.ok or not tid:
                            # A consumer domain or an org with no Entra tenant simply has no
                            # answer. That is a fact about the domain, not a failure.
                            continue
                        tenant_by_domain[dom] = {
                            "tenant_id": tid,
                            "display_name": str(body.get("displayName") or ""),
                            "default_domain": str(body.get("defaultDomainName") or ""),
                        }
                        resolved += 1
                await ctx.say("ok", f"People: {resolved:,} of {len(guest_domains):,} guest domain(s) "
                                    "map to a partner tenant")
            except GraphPermissionError as exc:
                notes.append("Guest domains could not be resolved to partner tenants "
                             f"({clip(exc.message, 120)}) — partner governance shows as unknown.")
            except GraphError as exc:
                notes.append(f"Guest domain resolution unavailable: {clip(exc, 160)}.")

        # --- groups -------------------------------------------------------------
        await ctx.say("info", "People: collecting groups…")
        groups_raw, grp_trunc = await client.get_all(
            "/groups", select=_GROUP_SELECT, top=999, max_items=ctx.max_groups
        )
        truncated = truncated or grp_trunc
        if grp_trunc:
            notes.append(f"Group collection capped at {ctx.max_groups:,}.")

        groups: dict[str, dict[str, Any]] = {}
        for g in groups_raw:
            g = as_dict(g)
            gid = str(g.get("id") or "")
            if not gid:
                continue
            gtypes = [str(t) for t in as_list(g.get("groupTypes"))]
            groups[gid] = {
                "id": gid,
                "display_name": g.get("displayName", "") or "",
                "description": g.get("description", "") or "",
                "group_types": gtypes,
                "dynamic": "DynamicMembership" in gtypes,
                "unified": "Unified" in gtypes,
                "security_enabled": bool(g.get("securityEnabled")),
                "mail_enabled": bool(g.get("mailEnabled")),
                "is_assignable_to_role": bool(g.get("isAssignableToRole")),
                "membership_rule": g.get("membershipRule", "") or "",
                "membership_rule_state": g.get("membershipRuleProcessingState", "") or "",
                "created_at": g.get("createdDateTime", "") or "",
                "visibility": g.get("visibility", "") or "",
                "on_prem_synced": bool(g.get("onPremisesSyncEnabled")),
                "owner_ids": [],
                "owners_known": False,
            }
        await ctx.say("ok", f"People: {len(groups):,} group(s)")

        # --- group owners (batched, uncapped) -----------------------------------
        if groups:
            await ctx.say("info", f"People: resolving owners for {len(groups):,} group(s)…")
            owners, owner_trunc, forbidden = await batch_collection(
                client,
                list(groups),
                lambda gid: f"/groups/{gid}/owners?$select=id,displayName&$top=50",
                cap=ctx.max_owner_lookups or None,
                ctx=ctx,
                label="People: group owners",
            )
            if owner_trunc:
                truncated = True
                notes.append(
                    f"Group owner lookups capped at {ctx.max_owner_lookups:,}; "
                    "ownerless detection covers that subset only."
                )
            if forbidden:
                notes.append(f"{forbidden} group owner lookup(s) were forbidden.")
            for gid, rows in owners.items():
                groups[gid]["owner_ids"] = [str(as_dict(r).get("id") or "") for r in rows if as_dict(r).get("id")]
                groups[gid]["owners_known"] = True
            await ctx.say("ok", f"People: owners resolved for {len(owners):,} group(s)")

        guests = sum(1 for u in users.values() if u["user_type"] == "Guest")
        enabled_members = sum(1 for u in users.values() if u["enabled"] and u["user_type"] == "Member")
        data = {
            "users": list(users.values()),
            "groups": list(groups.values()),
            # domain -> {tenant_id, display_name, default_domain}. Only domains that actually
            # resolve appear; a consumer domain or an org with no Entra tenant is simply
            # absent, which the rollup reads as "no partner tenant", not as a failure.
            "guest_domain_tenants": tenant_by_domain,
            "capabilities": {
                "signin_activity": signin_available,
                "mfa_registration_report": mfa_available,
                "group_owners": bool(groups) and any(g["owners_known"] for g in groups.values()),
                # Distinguishes "no guest has a sponsor" from "the sponsor pass never ran".
                "guest_sponsors": any(u.get("sponsors_known") for u in users.values()),
                "guest_domain_tenants": bool(tenant_by_domain),
            },
            "counts": {
                "users": len(users),
                "members": len(users) - guests,
                "guests": guests,
                "enabled_members": enabled_members,
                "disabled": sum(1 for u in users.values() if not u["enabled"]),
                "groups": len(groups),
                "role_assignable_groups": sum(1 for g in groups.values() if g["is_assignable_to_role"]),
            },
        }
        status = model.STATUS_PARTIAL if (notes or truncated) else model.STATUS_OK
        blockers = []
        if truncated:
            blockers.append(model.blocker(
                model.BLOCKER_CAP,
                f"User collection stopped at {ctx.max_users:,} accounts.",
                scope=f"{ctx.max_users:,} users",
                impact="Every people count is a lower bound.",
            ))
        return model.domain_payload(
            DOMAIN, data, status=status,
            item_count=len(users) + len(groups), truncated=truncated, notes=notes,
            blockers=blockers,
        )

    return await guarded(DOMAIN, ctx, _run)


# ------------------------------------------------------------------------- helpers
async def expand_groups(
    client: GraphClient, group_ids: list[str], *, cap: int = 500
) -> tuple[dict[str, list[str]], bool, list[str]]:
    """Transitive user members for a **bounded** set of groups.

    Used by the roles and Conditional Access collectors for the groups they actually
    reference (role-granting groups, policy-targeted groups) rather than building a
    tenant-wide membership index no screen would read.

    Returns ``(group_id -> [user ids], truncated, notes)``.
    """
    notes: list[str] = []
    wanted = list(dict.fromkeys(g for g in group_ids if g))
    truncated = False
    if len(wanted) > cap:
        wanted = wanted[:cap]
        truncated = True
        notes.append(f"Group membership expansion capped at {cap} group(s).")
    if not wanted:
        return {}, truncated, notes
    members, _, forbidden = await batch_collection(
        client,
        wanted,
        lambda gid: f"/groups/{gid}/transitiveMembers/microsoft.graph.user?$select=id&$top=999",
    )
    if forbidden:
        notes.append(f"{forbidden} group membership expansion(s) were forbidden.")
    return (
        {gid: [str(as_dict(m).get("id") or "") for m in rows if as_dict(m).get("id")] for gid, rows in members.items()},
        truncated,
        notes,
    )


def weak_only(methods: list[str]) -> bool:
    """True when every registered method is phishable (SMS / voice / email only)."""
    if not methods:
        return False
    return all(m in _WEAK_METHODS for m in methods)
