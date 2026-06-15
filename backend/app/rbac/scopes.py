"""Scope-tree construction + scope/workload filtering for the RBAC access review.

Two things power the "filter by Azure scope or workload" controls on the RBAC screen:

1. :func:`build_scope_tree` turns the cached access rows into a Tenant Root → management
   groups → subscriptions tree (with a grant count per node), built purely from the cache so
   it never triggers an Azure call. Subscriptions are nested under a management group when the
   data links them; when the data carries exactly one management group (the common single-MG
   tenant, and the demo dataset) the orphan subscriptions are nested under it as an *inferred*
   parent so the tree reads naturally.

2. :func:`filter_rows` applies a selected scope and/or workload to the master row set. Scope
   matching is hierarchical (selecting a subscription includes its resource-group/resource
   rows; selecting a management group includes its child subscriptions). Workload matching
   reuses the assessment scope resolver so a workload's subscriptions / resource groups /
   resources — plus the subscription-level grants that inherit down to them — are included."""
from __future__ import annotations

from typing import Any

from app.rbac import cache, compose, schema

# A management-group ARM scope prefix (lower-cased) used to detect MG-scoped rows.
_MG_PREFIX = "/providers/microsoft.management/managementgroups/"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def mg_scope_id(mg_id: str) -> str:
    return f"/providers/Microsoft.Management/managementGroups/{mg_id}"


def sub_scope_id(guid: str) -> str:
    return f"/subscriptions/{guid}"


# --------------------------------------------------------------------------- scope tree
def build_scope_tree(tenant_id: str) -> dict[str, Any]:
    """Build the Tenant Root → MG → subscription tree from the cached rows + scope metas.

    Returns ``{"root": <node>, "demo": bool, "subscription_count": int, "mg_count": int}``.
    Each node is ``{id, name, type, count, subscriptionIds, inferred?, children}`` where
    ``count`` is the number of grants at or below the node and ``subscriptionIds`` lists every
    subscription GUID the node covers (so the client can drive a hierarchical filter)."""
    metas = cache.list_scope_meta(tenant_id)
    rows = compose.build_master_rows(tenant_id)
    # Resolved management-group id → display name map (populated by the directory refresh). Used
    # to show MG names instead of GUIDs; a row/meta name only wins when it's a real (non-GUID) name.
    mg_names = {str(k).lower(): v for k, v in (cache.read_directory(tenant_id).get("management_groups", {}) or {}).items()}

    def _best_mg_name(mg: str, *candidates: str) -> str:
        for c in candidates:
            if c and str(c).lower() != mg:
                return str(c)
        return mg_names.get(mg) or mg

    # Discover subscriptions (guid -> display name) from scanned scope metas first (best names),
    # then from any row that carries a subscription id.
    subs: dict[str, str] = {}
    for m in metas:
        if m.get("scopeType") == schema.SCOPE_SUBSCRIPTION:
            guid = str(m.get("subscriptionId", "")).lower()
            if guid:
                subs.setdefault(guid, m.get("displayName") or guid)
    for r in rows:
        guid = str(r.get("subscriptionId", "")).lower()
        if guid and guid not in subs:
            subs[guid] = r.get("subscriptionName") or guid

    # Discover management groups (id -> display name) from scope metas + rows + the resolved map.
    mgs: dict[str, str] = {}
    for m in metas:
        if m.get("scopeType") == schema.SCOPE_MANAGEMENT_GROUP:
            mg = str(m.get("managementGroupId", "")).lower()
            if mg:
                mgs[mg] = _best_mg_name(mg, m.get("displayName", ""))
    for r in rows:
        mg = str(r.get("managementGroupId", "")).lower()
        if mg and mg not in mgs:
            mgs[mg] = _best_mg_name(mg, r.get("managementGroupName", ""))

    # Per-subscription grant counts (every row that lands in the subscription, at any depth).
    sub_count: dict[str, int] = {g: 0 for g in subs}
    # Rows scoped directly at a management group (inherited assignments at the MG level).
    mg_direct: dict[str, int] = {m: 0 for m in mgs}
    for r in rows:
        guid = str(r.get("subscriptionId", "")).lower()
        if guid:
            sub_count[guid] = sub_count.get(guid, 0) + 1
        scp = _norm(r.get("scope"))
        if scp.startswith(_MG_PREFIX):
            mg = scp.rsplit("/", 1)[-1]
            if mg in mg_direct:
                mg_direct[mg] = mg_direct.get(mg, 0) + 1

    # Link subscriptions to management groups. Prefer an explicit link carried on a row; when
    # the data doesn't link them but there's exactly one MG, nest every subscription under it
    # (single-MG tenants + the demo dataset) and flag the link as inferred.
    sub_to_mg: dict[str, str] = {}
    for r in rows:
        guid = str(r.get("subscriptionId", "")).lower()
        mg = str(r.get("managementGroupId", "")).lower()
        if guid and mg and mg in mgs:
            sub_to_mg.setdefault(guid, mg)
    inferred = False
    if len(mgs) == 1:
        only_mg = next(iter(mgs))
        for guid in subs:
            if guid not in sub_to_mg:
                sub_to_mg[guid] = only_mg
                inferred = True

    def _sub_node(guid: str) -> dict[str, Any]:
        return {
            "id": sub_scope_id(guid),
            "name": subs[guid],
            "type": "subscription",
            "count": sub_count.get(guid, 0),
            "subscriptionIds": [guid],
            "children": [],
        }

    mg_nodes: list[dict[str, Any]] = []
    for mg in sorted(mgs, key=lambda m: mgs[m].lower()):
        child_guids = sorted((g for g, parent in sub_to_mg.items() if parent == mg), key=lambda g: subs[g].lower())
        children = [_sub_node(g) for g in child_guids]
        covered = list(child_guids)
        total = mg_direct.get(mg, 0) + sum(sub_count.get(g, 0) for g in child_guids)
        node = {
            "id": mg_scope_id(mg),
            "name": mgs[mg],
            "type": "managementGroup",
            "count": total,
            "subscriptionIds": covered,
            "children": children,
        }
        if inferred:
            node["inferred"] = True
        mg_nodes.append(node)

    orphan_guids = sorted((g for g in subs if g not in sub_to_mg), key=lambda g: subs[g].lower())
    orphan_nodes = [_sub_node(g) for g in orphan_guids]

    root = {
        "id": "",
        "name": "All scopes",
        "type": "root",
        "count": len(rows),
        "subscriptionIds": sorted(subs),
        "children": [*mg_nodes, *orphan_nodes],
    }
    return {
        "root": root,
        "demo": cache.is_demo(tenant_id),
        "subscription_count": len(subs),
        "mg_count": len(mgs),
    }


