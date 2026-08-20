"""Group membership for Investigate — both directions, cached and live.

Membership is one edge read two ways, and the two readings have different sources and
different completeness. They are kept apart rather than merged because merging them would
force one honesty caveat to stand for both:

  cached_members()      DOWN, from cache. The IAM directory's expansion: already collected,
                        free, but TRANSITIVE, and nested groups were filtered out at
                        collection time (`iam/collectors.py` drops anything whose
                        @odata.type is a group). Answers "who ultimately gets access
                        through this group" and cannot answer "how".

  cached_memberships()  UP, from cache. A reverse index over every group whose membership
                        anyone bothered to expand — those holding Azure RBAC, those
                        granting an Entra directory role, and those a Conditional Access
                        policy names. It is a FLOOR, never a complete list, and the section
                        says so: nobody expands 11,885 groups to draw one panel.

  fetch_children()      Either direction, live, one Graph call. Keeps the @odata.type so a
                        nested group comes back as a node to open rather than being
                        flattened away. This is the only thing that can build a tree,
                        because transitive membership has no intermediate nodes in it by
                        definition — they were never fetched.

The tree is walked lazily, one level per request. A tenant here has 11,885 groups and the
largest expanded group has 562 members; eagerly walking a membership graph of that shape
would be thousands of Graph calls for a screen most people open to look at one branch.

Cycles are guarded rather than assumed away. Entra blocks most circular nesting, but it is
not a guarantee that holds across every group type and sync path, and a recursive walk that
trusts the directory to be acyclic hangs the request instead of reporting the loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Any

log = logging.getLogger("app.entra.investigate_members")

# One level is one Graph call per expanded node. These bound a single request, not the
# whole tree: the reader can keep opening branches, each costing one call.
MAX_DEPTH = 5
MAX_CHILDREN_PER_NODE = 200
MAX_NODES_PER_REQUEST = 1_000
# How many groups one request may expand. A "expand all" click on a broad group would
# otherwise fan out into hundreds of concurrent Graph calls and earn a 429 for the tenant.
MAX_EXPANSIONS_PER_REQUEST = 25
_GRAPH_FANOUT = 5

# Member kinds we render. Anything else (devices, orgContacts) is kept but labeled, rather
# than dropped — a device in a security group is unusual enough to be worth seeing.
TYPE_USER = "user"
TYPE_GROUP = "group"
TYPE_SP = "servicePrincipal"
# `memberOf` returns directory roles alongside groups. Dropping them would silently discard
# the single most privileged thing the answer can contain.
TYPE_DIRECTORY_ROLE = "directoryRole"
TYPE_ADMIN_UNIT = "administrativeUnit"


def _odata_kind(raw: str) -> str:
    """Map ``#microsoft.graph.user`` onto our own kind vocabulary."""
    tail = str(raw or "").rsplit(".", 1)[-1].strip()
    if tail == "user":
        return TYPE_USER
    if tail == "group":
        return TYPE_GROUP
    if tail == "servicePrincipal":
        return TYPE_SP
    return tail or "unknown"


# --------------------------------------------------------------------------- cached (P1)
def cached_members(tenant_id: str, group_id: str) -> tuple[list[dict[str, Any]], bool, str]:
    """Flattened membership from the IAM directory.

    Returns ``(members, known, reason)``. ``known`` is False when this group was never
    expanded — which is NOT the same as an empty group, and the caller must render the
    difference. Only groups holding an Azure RBAC assignment are expanded, so most of a
    large directory is legitimately absent here.
    """
    from app.iam import cache

    try:
        directory = cache.read_directory(tenant_id)
    except Exception as exc:  # noqa: BLE001
        return [], False, f"The Azure access cache could not be read: {exc}"

    groups = directory.get("groups") or {}
    record = groups.get(group_id)
    if not isinstance(record, dict):
        return [], False, (
            "This group's membership has not been expanded. Only groups that hold an Azure "
            "role assignment are expanded during a refresh, so this is a gap in what was "
            "collected — not a statement that the group is empty."
        )

    out: list[dict[str, Any]] = []
    for m in record.get("members") or []:
        if not isinstance(m, dict):
            continue
        out.append({
            "id": str(m.get("principalId") or ""),
            "kind": str(m.get("principalType") or "").lower() or "unknown",
            "display_name": str(m.get("principalDisplayName") or ""),
            "upn": str(m.get("principalUserPrincipalName") or ""),
        })
    out.sort(key=lambda r: (r["display_name"] or r["id"]).lower())
    return out, True, ""


