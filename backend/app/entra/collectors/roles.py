"""Directory roles, assignments and (best-effort) PIM schedules collector.

Extends the tiering already proven in ``identity/pim.py`` and replaces its demo-only
eligibility data with the real Graph datasets where the tenant is licensed for them.

Permanence is the flag that matters. ``/roleManagement/directory/roleAssignments`` returns
every *currently active* assignment — including one that a PIM user activated five minutes
ago. Only ``roleAssignmentSchedules`` distinguishes ``Assigned`` (permanent) from
``Activated`` (a time-bound activation). When that dataset is unavailable (no P2, or the
scope was not consented) we still report standing access, because an active assignment *is*
standing access right now, but we say plainly in the finding that PIM schedule data was
unavailable rather than quietly asserting permanence.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, guarded
from app.entra.collectors.people import expand_groups
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "roles"

GLOBAL_ADMIN = "global administrator"
DIRECTORY_SYNC_ROLE = "directory synchronization accounts"

# Tier 0 — a holder can take over the tenant.
TIER0 = {
    "global administrator",
    "privileged role administrator",
    "privileged authentication administrator",
    "partner tier2 support",
    "domain name administrator",
    "hybrid identity administrator",
    "directory synchronization accounts",
}
# Tier 1 — broad administrative power, or a documented path to tier 0.
TIER1 = {
    "application administrator",
    "cloud application administrator",
    "security administrator",
    "user administrator",
    "authentication administrator",
    "conditional access administrator",
    "exchange administrator",
    "sharepoint administrator",
    "teams administrator",
    "intune administrator",
    "helpdesk administrator",
    "password administrator",
    "billing administrator",
    "groups administrator",
    "directory writers",
    "partner tier1 support",
    "external identity provider administrator",
    "security operator",
    "privileged authentication administrator",
}

# Separation-of-duties rules evaluated over each principal's EFFECTIVE role set
# (direct + eligible + group-derived). Names are lowercase display names.
SOD_RULES: list[tuple[str, str, str]] = [
    ("global administrator", "privileged role administrator",
     "Unnecessary concentration — either role alone can already grant any other role."),
    ("application administrator", "privileged role administrator",
     "Consent-grant plus role-grant is a direct path to tenant takeover."),
    ("cloud application administrator", "privileged role administrator",
     "Consent-grant plus role-grant is a direct path to tenant takeover."),
    ("user administrator", "authentication administrator",
     "Can reset credentials and change account state — full account takeover of any user."),
    ("security administrator", "conditional access administrator",
     "Can weaken a control and suppress the signal that would reveal it."),
    ("privileged authentication administrator", "user administrator",
     "Can reset any administrator's credentials and manage the accounts."),
]


def tier_of(role_name: str) -> str:
    name = (role_name or "").strip().lower()
    if name in TIER0:
        return "tier0"
    if name in TIER1:
        return "tier1"
    return "tier2"


def is_privileged(role_name: str, definition: dict[str, Any] | None = None) -> bool:
    """Privileged when Microsoft flags it, or when it is in our tier-0/tier-1 sets."""
    if definition and definition.get("isPrivileged"):
        return True
    return tier_of(role_name) in ("tier0", "tier1")


def _principal_type(obj: dict[str, Any]) -> str:
    odata = str(obj.get("@odata.type") or "").lower()
    if "serviceprincipal" in odata:
        return "ServicePrincipal"
    if "group" in odata:
        return "Group"
    if "user" in odata:
        return "User"
    return ""


def _is_licence_error(exc: GraphError) -> bool:
    """Does this failure mean 'buy a license' rather than 'grant a permission'?

    Microsoft is not consistent about the status code: PIM reports a missing license as a
    **400 with a message**, while lifecycle workflows report one as a **403** — the same
    status as a genuine consent failure. The status is therefore not the signal; the message
    is. Reading the 403 as a consent problem told operators to grant a scope they already
    held, which is the one piece of advice guaranteed to waste their time.

    Both a license word and a product word are required, so an ordinary permission error
    that merely mentions licensing is not swallowed.
    """
    msg = (exc.message or "").lower()
    if "license" not in msg and "licence" not in msg:
        return False
    if exc.status not in (400, 403):
        return False
    return any(word in msg for word in ("p2", "governance", "premium", "insufficient"))


def _pim_note(feature: str, exc: GraphError, scope: str) -> str:
    if _is_licence_error(exc):
        return f"{feature} unavailable: this tenant is not licensed for Entra ID P2 / ID Governance."
    if isinstance(exc, GraphPermissionError):
        return f"{feature} unavailable: {scope} is not granted ({exc.message[:110]})"
    return f"{feature} unavailable: {str(exc)[:150]}"


async def _resolve_principals(
    client: GraphClient, ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Bulk-resolve principal ids to display names.

    Needed because the PIM schedule collections reject ``$expand=principal``. Best effort:
    a failure here costs display names, never the assignments themselves.
    """
    wanted = [i for i in dict.fromkeys(ids) if i]
    if not wanted:
        return {}
    try:
        return await client.get_by_ids(wanted)
    except GraphError:
        return {}


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        truncated = False

        # --- role definitions -----------------------------------------------------
        await ctx.say("info", "Roles: collecting role definitions…")
        # NOTE: `isPrivileged` exists only on the BETA unifiedRoleDefinition, and this
        # collection does not accept `$top` AT ALL (a 400 loses the whole domain). Both were
        # confirmed against a live tenant; see backend/scripts/entra_probe_roles.py.
        defs_raw, _ = await client.get_all(
            "/roleManagement/directory/roleDefinitions",
            select=["id", "templateId", "displayName", "isBuiltIn", "isEnabled"],
            top=0,
        )
        definitions: dict[str, dict[str, Any]] = {}
        for d in defs_raw:
            d = as_dict(d)
            rid = str(d.get("id") or "")
            if not rid:
                continue
            name = d.get("displayName", "") or ""
            definitions[rid] = {
                "id": rid,
                "template_id": str(d.get("templateId") or ""),
                "display_name": name,
                "is_built_in": bool(d.get("isBuiltIn")),
                "is_enabled": bool(d.get("isEnabled", True)),
                "ms_privileged": bool(d.get("isPrivileged")),
                "tier": tier_of(name),
                "privileged": is_privileged(name, d),
            }
        await ctx.say("ok", f"Roles: {len(definitions):,} role definition(s)")

        def role_meta(role_id: str) -> dict[str, Any]:
            return definitions.get(role_id) or {
                "id": role_id, "display_name": "", "tier": "tier2", "privileged": False,
                "template_id": "", "is_built_in": False, "ms_privileged": False, "is_enabled": True,
            }

        # --- active assignments ---------------------------------------------------
        await ctx.say("info", "Roles: collecting active role assignments…")
        assigns_raw, assign_trunc = await client.get_all(
            "/roleManagement/directory/roleAssignments",
            select=["id", "roleDefinitionId", "principalId", "directoryScopeId"],
            expand="principal($select=id,displayName,userPrincipalName,userType,accountEnabled,appId)",
            top=999,
            max_items=100_000,
        )
        truncated = truncated or assign_trunc

        # --- PIM schedules (best effort; P2) --------------------------------------
        activated_ids: set[str] = set()
        schedule_meta: dict[str, dict[str, Any]] = {}
        pim_available = False
        pim_licensed = True
        try:
            scheds, _ = await client.get_all(
                "/roleManagement/directory/roleAssignmentSchedules",
                select=["id", "roleDefinitionId", "principalId", "assignmentType", "memberType", "scheduleInfo"],
                top=999,
            )
            pim_available = True
            for s in scheds:
                s = as_dict(s)
                key = f"{s.get('principalId')}|{s.get('roleDefinitionId')}"
                info = as_dict(s.get("scheduleInfo"))
                expiration = as_dict(info.get("expiration"))
                end = str(expiration.get("endDateTime") or "")
                schedule_meta[key] = {
                    "assignment_type": str(s.get("assignmentType") or ""),
                    "member_type": str(s.get("memberType") or ""),
                    "end": end,
                    "permanent": str(expiration.get("type") or "") == "noExpiration" or not end,
                }
                if str(s.get("assignmentType") or "") == "Activated":
                    activated_ids.add(key)
            await ctx.say("ok", f"Roles: {len(scheds):,} PIM assignment schedule(s)")
        except GraphPermissionError as exc:
            notes.append(_pim_note("PIM assignment schedules", exc,
                                   "RoleManagement.Read.Directory / PrivilegedAccess.Read.AzureAD"))
        except GraphError as exc:
            notes.append(_pim_note("PIM assignment schedules", exc, "PrivilegedAccess.Read.AzureAD"))
            pim_licensed = not _is_licence_error(exc)

        eligible: list[dict[str, Any]] = []
        eligible_available = False
        try:
            # NOTE: `$expand=principal(...)` is rejected on EVERY PIM schedule collection
            # (roleEligibilitySchedules, roleAssignmentSchedules and both *Instances) with a
            # 400 whose message is the actively misleading "The filter is invalid" — even
            # though no $filter was sent. `$select` alone is fine. Verified against a live
            # tenant by scripts/entra_probe_pim_shapes.py. Principals are resolved in one
            # bulk getByIds below instead.
            elig_raw, _ = await client.get_all(
                "/roleManagement/directory/roleEligibilitySchedules",
                select=["id", "roleDefinitionId", "principalId", "memberType", "scheduleInfo", "status"],
                top=999,
            )
            eligible_available = True
            principals = await _resolve_principals(
                client, [str(as_dict(e).get("principalId") or "") for e in elig_raw])
            for e in elig_raw:
                e = as_dict(e)
                principal = principals.get(str(e.get("principalId") or "")) or {}
                meta = role_meta(str(e.get("roleDefinitionId") or ""))
                info = as_dict(e.get("scheduleInfo"))
                expiration = as_dict(info.get("expiration"))
                eligible.append({
                    "id": str(e.get("id") or ""),
                    "role_id": str(e.get("roleDefinitionId") or ""),
                    "role_name": meta["display_name"],
                    "role_tier": meta["tier"],
                    "role_privileged": meta["privileged"],
                    "principal_id": str(e.get("principalId") or ""),
                    "principal_type": _principal_type(principal),
                    "principal_name": principal.get("displayName", "") or "",
                    "principal_upn": principal.get("userPrincipalName", "") or "",
                    "principal_user_type": principal.get("userType", "") or "",
                    "member_type": str(e.get("memberType") or ""),
                    "start": str(as_dict(info.get("startDateTime")) or info.get("startDateTime") or ""),
                    "end": str(expiration.get("endDateTime") or ""),
                    "permanent": str(expiration.get("type") or "") == "noExpiration"
                    or not expiration.get("endDateTime"),
                    "status": str(e.get("status") or ""),
                })
            await ctx.say("ok", f"Roles: {len(eligible):,} eligible assignment(s)")
        except GraphPermissionError as exc:
            notes.append(_pim_note("PIM eligibility schedules", exc, "PrivilegedAccess.Read.AzureAD"))
        except GraphError as exc:
            notes.append(_pim_note("PIM eligibility schedules", exc, "PrivilegedAccess.Read.AzureAD"))
            pim_licensed = pim_licensed and not _is_licence_error(exc)

        # --- normalize active assignments ------------------------------------------
        assignments: list[dict[str, Any]] = []
        group_principals: list[str] = []
        for a in assigns_raw:
            a = as_dict(a)
            principal = as_dict(a.get("principal"))
            role_id = str(a.get("roleDefinitionId") or "")
            principal_id = str(a.get("principalId") or "")
            meta = role_meta(role_id)
            key = f"{principal_id}|{role_id}"
            sched = schedule_meta.get(key)
            ptype = _principal_type(principal) or ("Group" if principal.get("groupTypes") is not None else "")
            if ptype == "Group":
                group_principals.append(principal_id)
            assignments.append({
                "id": str(a.get("id") or ""),
                "role_id": role_id,
                "role_name": meta["display_name"],
                "role_tier": meta["tier"],
                "role_privileged": meta["privileged"],
                "principal_id": principal_id,
                "principal_type": ptype,
                "principal_name": principal.get("displayName", "") or "",
                "principal_upn": principal.get("userPrincipalName", "") or "",
                "principal_user_type": principal.get("userType", "") or "",
                "principal_app_id": principal.get("appId", "") or "",
                "principal_enabled": principal.get("accountEnabled", None),
                "scope": str(a.get("directoryScopeId") or "/"),
                "assignment_kind": "active",
                "source": "direct",
                "activated": key in activated_ids,
                "permanent": bool(sched["permanent"]) if sched else None,
                "end": (sched or {}).get("end", ""),
                "permanence_known": sched is not None,
            })
        await ctx.say("ok", f"Roles: {len(assignments):,} active assignment(s)")

        # --- expand role-granting groups -------------------------------------------
        group_members: dict[str, list[str]] = {}
        if group_principals:
            await ctx.say("info", f"Roles: expanding {len(set(group_principals))} role-granting group(s)…")
            group_members, gtrunc, gnotes = await expand_groups(
                client, group_principals, cap=ctx.max_group_expansions
            )
            truncated = truncated or gtrunc
            notes.extend(gnotes)

        # Derived: every user who holds a role via a group.
        derived: list[dict[str, Any]] = []
        for a in assignments:
            if a["principal_type"] != "Group":
                continue
            for uid in group_members.get(a["principal_id"], []):
                derived.append({
                    **a,
                    "id": f"{a['id']}:{uid}",
                    "principal_id": uid,
                    "principal_type": "User",
                    "principal_name": "",
                    "source": "group",
                    "source_group_id": a["principal_id"],
                    "source_group_name": a["principal_name"],
                })

        sync_accounts = sorted({
            a["principal_id"] for a in assignments
            if (a["role_name"] or "").strip().lower() == DIRECTORY_SYNC_ROLE
        })

        # Backfill every principal we still cannot name. Group-derived rows start with an
        # empty name by construction, and a few expanded principals come back without a
        # displayName. Left alone those surface as "55ce7671-… holds a conflicting role
        # pair", which is a finding nobody can act on without a second tool.
        nameless = {
            str(row.get("principal_id") or "")
            for bucket in (assignments, derived, eligible)
            for row in bucket
            if not str(row.get("principal_name") or "").strip() and row.get("principal_id")
        }
        if nameless:
            await ctx.say("info", f"Roles: resolving {len(nameless):,} principal name(s)…")
            resolved = await _resolve_principals(client, sorted(nameless))
            for bucket in (assignments, derived, eligible):
                for row in bucket:
                    if str(row.get("principal_name") or "").strip():
                        continue
                    obj = resolved.get(str(row.get("principal_id") or "")) or {}
                    name = str(obj.get("displayName") or obj.get("userPrincipalName") or "")
                    if name:
                        row["principal_name"] = name
                        row.setdefault("principal_upn",
                                       str(obj.get("userPrincipalName") or ""))
                        ptype = _principal_type(obj)
                        if ptype:
                            row["principal_type"] = ptype
            still = sum(
                1 for bucket in (assignments, derived, eligible) for row in bucket
                if not str(row.get("principal_name") or "").strip()
            )
            if still:
                notes.append(f"{still} role assignment(s) reference a principal that no longer "
                             "exists in the directory; they are shown by object id.")

        data = {
            "definitions": list(definitions.values()),
            "assignments": assignments,
            "group_derived": derived,
            "eligible": eligible,
            "group_members": group_members,
            "sync_account_ids": sync_accounts,
            "capabilities": {
                "pim_schedules": pim_available,
                "pim_eligibility": eligible_available,
                "permanence_known": pim_available,
                "pim_licensed": pim_licensed,
            },
            "counts": {
                "definitions": len(definitions),
                "active": len(assignments),
                "group_derived": len(derived),
                "eligible": len(eligible),
                "privileged_active": sum(1 for a in assignments if a["role_privileged"]),
                "global_admins": sum(
                    1 for a in assignments
                    if (a["role_name"] or "").strip().lower() == GLOBAL_ADMIN
                ),
            },
        }
        status = model.STATUS_PARTIAL if (notes or truncated) else model.STATUS_OK
        return model.domain_payload(
            DOMAIN, data, status=status,
            item_count=len(assignments) + len(eligible), truncated=truncated, notes=notes,
        )

    return await guarded(DOMAIN, ctx, _run)