# --------------------------------------------------------------------------- scope filter
def _row_in_scope(row: dict[str, Any], scope_id: str, sub_ids: set[str]) -> bool:
    """A row is within the selection when its subscription is one of ``sub_ids`` OR its ARM
    scope is at/below ``scope_id`` (prefix containment)."""
    sub = str(row.get("subscriptionId", "")).lower()
    if sub and sub in sub_ids:
        return True
    if scope_id:
        scp = _norm(row.get("scope"))
        if scp == scope_id or scp.startswith(scope_id + "/"):
            return True
    return False


# --------------------------------------------------------------------------- workload filter
async def resolve_workload_filter(workload_id: str, connection: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve a workload to the scope set used for matching rows. Reuses the assessment scope
    resolver (so management-group nodes expand to their subscriptions). Returns ``None`` when
    the workload can't be found."""
    from app.assessments.runner import _resolve_scope
    from app.workloads.registry import get_workload

    wl = get_workload(workload_id)
    if not wl:
        return None
    scope = await _resolve_scope(wl, connection)
    return {
        "direct_subs": {str(s).lower() for s in scope.get("subscriptions", [])},
        "effective_subs": {str(s).lower() for s in scope.get("effective_subscriptions", [])},
        "rg_pairs": {(str(g).lower(), str(rg).lower()) for g, rg in scope.get("rg_pairs", [])},
        "resource_ids": [str(r).lower().rstrip("/") for r in scope.get("resource_ids", [])],
        "name": wl.get("name", ""),
    }


def _row_in_workload(row: dict[str, Any], f: dict[str, Any]) -> bool:
    """Whether an access row falls within a workload's footprint.

    Includes: any row in a wholly-selected subscription; resource-group / resource rows within
    the workload's groups/resources; and the subscription-level (inherited) grants that govern
    those subscriptions — since a subscription Owner effectively has access to the workload."""
    sub = str(row.get("subscriptionId", "")).lower()
    rg = str(row.get("resourceGroup", "")).lower()
    scp = _norm(row.get("scope"))
    scope_type = str(row.get("scopeType", "")).lower()

    if sub and sub in f["direct_subs"]:
        return True
    if sub and rg and (sub, rg) in f["rg_pairs"]:
        return True
    for guid, rgn in f["rg_pairs"]:
        pref = f"/subscriptions/{guid}/resourcegroups/{rgn}"
        if scp == pref or scp.startswith(pref + "/"):
            return True
    for rid in f["resource_ids"]:
        if scp == rid or scp.startswith(rid + "/"):
            return True
    # Subscription-level grants inherit down to every resource in the subscription, so they are
    # effective access to a resource-group/resource-scoped workload too.
    if sub and sub in f["effective_subs"] and scope_type == schema.SCOPE_SUBSCRIPTION.lower():
        return True
    return False


# --------------------------------------------------------------------------- combined
async def filter_rows(
    rows: list[dict[str, Any]],
    *,
    scope_id: str = "",
    subscription_ids: list[str] | None = None,
    workload_id: str = "",
    connection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply the scope and/or workload selection to ``rows`` (AND semantics when both set)."""
    sub_ids = {str(s).lower() for s in (subscription_ids or []) if s}
    sid = _norm(scope_id)
    scope_active = bool(sid) or bool(sub_ids)

    wl_filter: dict[str, Any] | None = None
    if workload_id:
        wl_filter = await resolve_workload_filter(workload_id, connection)

    if not scope_active and wl_filter is None:
        return rows

    out: list[dict[str, Any]] = []
    for r in rows:
        if scope_active and not _row_in_scope(r, sid, sub_ids):
            continue
        if wl_filter is not None and not _row_in_workload(r, wl_filter):
            continue
        out.append(r)
    return out
