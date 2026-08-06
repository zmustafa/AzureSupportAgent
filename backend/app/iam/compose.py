"""Compose the effective-access view from cached scope slices + the directory layer.

The per-scope cache stores *direct* assignments (Azure RBAC / Key Vault / classic) per scope and
*directory* rows (Entra roles, SP-owner rows) once per tenant. Reading any grid is a cheap
in-memory **compose**: union those, then expand every group assignment into one effective row
per transitive member using the directory's group graph. No Azure calls happen on read — only an
explicit per-scope refresh repopulates the cache.

``build_master_rows`` is the single source of truth; the API filters it per tab and
:mod:`app.iam.pivots` aggregates it for the Insights tab."""
from __future__ import annotations

import threading
from typing import Any

from app.iam import cache, schema

# KPI / group keys the Overview renders.
GROUPS = (
    "privileged",
    "data_plane",
    "group_derived",
    "owners",
    "entra_roles",
    "eligible",
)


def _has_member(grp: dict[str, Any] | None, member_id: str) -> bool:
    return any(
        str(m.get("principalId", "")).lower() == member_id
        for m in ((grp or {}).get("members") or [])
    )


def membership_group(
    groups: dict[str, Any], source_gid: str, member_id: str
) -> tuple[str, str, str]:
    """Which group the membership is ACTUALLY in -> (group id, group name, resolution).

    A role assignment held by group G reaches everyone in G's nesting tree, but a membership
    only ever exists in ONE group. `az ad group member remove --group G` deletes a DIRECT
    membership and nothing else, so aimed at G it fails outright — "Resource 'G' does not exist
    or one of its queried reference-property objects are not present" — when the person is
    really a member of a child. Reported from a real run.

    `nested` is the TRANSITIVE set of descendant groups, so the group the member is directly in
    is the deepest candidate: the one that contains no other candidate.

    Resolution is one of `direct` (the member is in G itself), `nested`, `ambiguous` (the member
    sits directly in more than one child, so no single removal is enough) or `unknown` (part of
    the nesting could not be expanded, so any answer would be a guess)."""
    member_id = (member_id or "").lower()
    src = groups.get(source_gid) or {}
    nested = [n for n in (src.get("nested") or []) if n]
    candidates = [n for n in nested if _has_member(groups.get(n), member_id)]
    # A group we never managed to expand could hold the membership without us knowing.
    incomplete = any(n not in groups for n in nested)

    if not candidates:
        if incomplete:
            return "", "", "unknown"
        # Nothing nested holds them, so the membership is in the assignment group itself.
        return source_gid, str(src.get("name") or ""), "direct"

    cset = set(candidates)
    deepest = [h for h in candidates if not (cset & set((groups.get(h) or {}).get("nested") or []))]
    if len(deepest) != 1:
        names = "; ".join(sorted(str((groups.get(h) or {}).get("name") or h) for h in deepest)) or ""
        return "", names, "ambiguous"
    if incomplete:
        return "", "", "unknown"
    only = deepest[0]
    return only, str((groups.get(only) or {}).get("name") or only), "nested"


