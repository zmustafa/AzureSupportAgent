"""Azure-plane privilege escalation: *"Alice is not an Owner. Can Alice become one?"*

Almost always yes, and almost never through a role called Owner.

A **primitive** is a rule of the form *(the principal effectively holds action A) → (capability
C)*. Each detected instance becomes a directed edge, and a path through those edges is an
escalation route. This is the Azure counterpart to :mod:`app.entra.blastradius`, and it inherits
that module's hard-won graph rules verbatim, because each was a production defect:

1. **Never emit an edge whose endpoints are absent.** Cytoscape rejects the *entire batch* when
   one edge points at a missing node, which blanks the canvas — a single bad edge loses the whole
   view. :func:`_finish` filters and counts them.
2. **Cap fan-out at ``MAX_FAN_OUT`` per (source, primitive)**, keeping the true total. One
   service principal produced 224 arrows in the Entra version; the 225th adds no information and
   costs the legibility that is the entire point of the view.
3. **The higher-confidence edge wins**, and the loser is kept in ``also_via`` rather than
   discarded. A medium-confidence path once masked a high-confidence one to the same target and
   the operator read the weaker explanation.
4. **Publish ``limitations``.** An escalation map that cannot see policy identities must say so.
   Silence reads as "there are none", which is the opposite of the truth.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Iterable

from app.iam import effective

log = logging.getLogger("app.iam.escalation")

MAX_FAN_OUT = 12
MAX_NODES = 1500

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
_CONFIDENCE_RANK = {CONF_HIGH: 2, CONF_MEDIUM: 1, CONF_LOW: 0}

EDGE_ESCALATES_TO = "escalates_to"
EDGE_HOLDS = "holds"
EDGE_ATTACHED_TO = "attached_to"


# --------------------------------------------------------------------------- primitive registry
# Each entry declares the ACTIONS that confer the capability. Detection runs through the P4
# effective-permission engine rather than matching role names, so a custom role that happens to
# grant `Microsoft.Authorization/roleAssignments/write` is caught exactly like Owner is — which
# is the entire reason the engine had to exist first.
PRIMITIVES: list[dict[str, Any]] = [
    {
        "key": "role_write",
        "name": "Can grant itself any role",
        "actions": ("Microsoft.Authorization/roleAssignments/write",),
        "confidence": CONF_HIGH,
        "target": "tier0",
        "rule": "Holds roleAssignments/write, so it can assign itself Owner at this scope.",
    },
    {
        "key": "deny_write",
        "name": "Can remove deny assignments",
        "actions": ("Microsoft.Authorization/denyAssignments/write",),
        "confidence": CONF_HIGH,
        "target": "tier0",
        "rule": "Can delete the deny assignment that was containing it.",
    },
    {
        "key": "keyvault_pivot",
        "name": "Key Vault control plane to data plane",
        "actions": ("Microsoft.KeyVault/vaults/write", "Microsoft.KeyVault/vaults/accessPolicies/write"),
        "confidence": CONF_HIGH,
        "target": "scope",
        "rule": (
            "Can add itself a Key Vault access policy and then read every secret. The classic "
            "privilege-boundary break: control-plane rights become data-plane rights."
        ),
    },
    {
        "key": "storage_key",
        "name": "Can list storage account keys",
        "actions": ("Microsoft.Storage/storageAccounts/listKeys/action",),
        "confidence": CONF_HIGH,
        "target": "scope",
        "rule": "A shared key grants full data-plane access regardless of RBAC.",
    },
    {
        "key": "aks_admin",
        "name": "Can obtain AKS cluster-admin credentials",
        "actions": ("Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action",),
        "confidence": CONF_HIGH,
        "target": "scope",
        "rule": "Cluster-admin regardless of the cluster's Azure RBAC settings.",
    },
    {
        "key": "federated_cred",
        "name": "Can add a federated credential",
        "actions": ("Microsoft.ManagedIdentity/userAssignedIdentities/write",),
        "confidence": CONF_HIGH,
        "target": "identity",
        "rule": (
            "Can add a federated identity credential to a managed identity and mint tokens from "
            "any OIDC issuer — no secret, no expiry, no unusual sign-in."
        ),
    },
    {
        "key": "lighthouse",
        "name": "Can onboard the subscription to another tenant",
        "actions": ("Microsoft.ManagedServices/registrationAssignments/write",),
        "confidence": CONF_HIGH,
        "target": "tier0",
        "rule": "Can delegate this scope to an external tenant via Azure Lighthouse.",
    },
    {
        "key": "lock_delete",
        "name": "Can remove resource locks",
        "actions": ("Microsoft.Authorization/locks/delete",),
        "confidence": CONF_MEDIUM,
        "target": "scope",
        "rule": "Can remove the lock that was preventing deletion.",
    },
    {
        "key": "identity_hijack_vm",
        "name": "Can run code on a VM as its managed identity",
        "actions": ("Microsoft.Compute/virtualMachines/runCommand/action",),
        "confidence": CONF_HIGH,
        "target": "identity_on_resource",
        "rule": "Can execute code on the VM and inherit whatever its managed identity holds.",
    },
    {
        "key": "identity_hijack_web",
        "name": "Can deploy code to an app as its managed identity",
        "actions": ("Microsoft.Web/sites/config/write", "Microsoft.Web/sites/publishxml/action"),
        "confidence": CONF_HIGH,
        "target": "identity_on_resource",
        "rule": "Can change the app's configuration or deploy to it, running as its identity.",
    },
    {
        "key": "identity_hijack_aci",
        "name": "Can create a container with an existing identity",
        "actions": ("Microsoft.ContainerInstance/containerGroups/write",),
        "confidence": CONF_MEDIUM,
        "target": "identity_on_resource",
        "rule": "Can start a container bound to a user-assigned identity and inherit its roles.",
    },
    {
        "key": "automation_runas",
        "name": "Can run code as an Automation account identity",
        "actions": ("Microsoft.Automation/automationAccounts/runbooks/write",
                    "Microsoft.Automation/automationAccounts/jobs/write"),
        "confidence": CONF_MEDIUM,
        "target": "identity_on_resource",
        "rule": "Can author and run a runbook as the automation account's identity.",
    },
    {
        "key": "deployment_as",
        "name": "Can deploy as a policy remediation identity",
        "actions": ("Microsoft.Resources/deployments/write",),
        "confidence": CONF_LOW,
        "target": "scope",
        "rule": (
            "Can trigger a deployment that runs as a deployIfNotExists / modify policy identity, "
            "which frequently holds Contributor or higher."
        ),
    },
]

PRIMITIVE_BY_KEY = {p["key"]: p for p in PRIMITIVES}

# The capability every escalation is measured against.
TIER0_LABEL = "Owner / full control"
TIER0_NODE = "tier0::owner"


# --------------------------------------------------------------------------- node/edge helpers
def principal_node(pid: str) -> str:
    return f"principal::{pid}".lower()


def scope_node(scope: str) -> str:
    return f"scope::{scope}".lower()


def identity_node(pid: str) -> str:
    return f"identity::{pid}".lower()


def _node(nid: str, kind: str, label: str, **data: Any) -> dict[str, Any]:
    return {"id": nid, "kind": kind, "label": label, **data}


def _edge(source: str, target: str, kind: str, **data: Any) -> dict[str, Any]:
    return {
        "id": f"{source}__{kind}__{target}",
        "source": source,
        "target": target,
        "kind": kind,
        "data": dict(data),
    }


def _finish(
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    *,
    limitations: list[str],
    fan_out_total: dict[str, int],
) -> dict[str, Any]:
    """Deduplicate, cap, and drop every edge whose endpoints are not present.

    The dangling-edge filter is not defensive programming. Cytoscape rejects the whole batch when
    one edge points at a node that is not in the payload, which blanks the canvas — so a single
    stray edge costs the entire view. This is the most important function in the module."""
    by_id: dict[str, dict[str, Any]] = {}
    truncated = False
    for node in nodes:
        by_id.setdefault(node["id"], node)
    if len(by_id) > MAX_NODES:
        truncated = True
        by_id = dict(list(by_id.items())[:MAX_NODES])
    present = set(by_id)

    kept: dict[str, dict[str, Any]] = {}
    dropped = 0
    for edge in edges:
        if edge["source"] not in present or edge["target"] not in present:
            dropped += 1
            continue
        if edge["source"] == edge["target"]:
            dropped += 1
            continue
        kept.setdefault(edge["id"], edge)

    return {
        "nodes": list(by_id.values()),
        "edges": list(kept.values()),
        "dropped_edges": dropped,
        "fan_out_total": fan_out_total,
        "truncated": truncated,
        "limitations": limitations,
        "stats": {"node_count": len(by_id), "edge_count": len(kept)},
    }


# --------------------------------------------------------------------------- detection
def _distinct_scopes(rows: list[dict[str, Any]]) -> list[str]:
    """Scopes worth evaluating at: every distinct assignment scope.

    Evaluating at every resource in the estate would be intractable and pointless — an escalation
    primitive is held *because of an assignment*, so the assignment scopes are exactly the places
    a capability can first appear."""
    seen: dict[str, None] = {}
    for r in rows:
        s = str(r.get("scope", "")).strip()
        if s:
            seen.setdefault(s, None)
    return list(seen)


def filter_by_confidence(graph: dict[str, Any], min_confidence: str) -> dict[str, Any]:
    """Narrow an already-built graph to a confidence floor. Pure, and cheap.

    ``min_confidence`` only ever *excludes primitives* from detection, so a graph built at
    ``low`` is a strict superset of one built at ``medium`` or ``high``. Verified against
    natively-built graphs on a 5,506-grant tenant: **the edge set and the path set come out
    identical at every level**, which is what makes deriving them safe rather than approximate.

    That matters because each level used to cost its own full build — 31s at low, 30s at medium,
    19s at high — so simply moving the confidence selector re-ran the entire engine, and after
    any refresh all three were cold again.

    The one difference from a native build is isolated nodes: a native run never emits a
    principal or scope that no surviving primitive touched, whereas filtering leaves it behind.
    Those are dropped here, which makes this a strict subset of the native node set (never a
    superset — an edge whose endpoint is missing blanks the whole Cytoscape canvas). A node with
    no edges at the chosen confidence carries no information anyway, and the view hides them by
    default."""
    rank = _CONFIDENCE_RANK.get(min_confidence, 0)
    if rank <= 0:
        return graph

    edges = [
        e for e in graph.get("edges") or []
        if _CONFIDENCE_RANK.get((e.get("data") or {}).get("confidence"), 0) >= rank
    ]
    endpoints = {e["source"] for e in edges} | {e["target"] for e in edges}
    nodes = [n for n in graph.get("nodes") or [] if n["id"] in endpoints]
    paths = [
        p for p in graph.get("paths") or []
        if _CONFIDENCE_RANK.get(p.get("min_confidence"), 0) >= rank
    ]
    return {
        **graph,
        "nodes": nodes,
        "edges": edges,
        "paths": paths,
        "min_confidence": min_confidence,
        "stats": {**(graph.get("stats") or {}), "node_count": len(nodes), "edge_count": len(edges)},
    }


def graph_for_tenant(
    tenant_id: str,
    rows: list[dict[str, Any]],
    role_index: dict[str, effective.RoleActionSet],
    *,
    identities: dict[str, dict[str, Any]] | None = None,
    federated: list[dict[str, Any]] | None = None,
    min_confidence: str = CONF_LOW,
    force: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """The unfiltered escalation graph for a tenant's current snapshot, memoised and PERSISTED.

    :func:`detect` runs the effective-permission engine across (principals x primitives x
    scopes). Measured on a realistic 5,506-grant / 45-scope tenant: **30 seconds**, producing
    1,198 nodes and 7,052 edges — and `/iam/escalation`, `/iam/findings` and `/iam/score` all
    want the SAME graph on every page load.

    Two levels, because an in-process memo alone was not enough:

    1. a bounded LRU in this process — the fast path;
    2. a gzipped copy in the tenant cache, stamped with the ``cache_version`` it was built from.

    The disk layer exists because the memo used to hold exactly ONE entry and was cleared before
    every write, so a user with two connections paid the full 30 seconds *on every switch, in
    both directions*, and every API restart threw the work away. Persisted, the graph survives
    both — and on a tenant twice this size, where the build is over a minute, that is the
    difference between a usable screen and an unusable one. It costs ~430 KB gzipped.

    Freshness is the cache version, never a TTL: the rows ARE the cache at that version, so any
    write invalidates it exactly. ``force=True`` rebuilds and is what the refresh button uses.
    Callers holding rows that did not come from the tenant cache must call :func:`detect`.

    **Always built at the lowest confidence and narrowed afterwards.** The floor only excludes
    primitives from detection, so the low graph contains every higher one; caching per level
    instead meant the confidence selector cost a full 20-30 second rebuild per position, and a
    refresh left two of the three cold. See :func:`filter_by_confidence`."""
    from app.iam import cache

    version = cache.cache_version()
    key = (tenant_id, cache.cache_fingerprint(), CONF_LOW)

    if not force:
        hit = _GRAPH_CACHE.get(key)
        if hit is not None:
            _GRAPH_CACHE.move_to_end(key)
            return filter_by_confidence(hit, min_confidence)
        stored = cache.read_escalation(tenant_id)
        if stored.get("cache_version") == version and stored.get("min_confidence") == CONF_LOW:
            graph = stored.get("graph") or {}
            if graph:
                _remember(key, graph)
                return filter_by_confidence(graph, min_confidence)

    started = time.monotonic()
    graph = detect(
        rows, role_index, identities=identities, federated=federated,
        min_confidence=CONF_LOW,
    )
    duration = time.monotonic() - started
    _remember(key, graph)
    try:
        cache.write_escalation(
            tenant_id, graph,
            cache_version=version, min_confidence=CONF_LOW, duration_seconds=duration,
        )
    except Exception:  # noqa: BLE001 - a cache write must never fail the request
        log.warning("iam escalation: could not persist the graph", exc_info=True)
    return filter_by_confidence(graph, min_confidence)


# Escalation-graph memo: (tenant, cache version, confidence floor) -> graph.
#
# Bounded but NOT single-entry. The previous version cleared the whole dict before every write,
# which made it useless the moment a user had more than one connection: A -> B -> A re-paid the
# full 30-second build each way. Several entries are cheap (the payload is already in memory for
# whichever tenant is active) and remove the thrash entirely.
MAX_MEMO_ENTRIES = 6
_GRAPH_CACHE: "OrderedDict[tuple[str, tuple[str, int], str], dict[str, Any]]" = OrderedDict()


def _remember(key: tuple[str, tuple[str, int], str], graph: dict[str, Any]) -> None:
    _GRAPH_CACHE[key] = graph
    _GRAPH_CACHE.move_to_end(key)
    while len(_GRAPH_CACHE) > MAX_MEMO_ENTRIES:
        _GRAPH_CACHE.popitem(last=False)


def build_duration(tenant_id: str) -> float | None:
    """How long this tenant's graph took to build last time, or None if never measured.

    Used to tell somebody waiting how long the wait is likely to be. Returns None rather than a
    default so the caller can say "no estimate yet" instead of inventing one."""
    from app.iam import cache

    value = cache.read_escalation_meta(tenant_id).get("duration_seconds")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def detect(
    rows: list[dict[str, Any]],
    role_index: dict[str, effective.RoleActionSet],
    *,
    identities: dict[str, dict[str, Any]] | None = None,
    federated: list[dict[str, Any]] | None = None,
    min_confidence: str = CONF_LOW,
    principal_id: str = "",
    scope_filter: str = "",
) -> dict[str, Any]:
    """Build the escalation graph from composed access rows.

    ``identities`` maps a principal id to its managed-identity facts, which is what turns
    "somebody can run code on this VM" into "somebody can become whatever that VM's identity
    is" — the most common real finding in every tenant.

    Pure and uncached. Use :func:`graph_for_tenant` for the unfiltered graph over a tenant's
    cached snapshot, which is expensive enough to be worth memoising."""
    identities = identities or {}
    federated = federated or []
    min_rank = _CONFIDENCE_RANK.get(min_confidence, 0)

    limitations: list[str] = []
    if not identities:
        limitations.append(
            "Managed identities were not collected, so identity-hijack paths (running code on a "
            "resource to inherit its identity's roles) are NOT shown. This is usually the most "
            "common escalation path in a tenant — absence here does not mean absence in Azure."
        )
    elif not federated:
        # Federated credentials only exist on USER-ASSIGNED identities. If the inventory ran and
        # found none of those, "no federated credentials" is a complete answer rather than a
        # blind spot — and calling it a limitation would train the reader to ignore the list,
        # which is the one thing that must not happen to it.
        has_uami = any(i.get("identityKind") == "UserAssigned" for i in identities.values())
        if has_uami:
            limitations.append(
                "Federated identity credentials were not collected, so external OIDC trust paths "
                "are not shown."
            )
    limitations.append(
        "Policy remediation (deployIfNotExists / modify) identities are not inventoried, so "
        "deployment-as-policy-identity paths are inferred from the deployment right alone."
    )

    nodes: list[dict[str, Any]] = [
        _node(TIER0_NODE, "capability", TIER0_LABEL, tier=0)
    ]
    edges: list[dict[str, Any]] = []
    seen_edge: dict[str, int] = {}
    fan_out: dict[tuple[str, str], int] = {}
    fan_out_total: dict[str, int] = {}

    def add_edge(source: str, target: str, key: str, why: str, evidence: dict[str, Any]) -> None:
        """One edge per (source, target). Stronger confidence wins; fan-out is capped."""
        if not source or not target or source == target:
            return
        prim = PRIMITIVE_BY_KEY[key]
        rank = _CONFIDENCE_RANK.get(prim["confidence"], 0)
        eid = f"{source}__{EDGE_ESCALATES_TO}__{target}"

        if eid in seen_edge:
            idx = seen_edge[eid]
            existing = edges[idx]
            if rank > _CONFIDENCE_RANK.get(existing["data"]["confidence"], 0):
                also = [*(existing["data"].get("also_via") or []), existing["data"]["primitive"]]
                fresh = _edge(
                    source, target, EDGE_ESCALATES_TO,
                    label=prim["name"], primitive=key, confidence=prim["confidence"],
                    reason=why, rule=prim["rule"], evidence=evidence,
                )
                fresh["data"]["also_via"] = also
                edges[idx] = fresh
            else:
                # The weaker path is kept rather than dropped: it is still a route, and hiding it
                # makes the map look narrower than the tenant really is.
                existing["data"].setdefault("also_via", []).append(key)
            return

        bucket = (source, key)
        count = fan_out.get(bucket, 0) + 1
        fan_out[bucket] = count
        if count > MAX_FAN_OUT:
            fan_out_total[f"{source}|{key}"] = count
            return

        seen_edge[eid] = len(edges)
        edges.append(
            _edge(
                source, target, EDGE_ESCALATES_TO,
                label=prim["name"], primitive=key, confidence=prim["confidence"],
                reason=why, rule=prim["rule"], evidence=evidence,
            )
        )

    # Which principals to evaluate, and where.
    principals: dict[str, str] = {}
    for r in rows:
        pid = str(r.get("effectivePrincipalId", "") or r.get("principalId", ""))
        if not pid:
            continue
        if principal_id and pid.lower() != principal_id.lower():
            continue
        principals.setdefault(
            pid, str(r.get("effectivePrincipalName", "") or r.get("principalDisplayName", "") or pid)
        )

    scopes = _distinct_scopes(rows)
    if scope_filter:
        scopes = [s for s in scopes if effective.scope_covers(scope_filter, s) or effective.scope_covers(s, scope_filter)]

    # Index identities by the resource they are attached to, so a capability ON a resource can be
    # turned into a capability AS that resource's identity.
    identity_by_resource: dict[str, list[dict[str, Any]]] = {}
    for ident in identities.values():
        for rid in ident.get("attachedResourceIds") or []:
            identity_by_resource.setdefault(str(rid).lower(), []).append(ident)

    fic_by_identity: dict[str, list[dict[str, Any]]] = {}
    for f in federated:
        fic_by_identity.setdefault(str(f.get("identityResourceId", "")).lower(), []).append(f)

    # Index rows by principal ONCE. `evaluate` filters by principal internally, so handing it an
    # already-filtered list is equivalent — but detection runs (principals x primitives x scopes)
    # evaluations, and re-scanning the whole row set inside each one is the difference between a
    # sub-second graph and a ten-second one on a real tenant.
    by_principal: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        pid_r = str(r.get("effectivePrincipalId", "") or r.get("principalId", "")).lower()
        if pid_r:
            by_principal.setdefault(pid_r, []).append(r)

    for pid, pname in principals.items():
        src = principal_node(pid)
        src_added = False
        ident = identities.get(pid.lower())
        mine = by_principal.get(pid.lower(), [])

        # Does this principal ALREADY hold full control somewhere? If so it cannot escalate —
        # every "path" it has is a permission it already exercises. The node is still emitted
        # (the graph is about reachability, and an Owner is a legitimate target of one) but the
        # flag lets the findings stop reporting Owners as at risk of becoming Owners, which
        # would bury every real finding under the tenant's entire administrator list.
        already_tier0 = any(
            effective.evaluate(
                mine, role_index, principal_id=pid, scope=s,
                action="Microsoft.Authorization/roleAssignments/write",
                plane=effective.PLANE_CONTROL,
            ).verdict == effective.ALLOWED
            for s in scopes
        )

        for prim in PRIMITIVES:
            if _CONFIDENCE_RANK[prim["confidence"]] < min_rank:
                continue
            for scope in scopes:
                dec = None
                for action in prim["actions"]:
                    d = effective.evaluate(
                        mine, role_index, principal_id=pid, scope=scope,
                        action=action, plane=effective.PLANE_CONTROL,
                    )
                    if d.verdict == effective.ALLOWED:
                        dec = d
                        break
                if dec is None:
                    continue

                if not src_added:
                    nodes.append(
                        _node(
                            src, "principal", pname,
                            principalId=pid,
                            alreadyTier0=already_tier0,
                            identityKind=(ident or {}).get("identityKind", ""),
                            attachedResourceId=((ident or {}).get("attachedResourceIds") or [""])[0],
                        )
                    )
                    src_added = True

                decided = dec.decided_by or {}
                evidence = {
                    "assignmentId": decided.get("assignmentId", ""),
                    "action": dec.action,
                    "roleName": decided.get("roleName", ""),
                    "scope": scope,
                }
                why = f"{decided.get('roleName', 'An assignment')} at {decided.get('scopeDisplayName', scope)}"

                if prim["target"] == "tier0":
                    add_edge(src, TIER0_NODE, prim["key"], why, evidence)
                elif prim["target"] == "scope":
                    tgt = scope_node(scope)
                    nodes.append(_node(tgt, "scope", scope, scope=scope))
                    add_edge(src, tgt, prim["key"], why, evidence)
                elif prim["target"] == "identity":
                    # Can add a federated credential to any user-assigned identity in scope.
                    for other in identities.values():
                        if other.get("identityKind") != "UserAssigned":
                            continue
                        irid = str(other.get("identityResourceId", ""))
                        if not effective.scope_covers(scope, irid):
                            continue
                        tgt = identity_node(str(other["principalId"]))
                        nodes.append(
                            _node(tgt, "identity", other.get("identityName", "") or other["principalId"],
                                  principalId=other["principalId"], identityKind="UserAssigned",
                                  resourceId=irid,
                                  federatedCredentials=len(fic_by_identity.get(irid.lower(), []))),
                        )
                        add_edge(src, tgt, prim["key"], why, {**evidence, "resourceId": irid})
                elif prim["target"] == "identity_on_resource":
                    # The capability is on a RESOURCE; the escalation is to whatever identity that
                    # resource carries. Without the identity inventory there is nothing to point
                    # at, which is exactly what `limitations` warns about.
                    for rid, idents in identity_by_resource.items():
                        if not effective.scope_covers(scope, rid):
                            continue
                        for other in idents:
                            opid = str(other["principalId"])
                            if opid.lower() == pid.lower():
                                continue
                            tgt = identity_node(opid)
                            nodes.append(
                                _node(tgt, "identity",
                                      other.get("identityName", "") or opid,
                                      principalId=opid,
                                      identityKind=other.get("identityKind", ""),
                                      resourceId=other.get("identityResourceId", "")),
                            )
                            add_edge(src, tgt, prim["key"], why, {**evidence, "resourceId": rid})

    # An identity that itself holds a tier-0 primitive continues the path: hijacking it reaches
    # Owner. Without this the graph stops at "you can become the VM" and never says "…and the VM
    # is an Owner", which is the half that matters.
    for n in list(nodes):
        if n["kind"] != "identity":
            continue
        opid = str(n.get("principalId", ""))
        for scope in scopes:
            d = effective.evaluate(
                by_principal.get(opid.lower(), []), role_index, principal_id=opid, scope=scope,
                action="Microsoft.Authorization/roleAssignments/write",
                plane=effective.PLANE_CONTROL,
            )
            if d.verdict == effective.ALLOWED:
                decided = d.decided_by or {}
                add_edge(
                    n["id"], TIER0_NODE, "role_write",
                    f"{decided.get('roleName', 'An assignment')} at {decided.get('scopeDisplayName', scope)}",
                    {"assignmentId": decided.get("assignmentId", ""), "action": d.action, "scope": scope},
                )
                break

    graph = _finish(nodes, edges, limitations=limitations, fan_out_total=fan_out_total)
    graph["paths"] = shortest_paths_to(graph, TIER0_NODE)
    graph["primitives"] = [
        {k: p[k] for k in ("key", "name", "confidence", "rule")} for p in PRIMITIVES
    ]
    return graph


# --------------------------------------------------------------------------- paths
def shortest_paths_to(graph: dict[str, Any], target: str, *, max_len: int = 6) -> list[dict[str, Any]]:
    """Shortest escalation path from each principal to ``target``.

    Answers *"how many hops from a non-privileged principal to Owner?"* — a one-hop path from an
    ordinary user is a very different finding from a three-hop one, and reporting only that a
    path exists loses the distinction that decides whether anyone acts on it."""
    adj: dict[str, list[dict[str, Any]]] = {}
    for e in graph.get("edges", []):
        if e["kind"] == EDGE_ESCALATES_TO:
            adj.setdefault(e["source"], []).append(e)
    if target not in {n["id"] for n in graph.get("nodes", [])}:
        return []

    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    out: list[dict[str, Any]] = []
    for node in graph.get("nodes", []):
        if node["kind"] != "principal":
            continue
        start = node["id"]
        # BFS: the shortest path is the one an attacker takes.
        queue: list[tuple[str, list[dict[str, Any]]]] = [(start, [])]
        visited = {start}
        found: list[dict[str, Any]] | None = None
        while queue and found is None:
            current, trail = queue.pop(0)
            if len(trail) >= max_len:
                continue
            for e in adj.get(current, []):
                if e["target"] == target:
                    found = [*trail, e]
                    break
                if e["target"] not in visited:
                    visited.add(e["target"])
                    queue.append((e["target"], [*trail, e]))
        if found:
            confs = [_CONFIDENCE_RANK.get(e["data"]["confidence"], 0) for e in found]
            out.append(
                {
                    "from": start,
                    "fromLabel": node["label"],
                    "to": target,
                    "length": len(found),
                    "hops": [
                        {
                            "source": e["source"],
                            "sourceLabel": by_id.get(e["source"], {}).get("label", e["source"]),
                            "target": e["target"],
                            "targetLabel": by_id.get(e["target"], {}).get("label", e["target"]),
                            "primitive": e["data"]["primitive"],
                            "confidence": e["data"]["confidence"],
                            "reason": e["data"].get("reason", ""),
                        }
                        for e in found
                    ],
                    "min_confidence": next(
                        (k for k, v in _CONFIDENCE_RANK.items() if v == min(confs)), CONF_LOW
                    ),
                }
            )
    out.sort(key=lambda p: (p["length"], p["fromLabel"]))
    return out


# --------------------------------------------------------------------------- federated creds
# A subject that pins nothing. `repo:org/*:*` means any repository in the org; a `pull_request`
# subject means anyone who can open a PR — including from a fork.
_LOOSE_SUBJECT_MARKERS = ("*", ":pull_request", "ref:refs/heads/*")


def loose_subject_reason(subject: str) -> str:
    """Why this federated-credential subject is too permissive, or "" if it is fine."""
    s = (subject or "").strip()
    if not s:
        return "The subject is empty, so any token from this issuer is accepted."
    low = s.lower()
    if "*" in low:
        return "The subject contains a wildcard, so it matches more than one identity."
    if "pull_request" in low:
        return (
            "The subject accepts pull-request tokens, so anyone who can open a pull request — "
            "including from a fork — can assume this identity."
        )
    if low.endswith(":ref:refs/heads/") or low.endswith(":environment:"):
        return "The subject is truncated and pins no branch or environment."
    return ""


def unknown_issuer(issuer: str) -> bool:
    iss = (issuer or "").strip().lower().rstrip("/")
    if not iss:
        return True
    from app.iam.collectors import KNOWN_FIC_ISSUERS

    return not any(iss.startswith(k.lower().rstrip("/")) for k in KNOWN_FIC_ISSUERS)


__all__ = [
    "PRIMITIVES", "PRIMITIVE_BY_KEY", "MAX_FAN_OUT", "TIER0_NODE", "TIER0_LABEL",
    "detect", "shortest_paths_to", "loose_subject_reason", "unknown_issuer",
    "principal_node", "scope_node", "identity_node",
]
