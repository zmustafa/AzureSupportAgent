"""Azure Resource Graph queries that feed the seed-resource workload trace.

This is the I/O half of :mod:`app.workloads.seed` (which is pure). Everything here is
read-only and best-effort: each query is wrapped so a permission gap or a throttled
subscription degrades the trace instead of failing it.

The crawl is deliberately BOUNDED — it never enumerates the estate. It builds a candidate
universe from the seed's own neighbourhood (same resource group, matching app/deployment
tags, matching name token, resources that reference the seed) and then hydrates whatever
those resources reference, one round per hop.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.exec.command_runner import (
    KQL_RESOURCE_CAPTURE_BYTES,
    parse_kql_rows,
    run_kql_capture,
    run_kql_collect,
)
from app.workloads import seed as seed_mod

logger = logging.getLogger("app.workloads.seed_links")

# Bounds so a pathological estate can't make the trace unbounded.
_UNIVERSE_CAP = 1500      # total resources we're willing to reason about
_RG_CAP = 600             # resources pulled from the seed's resource group
_MATCH_CAP = 400          # tag / name-token matches per query
_REF_CAP = 400            # inbound references to the seed
_HYDRATE_CAP = 300        # ids hydrated per expansion round
_ID_CHUNK = 60            # ARM ids per `id in~ (...)` clause

_PROJECT = "id, name, type, location, resourceGroup, subscriptionId, tags, properties, identity"


def _esc(val: str) -> str:
    """Escape a value for safe single-quoted embedding in a KQL string."""
    return (val or "").replace("'", "''")


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _norm(row: dict[str, Any]) -> dict[str, Any]:
    """Resource Graph row → the resource shape the rest of the workload code expects."""
    return {
        "kind": "resource",
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "resource_type": row.get("type", ""),
        "location": row.get("location", ""),
        "resource_group": row.get("resourceGroup", ""),
        "subscription_id": row.get("subscriptionId", ""),
        "tags": row.get("tags") or {},
        "properties": row.get("properties") or {},
        "identity": row.get("identity") or {},
    }


async def _collect(
    kql: str, connection: dict | None, cap: int, session: str | None = None
) -> list[dict[str, Any]]:
    """Run a paged Resource Graph query, returning normalized rows (never raises)."""
    try:
        res = await run_kql_collect(
            kql, connection, max_rows=cap, page_size=min(1000, cap), session_config_dir=session
        )
    except Exception:  # noqa: BLE001
        logger.debug("seed trace query failed", exc_info=True)
        return []
    if not res.ok:
        logger.debug("seed trace query not ok: %r", (res.error or "")[:200])
        return []
    return [_norm(r) for r in res.rows]


async def resolve_seed(
    connection: dict | None, resource_id: str, *, session: str | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Look the seed resource up in Resource Graph. Returns ``(resource, error)``."""
    rid = (resource_id or "").strip().rstrip("/")
    if not rid.lower().startswith("/subscriptions/"):
        return None, "That doesn't look like an Azure resource id (it should start with /subscriptions/…)."
    if "/providers/" not in rid.lower():
        return None, "That id points at a subscription or resource group — pick an individual resource."
    kql = f"Resources | where id =~ '{_esc(rid)}' | project {_PROJECT}"
    # KQL_RESOURCE_CAPTURE_BYTES + parse_kql_rows: _PROJECT includes `properties`, and a single
    # big resource (AKS, APIM, Front Door, a large Logic App definition) can exceed the default
    # 256 KB capture cap. On the REST path that truncation yields invalid JSON, and a plain
    # json.loads would return [] - reporting a resource that plainly exists as "not found".
    cap = await run_kql_capture(
        kql, connection, output="json", session_config_dir=session,
        max_bytes=KQL_RESOURCE_CAPTURE_BYTES,
    )
    if not cap.ok:
        return None, f"Couldn't query Azure Resource Graph: {(cap.error or 'unknown error')[:200]}"
    rows = parse_kql_rows(cap.stdout)
    if not rows:
        return None, "That resource wasn't found in this connection's tenant (or you don't have read access to it)."
    return _norm(rows[0]), ""


def _tag_match_clauses(seedres: dict[str, Any]) -> list[str]:
    """KQL predicates matching the seed's authoritative app/deployment tag VALUES."""
    tags = seedres.get("tags") or {}
    if not isinstance(tags, dict):
        return []
    lowered = {str(k).lower(): str(v) for k, v in tags.items() if str(v or "").strip()}
    clauses: list[str] = []
    for key in (*seed_mod._DEPLOYMENT_TAG_KEYS, *seed_mod._APP_TAG_KEYS):
        val = lowered.get(key)
        if val:
            clauses.append(f"tostring(tags['{_esc(key)}']) =~ '{_esc(val)}'")
    return clauses[:6]