# --------------------------------------------------------------------------- helpers
def effective_role_names(roles_data: dict[str, Any], principal_id: str) -> set[str]:
    """Every role a principal effectively holds — direct, group-derived and eligible."""
    names: set[str] = set()
    for a in roles_data.get("assignments") or []:
        if a.get("principal_id") == principal_id:
            names.add((a.get("role_name") or "").strip().lower())
    for a in roles_data.get("group_derived") or []:
        if a.get("principal_id") == principal_id:
            names.add((a.get("role_name") or "").strip().lower())
    for e in roles_data.get("eligible") or []:
        if e.get("principal_id") == principal_id:
            names.add((e.get("role_name") or "").strip().lower())
    return {n for n in names if n}


def privileged_principal_ids(roles_data: dict[str, Any]) -> set[str]:
    """Every principal holding a privileged role by any path (active, group, eligible).

    This is the single definition of "privileged" used by the score, the CA coverage
    matrix and every signal — defining it twice is how two screens end up disagreeing.
    """
    out: set[str] = set()
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles_data.get(bucket) or []:
            if row.get("role_privileged") and row.get("principal_id"):
                out.add(str(row["principal_id"]))
    return out


def principal_names(roles_data: dict[str, Any]) -> dict[str, str]:
    """principal_id -> the best name Graph gave us on any role assignment row.

    Role holders are not only users: groups and service principals hold directory roles
    too, and those never appear in the people snapshot. Without this map they surfaced as
    raw GUIDs in finding titles, which is unreadable.
    """
    out: dict[str, str] = {}
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles_data.get(bucket) or []:
            pid = str(row.get("principal_id") or "")
            if not pid:
                continue
            name = str(row.get("principal_upn") or row.get("principal_name") or "").strip()
            if name and not out.get(pid):
                out[pid] = name
    return out


def global_admin_ids(roles_data: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles_data.get(bucket) or []:
            if (row.get("role_name") or "").strip().lower() == GLOBAL_ADMIN and row.get("principal_id"):
                out.add(str(row["principal_id"]))
    return out