def expand_group_rows(scope_rows: list[dict[str, Any]], groups: dict[str, Any]) -> list[dict[str, Any]]:
    """For each row assigned to a Group that the directory graph knows, emit one effective row
    per transitive member (accessPath=GroupTransitive), carrying the member as the effective
    principal and the group as the source. The original group row is kept by the caller."""
    out: list[dict[str, Any]] = []
    for row in scope_rows:
        if row.get("principalType") != "Group":
            continue
        gid = row.get("principalId", "")
        grp = groups.get(gid)
        if not grp:
            continue
        gname = grp.get("name", row.get("principalDisplayName", ""))
        for member in grp.get("members", []) or []:
            eff = dict(row)
            eff["accessPath"] = schema.PATH_GROUP
            eff["assignmentType"] = "GroupMembership"
            eff["sourceGroupId"] = gid
            eff["sourceGroupName"] = gname
            eff["effectivePrincipalId"] = member.get("principalId", "")
            eff["effectivePrincipalType"] = member.get("principalType", "")
            eff["effectivePrincipalName"] = member.get("principalDisplayName", "")
            eff["effectivePrincipalUserPrincipalName"] = member.get("principalUserPrincipalName", "")
            mid, mname, how = membership_group(groups, gid, member.get("principalId", ""))
            eff["membershipGroupId"] = mid
            eff["membershipGroupName"] = mname
            eff["membershipGroupResolution"] = how
            # Read from the GROUP, not from the principal directory: a nested child holds no
            # assignment, so it is not among the principals that get resolved, and every one of
            # these would read "unknown" for exactly the groups the removal now targets.
            mgrp = groups.get(mid) or {}
            eff["membershipGroupRoleAssignable"] = str(mgrp.get("roleAssignable") or schema.ENABLED_UNKNOWN)
            eff["membershipGroupDynamic"] = str(mgrp.get("dynamic") or schema.ENABLED_UNKNOWN)
            if mgrp.get("onPremSynced"):
                eff["membershipGroupOnPremSynced"] = str(mgrp["onPremSynced"])
            # The chain is what a reader needs to understand the row, and until now it was just
            # the assignment group repeated — a "chain" of one that hid the nesting entirely.
            eff["groupChain"] = f"{gname} > {mname}" if how == "nested" and mname else gname
            out.append(eff)
    return out