# ------------------------------------------------------------- memberships (the way up)
# Why a membership is worth showing. A group that grants nothing is still a membership, but
# these three are the ones that change what a reader should do about it — and they are also
# the only groups anyone expanded, which is the same fact seen from the other side.
SOURCE_AZURE_RBAC = "grants_azure_rbac"
SOURCE_DIRECTORY_ROLE = "grants_directory_role"
SOURCE_CA_TARGET = "targeted_by_ca"

SOURCE_LABEL = {
    SOURCE_AZURE_RBAC: "holds Azure RBAC",
    SOURCE_DIRECTORY_ROLE: "grants an Entra directory role",
    SOURCE_CA_TARGET: "named by a Conditional Access policy",
}

# The caveat that makes this list safe to publish. Without it a short list reads as a
# complete one, and "this account is in no privileged group" is a conclusion nobody may
# draw from a cache that only ever expanded the groups that grant something.
COVERAGE_NOTE = (
    "Membership is only collected for groups that grant something — groups holding an "
    "Azure role assignment, groups granting an Entra directory role, and groups named by "
    "a Conditional Access policy. This is a floor, not the complete list of groups this "
    "principal belongs to. Read the directory live to see every one."
)

NOTHING_READABLE = (
    "No membership source could be read for this tenant: the Azure access cache is "
    "unavailable and no group membership was expanded during the last directory "
    "collection. This is not a statement that the principal belongs to no groups."
)

# Keyed on (tenant, snapshot stamp) so a new collection can never be answered from the old
# index. Two entries: the snapshot being read, and the one it replaced while requests in
# flight finish against it.
_INDEX_MEMO: OrderedDict[tuple[str, str], tuple[dict[str, dict[str, set[str]]],
                                                dict[str, str], set[str], str]] = OrderedDict()
_INDEX_MEMO_MAX = 2
_INDEX_LOCK = threading.Lock()


def _iam_groups(tenant_id: str) -> tuple[dict[str, Any], str]:
    from app.iam import cache

    try:
        directory = cache.read_directory(tenant_id)
    except Exception as exc:  # noqa: BLE001
        return {}, f"The Azure access cache could not be read: {exc}"
    groups = directory.get("groups")
    return (groups if isinstance(groups, dict) else {}), ""


def _build_membership_index(
    tenant_id: str, roles_data: dict[str, Any], ca_data: dict[str, Any],
) -> tuple[dict[str, dict[str, set[str]]], dict[str, str], set[str], str]:
    """Invert every membership map we hold into ``principal id -> {group id: {source}}``.

    Returns ``(index, group names, sources that contributed, reason)``. ``sources`` being
    empty is the only condition under which the answer is unreadable rather than short.
    """
    index: dict[str, dict[str, set[str]]] = {}
    names: dict[str, str] = {}
    available: set[str] = set()

    def _add(pid: str, gid: str, source: str) -> None:
        if not pid or not gid:
            return
        index.setdefault(pid, {}).setdefault(gid, set()).add(source)
        available.add(source)

    iam_groups, reason = _iam_groups(tenant_id)
    for raw_gid, record in iam_groups.items():
        if not isinstance(record, dict):
            continue
        gid = str(raw_gid)
        name = str(record.get("name") or "")
        if name:
            names.setdefault(gid, name)
        for member in record.get("members") or []:
            if isinstance(member, dict):
                _add(str(member.get("principalId") or ""), gid, SOURCE_AZURE_RBAC)

    for bucket, source in ((roles_data, SOURCE_DIRECTORY_ROLE), (ca_data, SOURCE_CA_TARGET)):
        expanded = (bucket or {}).get("group_members")
        if not isinstance(expanded, dict):
            continue
        for raw_gid, member_ids in expanded.items():
            gid = str(raw_gid)
            for pid in member_ids or []:
                _add(str(pid), gid, source)

    # A role-granting group names itself in its own assignment row; that is the only place
    # its display name exists when the group predates the people collection's cap.
    for key in ("assignments", "eligible"):
        for row in (roles_data or {}).get(key) or []:
            if not isinstance(row, dict) or str(row.get("principal_type") or "") != "Group":
                continue
            gid, name = str(row.get("principal_id") or ""), str(row.get("principal_name") or "")
            if gid and name:
                names.setdefault(gid, name)

    return index, names, available, reason