async def gather_universe(
    connection: dict | None,
    seedres: dict[str, Any],
    subscription_ids: list[str],
    *,
    max_hops: int = 2,
    session: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[str]]:
    """Build the bounded candidate universe around the seed plus its raw link tables.

    Returns ``(resources_by_lowercased_id, raw_links, notes)`` where ``raw_links`` carries
    ``references`` / ``private_endpoints`` / ``identity_grants`` for :func:`seed.build_edges`.

    ``session`` is a pre-authenticated ``az`` config dir (see :func:`open_sp_session`). A
    trace fires ~8-12 Resource Graph queries; without a shared session every one of them
    re-runs ``az login`` for a service-principal connection, which dominates the wall clock.
    """
    notes: list[str] = []
    seed_id = str(seedres.get("id", "")).lower()
    sub = str(seedres.get("subscription_id", "")) or ""
    rg = str(seedres.get("resource_group", "")) or ""
    subs = [s for s in (subscription_ids or [sub]) if s] or [sub]
    sub_list = ", ".join(f"'{_esc(s)}'" for s in subs)

    resources: dict[str, dict[str, Any]] = {seed_id: seedres}

    def _absorb(rows: list[dict[str, Any]]) -> int:
        added = 0
        for r in rows:
            rid = str(r.get("id", "")).lower()
            if not rid or rid in resources:
                continue
            if len(resources) >= _UNIVERSE_CAP:
                return added
            resources[rid] = r
            added += 1
        return added

    # 1. The seed's own resource group — the strongest prior for "same workload".
    if rg and sub:
        rows = await _collect(
            f"Resources | where subscriptionId =~ '{_esc(sub)}' and resourceGroup =~ '{_esc(rg)}' "
            f"| project {_PROJECT}",
            connection, _RG_CAP, session,
        )
        n = _absorb(rows)
        notes.append(f"resource group “{rg}”: {n} resource(s)")

    # 2. Resources carrying the seed's application / deployment tag values.
    tag_clauses = _tag_match_clauses(seedres)
    if tag_clauses:
        rows = await _collect(
            f"Resources | where subscriptionId in~ ({sub_list}) "
            f"| where {' or '.join(tag_clauses)} | project {_PROJECT}",
            connection, _MATCH_CAP, session,
        )
        n = _absorb(rows)
        if n:
            notes.append(f"matching app/deployment tags: {n} resource(s)")

    # 3. Resources whose name shares the seed's app token.
    token = seed_mod.app_token(str(seedres.get("name", "")))
    if token and len(token) >= 4:
        rows = await _collect(
            f"Resources | where subscriptionId in~ ({sub_list}) "
            f"| where name contains '{_esc(token)}' | project {_PROJECT}",
            connection, _MATCH_CAP, session,
        )
        n = _absorb(rows)
        if n:
            notes.append(f"name token “{token}”: {n} resource(s)")

    # 4. INBOUND references: anything that embeds the seed's ARM id in its properties.
    rows = await _collect(
        f"Resources | where subscriptionId in~ ({sub_list}) "
        f"| where tostring(properties) contains '{_esc(seed_id)}' | project {_PROJECT}",
        connection, _REF_CAP, session,
    )
    n = _absorb(rows)
    if n:
        notes.append(f"resources referencing the seed: {n}")

    # 5. Hydration rounds — follow OUTBOUND references one hop at a time so a 2-hop trace
    #    can reach resources that live outside the seed's neighbourhood entirely. Ids that
    #    came back empty are remembered: without that, every round re-queries the same
    #    unresolvable references (cross-tenant scopes, deleted resources) and the trace
    #    spends most of its wall-clock on queries that can never return a row.
    attempted: set[str] = set()
    scanned: set[str] = set()
    for _round in range(max(1, min(int(max_hops), 3))):
        wanted: set[str] = set()
        for rid, res in list(resources.items()):
            if rid in scanned:
                continue
            scanned.add(rid)
            wanted |= seed_mod.extract_arm_ids(res.get("properties"))
        wanted -= set(resources)
        wanted -= attempted
        if not wanted:
            break
        batch = sorted(wanted)[:_HYDRATE_CAP]
        attempted |= set(batch)
        if not _absorb(await hydrate_ids(connection, batch, session=session)):
            break

    # 6. Link tables that can't be derived from properties alone.
    references = _reference_pairs(resources)
    private_endpoints = await _private_endpoint_links(connection, subs, set(resources), session)
    identity_grants = await _identity_grants(connection, subs, resources, session)

    raw = {
        "references": references,
        "private_endpoints": private_endpoints,
        "identity_grants": identity_grants,
    }
    return resources, raw, notes


