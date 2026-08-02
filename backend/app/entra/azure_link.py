"""The Azure bridge — join the Entra snapshot with the existing RBAC cache.

This is the product's differentiator: a principal that holds **both** Entra directory power
and Azure control-plane power is a concentration no Microsoft surface shows in one place.

No new Azure collection happens here. We read the cache that ``backend/app/rbac`` already
maintains, so the join is free — but it can therefore be *older* than the Entra snapshot,
and a stale join presented as current would be worse than no join at all. Every consumer
gets ``stale`` and ``generated_at`` alongside the data.
"""
from __future__ import annotations

import logging
from typing import Any

from app.entra import cache as entra_cache

log = logging.getLogger("app.entra.azure_link")

# Azure roles that confer control-plane power worth correlating with directory power.
POWERFUL_AZURE_ROLES = {
    "owner", "contributor", "user access administrator", "role based access control administrator",
    "security admin", "key vault administrator", "storage blob data owner",
}

# Tenant-wide scopes: privilege here is materially different from privilege on one resource.
BROAD_SCOPE_TYPES = {"tenantRoot", "managementGroup", "subscription"}

_memo: dict[str, tuple[Any, dict[str, Any]]] = {}


def empty(reason: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "generated_at": "",
        "stale": False,
        "age_seconds": None,
        "principals": {},
        "counts": {"rows": 0, "principals": 0, "powerful": 0},
    }


def build(tenant_id: str, *, entra_generated_at: str = "") -> dict[str, Any]:
    """Index the RBAC cache by principal id. Never raises."""
    try:
        from app.iam import cache as rbac_cache
        from app.iam import compose, schema
    except Exception as exc:  # noqa: BLE001 - the RBAC module is optional to this feature
        return empty(f"RBAC module unavailable: {exc}")

    try:
        signature = rbac_cache.cache_version()
    except Exception:  # noqa: BLE001
        signature = None
    key = tenant_id or "default"
    memo = _memo.get(key)
    if memo and signature is not None and memo[0] == signature:
        return _with_freshness(dict(memo[1]), entra_generated_at)

    try:
        rows = compose.build_master_rows(tenant_id)
    except Exception:  # noqa: BLE001 - a cold or corrupt RBAC cache must not break Entra
        log.warning("entra azure-link: could not read the RBAC cache", exc_info=True)
        return empty("The Azure RBAC cache could not be read.")

    if not rows:
        result = empty("No Azure RBAC scan has been run for this tenant yet.")
        _memo[key] = (signature, result)
        return result

    principals: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
        if not pid:
            continue
        surface = str(row.get("surface") or "")
        if surface == schema.SURFACE_ENTRA:
            continue          # directory roles come from the Entra side; do not double-count
        role = str(row.get("roleName") or "")
        scope_type = str(row.get("scopeType") or "")
        entry = principals.setdefault(pid, {
            "principal_id": pid,
            "name": str(row.get("effectivePrincipalName") or row.get("principalDisplayName") or ""),
            "upn": str(row.get("principalUserPrincipalName") or ""),
            "type": str(row.get("effectivePrincipalType") or row.get("principalType") or ""),
            "roles": [],
            "powerful_roles": [],
            "broad_scopes": [],
            "subscriptions": set(),
            "privileged": False,
        })
        entry["roles"].append({
            "role": role,
            "scope_type": scope_type,
            "scope_name": str(row.get("scopeDisplayName") or row.get("scope") or ""),
            "subscription": str(row.get("subscriptionName") or row.get("subscriptionId") or ""),
            "state": str(row.get("assignmentState") or ""),
            "path": str(row.get("accessPath") or ""),
        })
        if role.strip().lower() in POWERFUL_AZURE_ROLES:
            entry["powerful_roles"].append(role)
            if scope_type in BROAD_SCOPE_TYPES:
                entry["broad_scopes"].append(f"{role} @ {scope_type}")
        if row.get("roleIsPrivileged"):
            entry["privileged"] = True
        if row.get("subscriptionId"):
            entry["subscriptions"].add(str(row["subscriptionId"]))

    for entry in principals.values():
        entry["subscriptions"] = sorted(entry["subscriptions"])
        entry["powerful_roles"] = sorted(set(entry["powerful_roles"]))
        entry["broad_scopes"] = sorted(set(entry["broad_scopes"]))
        entry["role_count"] = len(entry["roles"])
        entry["roles"] = entry["roles"][:50]

    generated_at = ""
    try:
        generated_at = str(compose.compute_overview(tenant_id).get("generated_at") or "")
    except Exception:  # noqa: BLE001 - freshness is best-effort
        generated_at = ""

    result = {
        "available": True,
        "reason": "",
        "generated_at": generated_at,
        "stale": False,
        "age_seconds": None,
        "principals": principals,
        "counts": {
            "rows": len(rows),
            "principals": len(principals),
            "powerful": sum(1 for p in principals.values() if p["powerful_roles"]),
        },
    }
    _memo[key] = (signature, result)
    return _with_freshness(result, entra_generated_at)


def _with_freshness(link: dict[str, Any], entra_generated_at: str) -> dict[str, Any]:
    """Mark the join stale when the RBAC cache predates the Entra snapshot."""
    age = entra_cache.age_seconds(link.get("generated_at", ""))
    link["age_seconds"] = int(age) if age is not None else None
    if link.get("generated_at") and entra_generated_at:
        link["stale"] = link["generated_at"] < entra_generated_at
    else:
        link["stale"] = bool(link.get("available")) and not link.get("generated_at")
    return link


def azure_power(link: dict[str, Any], principal_id: str) -> dict[str, Any] | None:
    """Azure-side power for one principal, or None when it has none."""
    if not link.get("available"):
        return None
    return (link.get("principals") or {}).get(principal_id)


def invalidate() -> None:
    _memo.clear()