def membership_index(
    tenant_id: str, stamp: str, roles_data: dict[str, Any], ca_data: dict[str, Any],
) -> tuple[dict[str, dict[str, set[str]]], dict[str, str], set[str], str]:
    """Memoised ``_build_membership_index``.

    The build reads and parses the whole directory blob from disk. Investigate is linked
    from dozens of places and the recents strip alone re-resolves several principals, so
    rebuilding per dossier would pay that cost over and over for an answer that cannot
    change until the next collection.
    """
    key = (tenant_id, stamp)
    with _INDEX_LOCK:
        hit = _INDEX_MEMO.get(key)
        if hit is not None:
            _INDEX_MEMO.move_to_end(key)
            return hit
    built = _build_membership_index(tenant_id, roles_data, ca_data)
    with _INDEX_LOCK:
        _INDEX_MEMO[key] = built
        _INDEX_MEMO.move_to_end(key)
        while len(_INDEX_MEMO) > _INDEX_MEMO_MAX:
            _INDEX_MEMO.popitem(last=False)
    return built


def reset_membership_index() -> None:
    """Drop the memo. Tests only — a snapshot stamp already invalidates it in service."""
    with _INDEX_LOCK:
        _INDEX_MEMO.clear()


def cached_memberships(
    tenant_id: str,
    principal_id: str,
    *,
    stamp: str = "",
    roles_data: dict[str, Any] | None = None,
    ca_data: dict[str, Any] | None = None,
    people_groups: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Groups this principal belongs to, from cache alone.

    Returns ``(groups, readable, reason)``. ``readable`` is False only when no source could
    be read at all; a readable but empty answer still carries ``COVERAGE_NOTE``, because a
    floor of zero is not the same claim as a total of zero.
    """
    index, names, available, reason = membership_index(
        tenant_id, stamp, roles_data or {}, ca_data or {},
    )
    if not available:
        return [], False, reason or NOTHING_READABLE

    facts = {
        str(g.get("id") or ""): g
        for g in (people_groups or []) if isinstance(g, dict) and g.get("id")
    }
    out: list[dict[str, Any]] = []
    for gid, sources in (index.get(principal_id) or {}).items():
        rec = facts.get(gid) or {}
        out.append({
            "id": gid,
            "display_name": str(rec.get("display_name") or names.get(gid) or ""),
            "sources": sorted(sources),
            # Each of these changes how the membership should be read, and none of them is
            # visible from the membership itself.
            "dynamic": bool(rec.get("dynamic")),
            "role_assignable": bool(rec.get("is_assignable_to_role")),
            "on_prem_synced": bool(rec.get("on_prem_synced")),
            "membership_rule": str(rec.get("membership_rule") or ""),
        })
    # Role-assignable first: membership of one of those is a privilege-escalation path and
    # is the reason anyone opened this section.
    out.sort(key=lambda r: (not r["role_assignable"], -len(r["sources"]),
                            (r["display_name"] or r["id"]).lower()))
    partial = sorted({SOURCE_AZURE_RBAC, SOURCE_DIRECTORY_ROLE, SOURCE_CA_TARGET} - available)
    note = COVERAGE_NOTE
    if reason:
        note = f"{reason} {note}"
    elif partial:
        note = (f"{note} Groups that {', '.join(SOURCE_LABEL[s] for s in partial)} were not "
                "expanded in the last collection, so none of those can appear here.")
    return out, True, note


# --------------------------------------------------------------------------- live (P2)
async def _graph_get(connection: dict[str, Any], path: str, params: dict[str, str],
                     cap: int) -> tuple[list[dict[str, Any]], str]:
    """Page one Graph collection, stopping at ``cap``. Fail-soft with a stated reason."""
    from app.azure.credentials import get_graph_token

    token, terr = await get_graph_token(connection)
    if not token:
        return [], f"unavailable: {terr or 'no Graph token'}"

    import httpx

    url = f"https://graph.microsoft.com/v1.0{path}"
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict[str, Any]] = []
    query: dict[str, str] | None = params
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            for _page in range(10):
                resp = await http.get(url, headers=headers, params=query)
                query = None
                if resp.status_code in (401, 403):
                    return [], ("denied by Graph — reading membership needs "
                                "GroupMember.Read.All or Directory.Read.All.")
                if resp.status_code == 404:
                    return [], "not found in the directory."
                if resp.status_code == 400:
                    return rows, f"rejected the query ({resp.text[:120]})"
                if resp.status_code != 200:
                    return rows, f"failed ({resp.status_code})"
                body = resp.json()
                rows.extend(body.get("value") or [])
                if len(rows) >= cap:
                    return rows[:cap], ""
                url = body.get("@odata.nextLink") or ""
                if not url:
                    break
    except httpx.HTTPError as exc:
        return rows, f"error: {type(exc).__name__}"
    return rows, ""


def _node(raw: dict[str, Any]) -> dict[str, Any]:
    kind = _odata_kind(str(raw.get("@odata.type") or ""))
    return {
        "id": str(raw.get("id") or ""),
        "kind": kind,
        "display_name": str(raw.get("displayName") or ""),
        "upn": str(raw.get("userPrincipalName") or ""),
        "enabled": raw.get("accountEnabled"),
        # Only a group can be opened further. Sending this rather than letting the client
        # infer it from `kind` keeps the "is there more below" decision on one side.
        "expandable": kind == TYPE_GROUP,
    }


# Which Graph collection a principal lives in. `memberOf` is declared on the concrete types,
# not on the `directoryObject` base, so `/directoryObjects/{id}/memberOf` is rejected and a
# single kind-free path is not available. This map is a fact about Graph's schema, not a
# rendering decision — the UI still never switches on kind.
_SEGMENT_BY_KIND = {
    "user": "users",
    "guest": "users",
    "group": "groups",
    "servicePrincipal": "servicePrincipals",
    "managedIdentity": "servicePrincipals",
    "device": "devices",
    "orgContact": "contacts",
}


async def fetch_children(
    connection: dict[str, Any],
    object_id: str,
    *,
    direction: str = "down",
    kind: str = TYPE_GROUP,
    transitive: bool = False,
) -> tuple[list[dict[str, Any]], bool, str]:
    """DIRECT members of one group, or the groups one principal belongs to.

    Returns ``(children, truncated, error)``. Downward is direct — not transitive — because
    the intermediate groups ARE the tree; asking Graph for transitive members returns the
    leaves with the shape thrown away. Upward may be either: ``transitive`` answers "every
    group whose access reaches this principal", which is the question an access review asks,
    while direct answers "where was this principal actually added".
    """
    segment = _SEGMENT_BY_KIND.get(kind)
    if direction == "up":
        if not segment:
            return [], False, (
                f"a {kind or 'principal'} cannot belong to a group, so there is nothing "
                "to read above it."
            )
        nav = "transitiveMemberOf" if transitive else "memberOf"
        path = f"/{segment}/{object_id}/{nav}"
        # NO $select. memberOf returns a heterogeneous directoryObject collection, and
        # naming `userPrincipalName` or `accountEnabled` on it is rejected outright —
        # losing the whole answer to save a few hundred bytes.
        params = {"$top": "999"}
    else:
        path = f"/groups/{object_id}/members"
        params = {"$select": "id,displayName,userPrincipalName,accountEnabled", "$top": "999"}
    rows, err = await _graph_get(connection, path, params, cap=MAX_CHILDREN_PER_NODE + 1)
    if err:
        return [], False, err
    truncated = len(rows) > MAX_CHILDREN_PER_NODE
    nodes = [_node(r) for r in rows[:MAX_CHILDREN_PER_NODE]]
    nodes = [n for n in nodes if n["id"]]
    if direction == "up":
        # A directory role held through this path is the most privileged thing the answer
        # can contain, so it leads. Then groups, then anything else.
        def _rank(n: dict[str, Any]) -> tuple[int, str]:
            order = {TYPE_DIRECTORY_ROLE: 0, TYPE_GROUP: 1}.get(n["kind"], 2)
            return order, (n["display_name"] or n["id"]).lower()
        nodes.sort(key=_rank)
    else:
        # Groups first, then people: the branches are what you came to open.
        nodes.sort(key=lambda n: (n["kind"] != TYPE_GROUP, (n["display_name"] or n["id"]).lower()))
    return nodes, truncated, ""


async def expand(
    connection: dict[str, Any] | None,
    root_id: str,
    *,
    expand_ids: list[str],
    direction: str = "down",
    root_kind: str = TYPE_GROUP,
    transitive: bool = False,
) -> dict[str, Any]:
    """Expand one or more nodes by ONE level each.

    ``expand_ids`` is the set of nodes the reader has opened. The client holds the tree it
    has built so far and asks only for what it does not have, so a deep tree costs one call
    per branch opened rather than a full walk on every render.

    Only the ROOT can be something other than a group: every node the reader can open below
    it is one, in either direction.

    Returns ``{"nodes": {group_id: [child...]}, "notes": [...], "truncated": bool}``.
    """
    notes: list[str] = []
    if connection is None:
        return {"nodes": {}, "notes": ["No Azure connection is attached, so membership "
                                       "could not be read."], "truncated": False}

    wanted = [g for g in dict.fromkeys([root_id, *expand_ids]) if g]
    if len(wanted) > MAX_EXPANSIONS_PER_REQUEST:
        notes.append(
            f"Asked to open {len(wanted)} branches; {MAX_EXPANSIONS_PER_REQUEST} were read. "
            "Membership is one directory call per branch, and a wider fan-out earns the "
            "whole tenant a throttling penalty."
        )
        wanted = wanted[:MAX_EXPANSIONS_PER_REQUEST]

    sem = asyncio.Semaphore(_GRAPH_FANOUT)

    async def _one(gid: str) -> tuple[str, list[dict[str, Any]], bool, str]:
        async with sem:
            kids, trunc, err = await fetch_children(
                connection, gid, direction=direction,
                kind=root_kind if gid == root_id else TYPE_GROUP,
                # Transitivity applies to the root only. Below it the reader is walking the
                # tree by hand, and a transitive level would repeat what the next click
                # already shows while destroying the shape they opened it to see.
                transitive=transitive and gid == root_id,
            )
        return gid, kids, trunc, err

    results = await asyncio.gather(*[_one(g) for g in wanted], return_exceptions=True)

    nodes: dict[str, list[dict[str, Any]]] = {}
    truncated = False
    total = 0
    for item in results:
        if isinstance(item, BaseException):
            log.warning("investigate members: expansion failed: %s", item)
            notes.append(f"A branch could not be read: {item}")
            continue
        gid, kids, trunc, err = item
        if err:
            # An unreadable branch is recorded as unreadable, never as empty. "You may not
            # look" and "there is nobody in here" are opposite answers.
            notes.append(f"{gid}: {err}")
            nodes[gid] = []
            continue
        if trunc:
            truncated = True
            notes.append(
                f"A node has more than {MAX_CHILDREN_PER_NODE} "
                f"{'parent group(s)' if direction == 'up' else 'direct member(s)'}; the "
                f"first {MAX_CHILDREN_PER_NODE} are shown."
            )
        total += len(kids)
        if total > MAX_NODES_PER_REQUEST:
            truncated = True
            notes.append(f"Stopped at {MAX_NODES_PER_REQUEST} nodes for one request.")
            nodes[gid] = kids[: max(0, MAX_NODES_PER_REQUEST - (total - len(kids)))]
            break
        nodes[gid] = kids

    return {"nodes": nodes, "notes": notes, "truncated": truncated}


def summarize(children: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by kind for one node's children — what a collapsed branch shows."""
    out: dict[str, int] = {}
    for c in children:
        out[c.get("kind") or "unknown"] = out.get(c.get("kind") or "unknown", 0) + 1
    return out