def _principal_index(
    directory: dict[str, Any], scope_rows: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Build a GUID → {name, upn, type} map from every name we know: the resolved principal
    directory, the group-expansion members, the directory rows, and any scope row that already
    carries a name. Used to backfill the GUID-only Azure-RBAC assignments so every tab and the
    exports show friendly names. Lower-cased GUID keys; first non-empty name wins."""
    index: dict[str, dict[str, str]] = {}

    def _add(pid: str, name: str, upn: str, ptype: str) -> None:
        key = (pid or "").lower()
        if not key:
            return
        entry = index.setdefault(key, {"name": "", "upn": "", "type": ""})
        if name and not entry["name"]:
            entry["name"] = name
        if upn and not entry["upn"]:
            entry["upn"] = upn
        if ptype and not entry["type"]:
            entry["type"] = ptype

    # 1. The resolved principal directory (Graph getByIds / demo principal dir).
    for p in directory.get("principals", []) or []:
        _add(
            p.get("principalId", ""),
            p.get("displayName", ""),
            p.get("userPrincipalName", "") or p.get("appId", ""),
            p.get("principalType", ""),
        )
    # 2. Group-expansion members (each carries its own name).
    for grp in (directory.get("groups", {}) or {}).values():
        for m in grp.get("members", []) or []:
            _add(
                m.get("principalId", ""),
                m.get("principalDisplayName", ""),
                m.get("principalUserPrincipalName", ""),
                m.get("principalType", ""),
            )
    # 3. Any row (directory or scope) that already resolved a name — Entra/owner rows do.
    for r in [*directory.get("rows", []), *scope_rows]:
        _add(r.get("principalId", ""), r.get("principalDisplayName", ""), r.get("principalUserPrincipalName", ""), r.get("principalType", ""))
        _add(r.get("effectivePrincipalId", ""), r.get("effectivePrincipalName", ""), r.get("effectivePrincipalUserPrincipalName", ""), r.get("effectivePrincipalType", ""))
        sid = r.get("sourceGroupId", "")
        if sid:
            _add(sid, r.get("sourceGroupName", ""), "", "Group")
    return index


def _apply_names(rows: list[dict[str, Any]], index: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Backfill empty principal/effective-principal names (and types) on each row from the
    index. Mutates copies; rows that already have a name are left untouched."""
    if not index:
        return rows
    for r in rows:
        pid = str(r.get("principalId", "")).lower()
        if pid and not r.get("principalDisplayName"):
            ent = index.get(pid)
            if ent:
                if ent["name"]:
                    r["principalDisplayName"] = ent["name"]
                if ent["upn"] and not r.get("principalUserPrincipalName"):
                    r["principalUserPrincipalName"] = ent["upn"]
                if ent["type"] and not r.get("principalType"):
                    r["principalType"] = ent["type"]
        eid = str(r.get("effectivePrincipalId", "")).lower()
        if eid and not r.get("effectivePrincipalName"):
            ent = index.get(eid)
            if ent:
                if ent["name"]:
                    r["effectivePrincipalName"] = ent["name"]
                if ent["upn"] and not r.get("effectivePrincipalUserPrincipalName"):
                    r["effectivePrincipalUserPrincipalName"] = ent["upn"]
                if ent["type"] and not r.get("effectivePrincipalType"):
                    r["effectivePrincipalType"] = ent["type"]
    return rows


def _apply_mg_names(rows: list[dict[str, Any]], mg_names: dict[str, str]) -> list[dict[str, Any]]:
    """Backfill the management-group display name onto MG-scoped rows from the resolved id→name
    map, and replace a management-group ``scopeDisplayName`` that's still the raw ARM scope path
    (or the GUID) with the friendly name. Leaves rows with no MG, or an already-named MG, alone."""
    if not mg_names:
        return rows
    for r in rows:
        mg = str(r.get("managementGroupId", "")).lower()
        if not mg:
            continue
        name = mg_names.get(mg)
        if not name or name.lower() == mg:
            continue
        if not r.get("managementGroupName") or str(r.get("managementGroupName", "")).lower() == mg:
            r["managementGroupName"] = name
        # For MG-scoped assignments the collector sets scopeDisplayName to the scope path; swap it
        # for the name so the grid's Scope column reads naturally.
        if r.get("scopeType") == schema.SCOPE_MANAGEMENT_GROUP:
            sdn = str(r.get("scopeDisplayName", ""))
            if not sdn or sdn == r.get("scope") or sdn.lower() == mg:
                r["scopeDisplayName"] = name
    return rows


def dedupe_assignments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same assignment seen from several scopes into one authoritative row.

    ARM returns an assignment made at a management group from EVERY child subscription's
    ``roleAssignments`` query as an inherited row. Once management groups are collected in their
    own right (see ``orchestrator.refresh_all``), one grant made at an MG covering 26
    subscriptions would otherwise appear 26 times, each attributed to a different subscription —
    inflating every count and making the scope-tree totals meaningless.

    The winner is the copy collected AT the assignment's own scope (``row["scope"]`` equals the
    scope slice it came from, i.e. ``isInherited`` is false). Rows without an ``assignmentId``
    (group-expanded, ownership, classic admins) are never deduped — they are distinct facts that
    legitimately share no id."""
    best: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    order: list[str] = []
    for row in rows:
        aid = str(row.get("assignmentId", "")).strip().lower()
        # Group-derived rows share their parent's assignmentId but represent a DIFFERENT
        # principal's access, so they must key on the effective principal too.
        if not aid:
            passthrough.append(row)
            continue
        key = f"{aid}|{str(row.get('effectivePrincipalId', '')).lower()}|{row.get('accessPath', '')}"
        prev = best.get(key)
        if prev is None:
            best[key] = row
            order.append(key)
        elif prev.get("isInherited") and not row.get("isInherited"):
            # Prefer the copy collected at the assignment's own scope.
            best[key] = row
    return [*(best[k] for k in order), *passthrough]


def _apply_principal_existence(
    rows: list[dict[str, Any]], index: dict[str, dict[str, str]], directory_readable: bool
) -> list[dict[str, Any]]:
    """Mark each row's principal as existing, orphaned, or unknown.

    When a principal is deleted, ARM **keeps the role assignment** — the portal renders it as
    "Identity not found" with a bare GUID. Those rows inflate every count and, because object ids
    can be re-created against a recycled application, can silently hand access to a new object.

    The evidence is already here: anything the directory resolver could not name is a candidate.
    But that is only an orphan **if the directory read succeeded** — otherwise we simply could not
    look, and calling it deleted would fabricate a finding out of a permissions gap. Rows whose
    principal has no id at all (classic administrators are keyed by e-mail) stay ``unknown``.

    Existence is judged on a RESOLVED NAME, never on mere presence in the index. The index is
    built partly from the assignment rows themselves (`_principal_index` step 3), so every
    principal id — including a deleted one — has a key in it, keyed there by its own orphaned
    assignment. Testing `pid in index` therefore returned True for literally every row: measured
    on a real tenant, all 5,506 rows were classified ``true`` and orphan detection could
    not fire at all. Graph confirmed 26 of those principals return 404 Request_ResourceNotFound
    — 100 live role assignments, including Contributor, held by identities that no longer exist.
    A nameless index entry is the absence of evidence, not evidence of existence."""
    for r in rows:
        pid = str(r.get("effectivePrincipalId") or r.get("principalId") or "").strip()
        if not pid:
            r["principalExists"] = schema.EXISTS_UNKNOWN
            continue
        entry = index.get(pid.lower())
        resolved = bool(entry and entry.get("name"))
        if resolved or r.get("effectivePrincipalName") or r.get("principalDisplayName"):
            r["principalExists"] = schema.EXISTS_TRUE
        elif directory_readable:
            r["principalExists"] = schema.EXISTS_FALSE
        else:
            r["principalExists"] = schema.EXISTS_UNKNOWN
    return rows


def _apply_principal_state(
    rows: list[dict[str, Any]], principal_state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stamp each row with whether its principal's Entra account is enabled or disabled.

    Keyed on the **effective** principal, falling back to the assignee. That distinction is the
    whole point: when a group holds the assignment, ``principalId`` is the group (which has no
    account state at all) and ``effectivePrincipalId`` is the human member who actually receives
    the access. Keying on the assignee would report every group-derived grant as uncheckable and
    hide the single most overlooked case — a disabled leaver still sitting in a group that grants
    production access, which nobody looks at because the group itself is perfectly healthy.

    Rows whose principal is absent from the map stay ``unknown``. That is not a formality: a
    cache collected before this column existed has no map, and defaulting to "enabled" there
    would silently report every leaver in the estate as a current employee."""
    for r in rows:
        pid = str(r.get("effectivePrincipalId") or r.get("principalId") or "").strip().lower()
        ptype = str(r.get("effectivePrincipalType") or r.get("principalType") or "")
        # Stamped BEFORE the early exits below, because it answers a different question about a
        # different object: not "is this person synced" but "can this group's membership be
        # edited in Entra at all". Keyed on the group the REMOVAL will target — the membership
        # group — falling back to the assignment group when the nesting could not be resolved.
        # Left "" when there is no group at all so the column never claims to have checked
        # something that does not exist.
        gid = str(r.get("membershipGroupId") or r.get("sourceGroupId") or "").strip().lower()
        if gid and not r.get("membershipGroupOnPremSynced"):
            g_entry = principal_state.get(gid)
            r["membershipGroupOnPremSynced"] = str(
                (g_entry or {}).get("onPremSynced") or schema.ENABLED_UNKNOWN
                if isinstance(g_entry, dict)
                else schema.ENABLED_UNKNOWN
            )
        entry = principal_state.get(pid) if pid else None
        if isinstance(entry, dict):
            r["principalAccountEnabled"] = str(entry.get("accountEnabled") or schema.ENABLED_UNKNOWN)
            r["principalOnPremSynced"] = str(entry.get("onPremSynced") or schema.ENABLED_UNKNOWN)
            r["principalUserType"] = str(entry.get("userType") or r.get("principalUserType") or "")
            continue
        # No entry. Normalise the row anyway so the grid never renders a ragged column, and
        # distinguish "there is no account to check" (a group, a classic admin keyed by e-mail)
        # from "we did not manage to check".
        #
        # The test here is against UNKNOWN specifically, not truthiness: `make_row` already
        # defaults this column to the string "unknown", which is truthy, so a truthiness guard
        # skipped every single row and no group was ever marked notApplicable.
        current = str(r.get("principalAccountEnabled") or schema.ENABLED_UNKNOWN)
        if current != schema.ENABLED_UNKNOWN:
            continue
        r["principalAccountEnabled"] = (
            schema.ENABLED_UNKNOWN
            if (not ptype or ptype in schema.ACCOUNT_BEARING_TYPES)
            else schema.ENABLED_NA
        )
        if not r.get("principalOnPremSynced"):
            r["principalOnPremSynced"] = schema.ENABLED_UNKNOWN
        if not r.get("principalUserType"):
            r["principalUserType"] = ""
    return rows


