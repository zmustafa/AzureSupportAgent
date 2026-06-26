"""Compose the effective-access view from cached scope slices + the directory layer.

The per-scope cache stores *direct* assignments (Azure RBAC / Key Vault / classic) per scope and
*directory* rows (Entra roles, SP-owner rows) once per tenant. Reading any grid is a cheap
in-memory **compose**: union those, then expand every group assignment into one effective row
per transitive member using the directory's group graph. No Azure calls happen on read — only an
explicit per-scope refresh repopulates the cache.

``build_master_rows`` is the single source of truth; the API filters it per tab and
:mod:`app.rbac.pivots` aggregates it for the Insights tab."""
from __future__ import annotations

from typing import Any

from app.rbac import cache, schema

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
    index = _principal_index(directory, scope_rows)
    # Resolve names on the base rows BEFORE expanding groups, so a group assignment row carries
    # its display name and the expansion's sourceGroupName falls back to it.
    _apply_names(scope_rows, index)
    _apply_names(dir_rows, index)
    _apply_mg_names(scope_rows, mg_names)
    expanded = expand_group_rows(scope_rows, groups)
    _apply_names(expanded, index)
    return [*scope_rows, *dir_rows, *expanded]



def _effective_principals(rows: list[dict[str, Any]]) -> set[str]:
    return {r.get("effectivePrincipalId", "") for r in rows if r.get("effectivePrincipalId")}


def compute_overview(tenant_id: str, *, days: int = 0) -> dict[str, Any]:
    """KPIs + per-group severity + per-scope freshness for the Overview tab.

    Read-only over the cache; never triggers a scan."""
    master = build_master_rows(tenant_id)
    scopes = cache.list_scope_meta(tenant_id)
    directory_meta = cache.read_directory_meta(tenant_id)

    privileged = [r for r in master if r.get("roleIsPrivileged")]
    data_plane = [r for r in master if r.get("roleHasDataActions")]
    group_derived = [r for r in master if r.get("accessPath") == schema.PATH_GROUP]
    owners = [r for r in master if r.get("accessPath") == schema.PATH_OWNER]
    entra = [r for r in master if r.get("surface") == schema.SURFACE_ENTRA]
    eligible = [r for r in master if r.get("assignmentState") == schema.STATE_ELIGIBLE]

    kpis = {
        "total_assignments": len(master),
        "unique_principals": len(_effective_principals(master)),
        "privileged": len(privileged),
        "data_plane": len(data_plane),
        "group_derived": len(group_derived),
        "owners": len(owners),
        "entra_roles": len(entra),
        "eligible": len(eligible),
        "scopes": len(scopes),
        "subscriptions": len({r.get("subscriptionId") for r in master if r.get("subscriptionId")}),
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
    collectors = meta.get("collectors", []) or []
    needs = [c for c in collectors if c.get("status") in schema.ATTENTION_STATUSES]
    return {
        "scope": meta.get("scope", ""),
        "scopeType": meta.get("scopeType", ""),
        "displayName": meta.get("displayName", meta.get("scope", "")),
        "subscriptionId": meta.get("subscriptionId", ""),
        "status": meta.get("status", schema.STATUS_SUCCEEDED),
        "row_count": meta.get("row_count", 0),
        "generated_at": gen,
        "age_seconds": cache.age_seconds(gen),
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