async def hydrate_ids(
    connection: dict | None, ids: list[str], *, session: str | None = None
) -> list[dict[str, Any]]:
    """Fetch full resource rows for a list of ARM ids (chunked)."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i : i + _ID_CHUNK]
        joined = ", ".join(f"'{_esc(x)}'" for x in chunk)
        out.extend(await _collect(f"Resources | where id in~ ({joined}) | project {_PROJECT}", connection, len(chunk), session))
    return out


def _reference_pairs(resources: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Every ``source → target`` ARM-id reference *within* the universe (pure)."""
    pairs: list[dict[str, str]] = []
    known = set(resources)
    for rid, res in resources.items():
        for ref in seed_mod.extract_arm_ids(res.get("properties"), exclude=(rid,)):
            if ref in known:
                pairs.append({"source": rid, "target": ref})
    return pairs


async def _private_endpoint_links(
    connection: dict | None, subs: list[str], known: set[str], session: str | None = None
) -> list[dict[str, str]]:
    """Private endpoint → the PaaS resource it fronts, restricted to the universe."""
    sub_list = ", ".join(f"'{_esc(s)}'" for s in subs)
    rows: list[dict[str, Any]] = []
    kql = (
        f"Resources | where subscriptionId in~ ({sub_list}) "
        "| where type =~ 'microsoft.network/privateendpoints' "
        "| mv-expand conn = properties.privateLinkServiceConnections "
        "| extend target = tolower(tostring(conn.properties.privateLinkServiceId)) "
        "| where isnotempty(target) "
        "| project pe = tolower(id), target"
    )
    try:
        res = await run_kql_collect(kql, connection, max_rows=2000, page_size=1000, session_config_dir=session)
        rows = res.rows if res.ok else []
    except Exception:  # noqa: BLE001
        logger.debug("seed trace private-endpoint query failed", exc_info=True)
    out: list[dict[str, str]] = []
    for row in rows:
        pe = str(row.get("pe", "")).lower()
        target = seed_mod.top_level_id(str(row.get("target", ""))).lower()
        if pe in known and target in known:
            out.append({"pe": pe, "target": target})
    return out


async def _identity_grants(
    connection: dict | None, subs: list[str], resources: dict[str, dict[str, Any]],
    session: str | None = None,
) -> list[dict[str, str]]:
    """RBAC role assignments held by the managed identities of resources in the universe.

    "This function app's identity has Storage Blob Data Contributor on that storage account"
    is close to proof of same-workload membership, and it survives estates with no tagging
    discipline at all."""
    principals: dict[str, str] = {}  # principalId -> owning resource id
    for rid, res in resources.items():
        identity = res.get("identity") or {}
        if isinstance(identity, dict):
            pid = str(identity.get("principalId", "") or "").lower()
            if pid:
                principals[pid] = rid
            for uid in (identity.get("userAssignedIdentities") or {}).values():
                if isinstance(uid, dict):
                    upid = str(uid.get("principalId", "") or "").lower()
                    if upid:
                        principals.setdefault(upid, rid)
    if not principals:
        return []

    sub_list = ", ".join(f"'{_esc(s)}'" for s in subs)
    out: list[dict[str, str]] = []
    ids = sorted(principals)
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i : i + _ID_CHUNK]
        joined = ", ".join(f"'{_esc(x)}'" for x in chunk)
        kql = (
            f"authorizationresources | where subscriptionId in~ ({sub_list}) "
            "| where type =~ 'microsoft.authorization/roleassignments' "
            "| extend pid = tolower(tostring(properties.principalId)), "
            "scope = tolower(tostring(properties.scope)), "
            "roleDef = tolower(tostring(properties.roleDefinitionId)) "
            f"| where pid in~ ({joined}) "
            "| where scope contains '/providers/' "
            "| project pid, scope, roleDef"
        )
        try:
            res = await run_kql_collect(kql, connection, max_rows=1000, page_size=1000, session_config_dir=session)
            rows = res.rows if res.ok else []
        except Exception:  # noqa: BLE001
            logger.debug("seed trace role-assignment query failed", exc_info=True)
            rows = []
        for row in rows:
            owner = principals.get(str(row.get("pid", "")).lower())
            scope = seed_mod.top_level_id(str(row.get("scope", ""))).lower()
            if owner and scope in resources and scope != owner:
                out.append({"source": owner, "scope": scope, "role": "an RBAC role"})
    return out