def principal_state_measured(tenant_id: str) -> bool:
    """Did any refresh actually collect account state for this tenant?

    The gate for every disabled-access number in the product. An empty result with this False
    means "we have not looked"; an empty result with it True means "we looked and everyone who
    holds access is enabled". Those are opposite findings and the UI must be able to tell them
    apart — the same rule that stopped ``standing_ratio`` reporting 100% standing privilege on
    tenants where PIM had simply never been collected."""
    return bool(cache.read_directory(tenant_id).get("principal_state"))


def build_master_rows(tenant_id: str) -> list[dict[str, Any]]:
    """The full normalized row set for a tenant: direct scope rows + directory rows + the
    group-derived effective rows expanded from the directory group graph, with GUID-only
    principals backfilled to friendly names from the resolved principal directory, and
    management-group scopes shown by name rather than GUID.

    RP1 — memoised in-process keyed by the cache files' mtimes. This function is called by
    /access (incl. every search keystroke), /pivots, /diagnostics, /overview, /scope-tree and the
    exports, and each call otherwise re-reads + gunzips every scope sidecar from disk. The memo
    means repeated reads between refreshes are O(1); any cache write (which bumps the index/blob
    mtimes) transparently invalidates it.

    **Single-flight, and safe to call from worker threads.** Every caller now runs this off the
    event loop, so several can arrive at once — and they reliably do: finishing a refresh
    invalidates seven react-query keys simultaneously, each landing on a memo the final write
    just discarded. Without the lock that is seven identical full recomposes racing, and two
    threads could also store their results out of order, leaving an OLDER row set under a NEWER
    signature — a memo that is not merely stale but wrong. The lock makes the losers wait for
    the winner's result instead of duplicating it."""
    sig = _cache_signature(tenant_id)
    hit = _MASTER_CACHE.get(tenant_id)
    if hit is not None and hit[0] == sig:
        return hit[1]
    with _master_lock(tenant_id):
        # Re-check inside the lock: while waiting, the thread that held it has very likely just
        # built exactly what this caller wanted.
        hit = _MASTER_CACHE.get(tenant_id)
        sig = _cache_signature(tenant_id)
        if hit is not None and hit[0] == sig:
            return hit[1]
        rows = _build_master_rows_uncached(tenant_id)
        _MASTER_CACHE[tenant_id] = (sig, rows)
        return rows


