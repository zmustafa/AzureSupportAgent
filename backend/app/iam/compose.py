"""Compose the effective-access view from cached scope slices + the directory layer.

The per-scope cache stores *direct* assignments (Azure RBAC / Key Vault / classic) per scope and
*directory* rows (Entra roles, SP-owner rows) once per tenant. Reading any grid is a cheap
in-memory **compose**: union those, then expand every group assignment into one effective row
per transitive member using the directory's group graph. No Azure calls happen on read — only an
explicit per-scope refresh repopulates the cache.

``build_master_rows`` is the single source of truth; the API filters it per tab and
:mod:`app.iam.pivots` aggregates it for the Insights tab."""
from __future__ import annotations

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
            eff["groupChain"] = gname
            eff["sourceGroupId"] = gid
            eff["sourceGroupName"] = gname
            eff["effectivePrincipalId"] = member.get("principalId", "")
            eff["effectivePrincipalType"] = member.get("principalType", "")
            eff["effectivePrincipalName"] = member.get("principalDisplayName", "")
            eff["effectivePrincipalUserPrincipalName"] = member.get("principalUserPrincipalName", "")
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
    on the live `lu` tenant, all 5,506 rows were classified ``true`` and orphan detection could
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


def build_master_rows(tenant_id: str) -> list[dict[str, Any]]:
    """The full normalized row set for a tenant: direct scope rows + directory rows + the
    group-derived effective rows expanded from the directory group graph, with GUID-only
    principals backfilled to friendly names from the resolved principal directory, and
    management-group scopes shown by name rather than GUID.

    RP1 — memoised in-process keyed by the cache files' mtimes. This function is called by
    /access (incl. every search keystroke), /pivots, /diagnostics, /overview, /scope-tree and the
    exports, and each call otherwise re-reads + gunzips every scope sidecar from disk. The memo
    means repeated reads between refreshes are O(1); any cache write (which bumps the index/blob
    mtimes) transparently invalidates it."""
    sig = _cache_signature(tenant_id)
    hit = _MASTER_CACHE.get(tenant_id)
    if hit is not None and hit[0] == sig:
        return hit[1]
    rows = _build_master_rows_uncached(tenant_id)
    _MASTER_CACHE[tenant_id] = (sig, rows)
    return rows


# RP1 — in-process memo: tenant -> (cache-version, rows). Bounded to the active tenants in a
# process; entries are replaced (not accumulated) as the cache version advances.
_MASTER_CACHE: dict[str, tuple[int, list[dict[str, Any]]]] = {}


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
