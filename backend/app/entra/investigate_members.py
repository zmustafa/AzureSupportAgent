"""Group membership for Investigate — the flat answer from cache, and the nested tree.

Two sources, deliberately kept apart because they answer different questions and disagree
for honest reasons:

  cached_members()   the IAM directory's expansion. Already collected, free, but it is
                     TRANSITIVE and nested groups were filtered out at collection time
                     (`iam/collectors.py` drops anything whose @odata.type is a group).
                     So it answers "who ultimately gets access through this group" and
                     cannot answer "how".

  fetch_children()   one live Graph call for the DIRECT members of one group, keeping the
                     @odata.type so a nested group comes back as a node to open rather
                     than being flattened away. This is the only thing that can build a
                     tree, because transitive membership has no intermediate nodes in it
                     by definition — they were never fetched.

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

# Member kinds we render. Anything else (devices, orgContacts) is kept but labelled, rather
# than dropped — a device in a security group is unusual enough to be worth seeing.
TYPE_USER = "user"
TYPE_GROUP = "group"
TYPE_SP = "servicePrincipal"


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


async def fetch_children(
    connection: dict[str, Any], group_id: str, *, direction: str = "down",
) -> tuple[list[dict[str, Any]], bool, str]:
    """DIRECT members of one group (or, upward, the groups it belongs to).

    Returns ``(children, truncated, error)``. Direct — not transitive — because the
    intermediate groups ARE the tree; asking Graph for transitive members returns the
    leaves with the shape thrown away.
    """
    if direction == "up":
        path = f"/groups/{group_id}/memberOf"
    else:
        path = f"/groups/{group_id}/members"
    rows, err = await _graph_get(
        connection, path,
        {"$select": "id,displayName,userPrincipalName,accountEnabled", "$top": "999"},
        cap=MAX_CHILDREN_PER_NODE + 1,
    )
    if err:
        return [], False, err
    truncated = len(rows) > MAX_CHILDREN_PER_NODE
    nodes = [_node(r) for r in rows[:MAX_CHILDREN_PER_NODE]]
    nodes = [n for n in nodes if n["id"]]
    # Groups first, then people: the branches are what you came to open.
    nodes.sort(key=lambda n: (n["kind"] != TYPE_GROUP, (n["display_name"] or n["id"]).lower()))
    return nodes, truncated, ""


async def expand(
    connection: dict[str, Any] | None,
    root_id: str,
    *,
    expand_ids: list[str],
    direction: str = "down",
) -> dict[str, Any]:
    """Expand one or more group nodes by ONE level each.

    ``expand_ids`` is the set of nodes the reader has opened. The client holds the tree it
    has built so far and asks only for what it does not have, so a deep tree costs one call
    per branch opened rather than a full walk on every render.

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
            kids, trunc, err = await fetch_children(connection, gid, direction=direction)
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
                f"A group has more than {MAX_CHILDREN_PER_NODE} direct members; the first "
                f"{MAX_CHILDREN_PER_NODE} are shown."
            )
        total += len(kids)
        if total > MAX_NODES_PER_REQUEST:
            truncated = True
            notes.append(f"Stopped at {MAX_NODES_PER_REQUEST} nodes for one request.")
            nodes[gid] = kids[: max(0, MAX_NODES_PER_REQUEST - (total - len(kids)))]
            break
        nodes[gid] = kids

    return {"nodes": nodes, "notes": notes, "truncated": truncated}


def summarise(children: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by kind for one node's children — what a collapsed branch shows."""
    out: dict[str, int] = {}
    for c in children:
        out[c.get("kind") or "unknown"] = out.get(c.get("kind") or "unknown", 0) + 1
    return out