# RP1 — in-process memo: tenant -> (cache-version, rows). Bounded to the active tenants in a
# process; entries are replaced (not accumulated) as the cache version advances.
_MASTER_CACHE: dict[str, tuple[int, list[dict[str, Any]]]] = {}

# One rebuild lock per tenant. Per-tenant rather than global so a slow recompose for one tenant
# does not serialise reads for another — this process serves several connections at once.
_MASTER_LOCKS: dict[str, threading.Lock] = {}
_MASTER_LOCKS_GUARD = threading.Lock()


def _master_lock(tenant_id: str) -> threading.Lock:
    lock = _MASTER_LOCKS.get(tenant_id)
    if lock is None:
        with _MASTER_LOCKS_GUARD:
            lock = _MASTER_LOCKS.setdefault(tenant_id, threading.Lock())
    return lock


def _cache_signature(tenant_id: str) -> int:
    """Freshness signature for a tenant's RBAC cache: the global write sequence, bumped on any
    scope/directory/index write. Robust to filesystem mtime granularity."""
    return cache.cache_version()


def _build_master_rows_uncached(tenant_id: str) -> list[dict[str, Any]]:
    scope_rows = cache.all_scope_rows(tenant_id)
    directory = cache.read_directory(tenant_id)
    dir_rows = directory.get("rows", [])
    groups = directory.get("groups", {})
    mg_names = directory.get("management_groups", {})
    # Dedupe BEFORE group expansion, or every duplicate of a group assignment multiplies into a
    # duplicate per transitive member.
    scope_rows = dedupe_assignments(scope_rows)
    index = _principal_index(directory, scope_rows)
    # Resolve names on the base rows BEFORE expanding groups, so a group assignment row carries
    # its display name and the expansion's sourceGroupName falls back to it.
    _apply_names(scope_rows, index)
    _apply_names(dir_rows, index)
    _apply_mg_names(scope_rows, mg_names)
    expanded = expand_group_rows(scope_rows, groups)
    _apply_names(expanded, index)
    # Orphan classification runs LAST, over every row, and only claims "deleted" when the
    # directory was actually readable.
    dir_meta = cache.read_directory_meta(tenant_id)
    directory_readable = bool(index) and dir_meta.get("status") not in (
        schema.STATUS_FAILED, schema.STATUS_UNAUTHORIZED, schema.STATUS_THROTTLED, schema.STATUS_SKIPPED,
    )
    all_rows = [*scope_rows, *dir_rows, *expanded]
    _apply_principal_existence(all_rows, index, directory_readable)
    # Account state runs over every row, INCLUDING the group-expanded ones, so it must come
    # after the expansion — the member is only a row's effective principal once it exists.
    _apply_principal_state(all_rows, directory.get("principal_state", {}) or {})
    return all_rows



def _effective_principals(rows: list[dict[str, Any]]) -> set[str]:
    return {r.get("effectivePrincipalId", "") for r in rows if r.get("effectivePrincipalId")}


# Collectors whose presence proves eligibility was actually looked for on a scope.
_PIM_COLLECTORS = frozenset({"AzurePimEligibility", "PimDirectoryAssignments"})


def _pim_was_collected(scopes: list[dict[str, Any]]) -> bool:
    """Did any cached scope actually run a PIM collector?

    A scope collected before the PIM collectors existed, or by a connection that got a 403 on
    the schedule APIs, contains no eligible rows — indistinguishable from a tenant that simply
    does not use PIM unless the collector status is consulted. ``Skipped`` counts: an unlicensed
    tenant genuinely has no eligibility, and we did look."""
    for meta in scopes:
        for c in meta.get("collectors", []) or []:
            if c.get("collector") in _PIM_COLLECTORS and c.get("status") not in (
                schema.STATUS_FAILED,
                schema.STATUS_UNAUTHORIZED,
                schema.STATUS_THROTTLED,
            ):
                return True
    return False


def compute_overview(tenant_id: str, *, days: int = 0) -> dict[str, Any]:
    """KPIs + per-group severity + per-scope freshness for the Overview tab.

    Read-only over the cache; never triggers a scan."""
    master = build_master_rows(tenant_id)
    scopes = cache.list_scope_meta(tenant_id)
    directory_meta = cache.read_directory_meta(tenant_id)

    # Deny assignments REMOVE access, so they must not be counted as grants — folding them into
    # "Total grants" would inflate the headline number with rows that mean the opposite.
    denies = [r for r in master if r.get("effect") == schema.EFFECT_DENY]
    grants = [r for r in master if r.get("effect") != schema.EFFECT_DENY]

    privileged = [r for r in grants if r.get("roleIsPrivileged")]
    data_plane = [r for r in grants if r.get("roleHasDataActions")]
    group_derived = [r for r in grants if r.get("accessPath") == schema.PATH_GROUP]
    owners = [r for r in grants if r.get("accessPath") == schema.PATH_OWNER]
    entra = [r for r in grants if r.get("surface") == schema.SURFACE_ENTRA]
    eligible = [r for r in grants if r.get("assignmentState") == schema.STATE_ELIGIBLE]
    kv_policies = [r for r in grants if r.get("surface") == schema.SURFACE_KEY_VAULT]
    classic = [r for r in grants if r.get("surface") == schema.SURFACE_CLASSIC]

    # The headline PIM numbers. "Privileged" alone cannot distinguish an Owner someone holds
    # permanently from one they must request and that expires — which is the whole point of PIM.
    standing_privileged = [r for r in grants if schema.is_standing_privilege(r)]
    eligible_privileged = [r for r in grants if r.get("roleIsPrivileged") and r.get("assignmentState") == schema.STATE_ELIGIBLE]
    activated = [r for r in grants if r.get("activationExpiresOn")]
    governed = len(standing_privileged) + len(eligible_privileged)

    # BLIND IS NOT ZERO. A cache collected before the PIM collectors existed — or by a
    # connection that could not read the PIM schedules — has no eligible rows at all, which
    # computes to "100% of privileged access is permanent". That reads as a damning finding when
    # the truth is that nobody looked. Only report a ratio when PIM was actually collected.
    pim_collected = _pim_was_collected(scopes)

    # Disabled principals that still hold access. Same gate, same reason: a tenant whose cache
    # predates the account-state collector has an empty disabled set, and "0 disabled principals
    # hold access" is a reassuring headline produced by never having asked.
    state_measured = bool((directory_meta or {}).get("principal_state_count")) or bool(
        cache.read_directory(tenant_id).get("principal_state")
    )
    disabled_rows = [r for r in grants if schema.is_disabled(r)]
    disabled_privileged = [r for r in disabled_rows if r.get("roleIsPrivileged")]
    disabled_principals = {
        str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower()
        for r in disabled_rows
    } - {""}

    kpis = {
        "total_assignments": len(grants),
        "unique_principals": len(_effective_principals(grants)),
        "privileged": len(privileged),
        "data_plane": len(data_plane),
        "group_derived": len(group_derived),
        "owners": len(owners),
        "entra_roles": len(entra),
        "eligible": len(eligible),
        "deny_assignments": len(denies),
        "key_vault_policies": len(kv_policies),
        "classic_admins": len(classic),
        "standing_privileged": len(standing_privileged),
        "eligible_privileged": len(eligible_privileged),
        "active_elevations": len(activated),
        # Whether the PIM collectors ran at all for the cached scopes. The UI needs this to tell
        # "no eligible access exists" apart from "eligibility was never collected".
        "pim_collected": pim_collected,
        # Whether account state was collected at all. Every disabled_* number below is
        # meaningless without it, so it travels with them rather than being inferred.
        "account_state_collected": state_measured,
        "disabled_principals": len(disabled_principals) if state_measured else None,
        "disabled_assignments": len(disabled_rows) if state_measured else None,
        "disabled_privileged": len(disabled_privileged) if state_measured else None,
        # Share of privileged access that is PERMANENT. None when PIM was not collected, or when
        # there is no privileged access at all — a 0% or 100% figure derived from an unmeasured
        # surface is worse than no figure.
        "standing_ratio": round(len(standing_privileged) / governed, 3) if (governed and pim_collected) else None,
        "scopes": len(scopes),
        "subscriptions": len({r.get("subscriptionId") for r in grants if r.get("subscriptionId")}),
    }

    # Per-group severity: privileged/owners are the loud ones.
    group_severity = {
        "privileged": "error" if privileged else "ok",
        "data_plane": "warning" if data_plane else "ok",
        "group_derived": "warning" if group_derived else "ok",
        "owners": "warning" if owners else "ok",
        "entra_roles": "warning" if any(r.get("roleIsPrivileged") for r in entra) else "ok",
        "eligible": "info" if eligible else "ok",
    }

    return {
        "tenant_id": tenant_id,
        "generated_at": _latest_generated(scopes, directory_meta),
        "kpis": kpis,
        "group_severity": group_severity,
        "scopes": [_scope_freshness(s) for s in scopes],
        "directory": _directory_freshness(directory_meta),
        "collectors": _all_collectors(scopes, directory_meta),
        "demo": cache.is_demo(tenant_id),
        "never_loaded": not cache.has_any(tenant_id),
    }


def _latest_generated(scopes: list[dict[str, Any]], directory_meta: dict[str, Any]) -> str:
    stamps = [str(s.get("generated_at", "")) for s in scopes]
    if directory_meta.get("generated_at"):
        stamps.append(str(directory_meta["generated_at"]))
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else ""


def _scope_freshness(meta: dict[str, Any]) -> dict[str, Any]:
    gen = str(meta.get("generated_at", ""))
    verified = str(meta.get("verified_at", ""))
    collectors = meta.get("collectors", []) or []
    needs = [c for c in collectors if c.get("status") in schema.ATTENTION_STATUSES]
    return {
        "scope": meta.get("scope", ""),
        "scopeType": meta.get("scopeType", ""),
        "displayName": meta.get("displayName", meta.get("scope", "")),
        "subscriptionId": meta.get("subscriptionId", ""),
        "status": meta.get("status", schema.STATUS_SUCCEEDED),
        "row_count": meta.get("row_count", 0),
        # Always the real COLLECTION time. A delta refresh that skipped this scope records
        # `verified_at` separately rather than moving this, so "4 days old, verified 2 minutes
        # ago" stays tellable from "collected 2 minutes ago".
        "generated_at": gen,
        "age_seconds": cache.age_seconds(gen),
        "verified_at": verified,
        "verified_age_seconds": cache.age_seconds(verified) if verified else None,
        "verified_unchanged": bool(meta.get("verified_unchanged")),
        # "arg" (tenant-wide Resource Graph sweep) or "arm" (per-scope calls). The two have
        # different blind spots, so a reader comparing scopes needs to know which ran.
        "source": str(meta.get("source", "")),
        "collectors_total": len(collectors),
        "collectors_attention": len(needs),
        "demo": bool(meta.get("demo")),
    }


def _directory_freshness(meta: dict[str, Any]) -> dict[str, Any]:
    gen = str(meta.get("generated_at", ""))
    return {
        "status": meta.get("status", "") or ("" if not meta else schema.STATUS_SUCCEEDED),
        "generated_at": gen,
        "age_seconds": cache.age_seconds(gen),
        "row_count": meta.get("row_count", 0),
        "role_def_count": meta.get("role_def_count", 0),
        "principal_count": meta.get("principal_count", 0),
        "group_count": meta.get("group_count", 0),
        "loaded": bool(meta),
    }


def _all_collectors(scopes: list[dict[str, Any]], directory_meta: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in scopes:
        label = s.get("displayName", s.get("scope", ""))
        for c in s.get("collectors", []) or []:
            out.append({**c, "scope": s.get("scope", ""), "scopeLabel": label})
    for c in directory_meta.get("collectors", []) or []:
        out.append({**c, "scope": cache.DIRECTORY_KEY, "scopeLabel": "Directory"})
    return out
