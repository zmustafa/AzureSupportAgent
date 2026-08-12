"""Reverse-engineer an architecture from a workload via Azure Resource Graph.

Resolves a workload's nodes to a KQL scope predicate, then pulls every member resource
WITH its full ``properties`` (the real configuration), which is what lets the AI infer
relationships (NIC→subnet→VNet, app→plan, private endpoint→target, etc.). Read-only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.exec.command_runner import (
    KQL_RESOURCE_CAPTURE_BYTES,
    close_sp_session,
    open_sp_session,
    run_kql_capture,
    run_kql_collect,
)
from app.workloads import discovery

logger = logging.getLogger("app.architectures.reverse")

# Per-resource cap on the serialized ``properties`` blob (chars) and an overall budget so
# a huge estate can't blow the LLM context window. The most relationship-relevant keys are
# kept when a blob is trimmed.
_PER_RESOURCE_PROPS = 6000
_TOTAL_BUDGET = 120_000
_PREDICATE_BUDGET = 6000
_DEFAULT_INVENTORY_ROWS = 20_000
_DEFAULT_ARCH_CONTEXT_RESOURCES = 240
_DEFAULT_ARCH_CONTEXT_BYTES = 350_000
_REL_KEYS = (
    "networkprofile", "ipconfigurations", "subnet", "privatelinkserviceconnections",
    "privateendpoint", "serverfarmid", "storageprofile", "agentpoolprofiles",
    "addonprofiles", "vnetsubnetid", "backendpools", "backendaddresspools",
    "routingrules", "frontendipconfigurations", "networkacls", "virtualnetworkrules",
    "privateendpointconnections", "keyvaultproperties", "connectionstrings",
    "siteconfig", "outboundipaddresses", "hostnames", "primaryendpoints",
)

_TOPOLOGY_TYPE_HINTS = (
    "virtualnetworks", "subnets", "networkinterfaces", "privateendpoints",
    "applicationgateways", "loadbalancers", "frontdoors", "profiles",
    "sites", "serverfarms", "managedclusters", "containergroups", "virtualmachines",
    "sql", "postgresql", "mysql", "cosmos", "redis", "storageaccounts",
    "servicebus", "eventhubs", "keyvault", "managedidentities", "workspaces",
    "components", "diagnosticsettings",
)


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _sub_guid(value: str) -> str:
    if not value:
        return ""
    m = re.search(r"/subscriptions/([0-9a-fA-F-]{36})", value)
    return m.group(1) if m else value


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


async def resolve_scope(workload: dict[str, Any], connection: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a workload's nodes into scope sets (subs / RG pairs / resource ids).

    Returns {subs, rg_pairs, resource_ids, resource_rgs, error}. ``resource_rgs`` are the
    (sub, rg) pairs derived from individual resource nodes — used to keep the Resource
    Graph query short while still post-filtering to exact membership."""
    subs: set[str] = set()
    rg_pairs: set[tuple[str, str]] = set()
    resource_ids: set[str] = set()
    resource_rgs: set[tuple[str, str]] = set()
    memberships: list[dict[str, Any]] = []

    for node in workload.get("nodes", []):
        kind = node.get("kind")
        excludes = {
            str(value).strip().rstrip("/").lower()
            for value in node.get("excludes", []) or [] if str(value).strip()
        }
        if kind == "subscription":
            guid = _sub_guid(node.get("id", "")) or _sub_guid(node.get("subscription_id", ""))
            if guid:
                subs.add(guid)
                memberships.append({"kind": kind, "subscriptions": {guid.lower()}, "excludes": excludes})
        elif kind == "mg":
            mg_id = node.get("id", "")
            try:
                mg_subs = {_sub_guid(s).lower() for s in await discovery.subscriptions_under_mg(connection, mg_id) if _sub_guid(s)}
                subs.update(mg_subs)
                memberships.append({"kind": kind, "subscriptions": mg_subs, "excludes": excludes})
            except Exception as exc:  # noqa: BLE001
                logger.warning("MG expansion failed for %s: %s", mg_id, exc)
        elif kind == "resource_group":
            guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(node.get("id", ""))
            rg = node.get("resource_group") or node.get("name", "")
            if guid and rg:
                rg_pairs.add((guid, rg))
                memberships.append({
                    "kind": kind, "subscription": guid.lower(), "resource_group": rg.lower(),
                    "excludes": excludes,
                })
        elif kind == "resource":
            rid = node.get("id", "")
            if rid:
                resource_ids.add(rid.lower())
                guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(rid)
                m = re.search(r"/resourcegroups/([^/]+)", rid, re.IGNORECASE)
                rg = node.get("resource_group") or (m.group(1) if m else "")
                if guid and rg:
                    resource_rgs.add((guid, rg))
                memberships.append({"kind": kind, "resource_id": rid.rstrip("/").lower(), "excludes": excludes})

    has_scope = bool(subs or rg_pairs or resource_ids)
    return {
        "subs": subs,
        "rg_pairs": rg_pairs,
        "resource_ids": resource_ids,
        "resource_rgs": resource_rgs,
        "memberships": memberships,
        "has_excludes": any(item.get("excludes") for item in memberships),
        "error": "" if has_scope else "Workload has no resolvable scope (empty membership).",
    }


def _row_in_membership(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    resource_id = str(row.get("id") or "").strip().rstrip("/").lower()
    subscription = str(row.get("subscriptionId") or "").lower()
    resource_group = str(row.get("resourceGroup") or row.get("name") or "").lower()
    for member in scope.get("memberships") or []:
        kind = member.get("kind")
        included = (
            (kind in {"subscription", "mg"} and subscription in member.get("subscriptions", set()))
            or (kind == "resource_group" and subscription == member.get("subscription") and resource_group == member.get("resource_group"))
            or (kind == "resource" and resource_id == member.get("resource_id"))
        )
        if not included:
            continue
        excluded = any(
            resource_id == value or resource_id.startswith(value + "/")
            for value in member.get("excludes") or set()
        )
        if not excluded:
            return True
    return False


def _pack_or_clauses(clauses: list[str], budget: int = _PREDICATE_BUDGET) -> list[str]:
    packed: list[str] = []
    current: list[str] = []
    current_len = 0
    for clause in clauses:
        added = len(clause) + (4 if current else 0)
        if current and current_len + added > budget:
            packed.append(" or ".join(current))
            current, current_len = [], 0
        current.append(clause)
        current_len += len(clause) + (4 if len(current) > 1 else 0)
    if current:
        packed.append(" or ".join(current))
    return packed


def _id_predicates(ids: list[str], *, field: str = "id", budget: int = _PREDICATE_BUDGET) -> list[str]:
    predicates: list[str] = []
    current: list[str] = []
    current_len = len(field) + 8
    for value in sorted(set(ids)):
        token = f"'{_esc(value)}'"
        added = len(token) + (2 if current else 0)
        if current and current_len + added > budget:
            predicates.append(f"{field} in~ ({', '.join(current)})")
            current, current_len = [], len(field) + 8
        current.append(token)
        current_len += added
    if current:
        predicates.append(f"{field} in~ ({', '.join(current)})")
    return predicates


def _resource_predicates(scope: dict[str, Any]) -> list[str]:
    """Return disjoint, length-bounded predicates for the exact workload membership."""
    subs = {str(value).lower() for value in scope.get("subs") or set() if value}
    rg_pairs = {(str(sub).lower(), str(rg).lower()) for sub, rg in scope.get("rg_pairs") or set()}
    predicates = _id_predicates(sorted(subs), field="subscriptionId")
    predicates.extend(_pack_or_clauses([
        f"(subscriptionId =~ '{_esc(sub)}' and resourceGroup =~ '{_esc(rg)}')"
        for sub, rg in sorted(rg_pairs)
        if sub not in subs
    ]))
    uncovered_ids: list[str] = []
    for resource_id in scope.get("resource_ids") or set():
        sub, rg = _rg_of(resource_id)
        if sub in subs or (sub, rg) in rg_pairs:
            continue
        uncovered_ids.append(str(resource_id))
    predicates.extend(_id_predicates(uncovered_ids))
    return predicates


def _resource_group_predicates(scope: dict[str, Any]) -> list[str]:
    subs = {str(value).lower() for value in scope.get("subs") or set() if value}
    predicates = _id_predicates(sorted(subs), field="subscriptionId")
    pairs = set(scope.get("rg_pairs") or set()) | set(scope.get("resource_rgs") or set())
    predicates.extend(_pack_or_clauses([
        f"(subscriptionId =~ '{_esc(str(sub))}' and name =~ '{_esc(str(rg))}')"
        for sub, rg in sorted(pairs)
        if str(sub).lower() not in subs
    ]))
    return predicates



def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        # A payload cut at the capture cap is invalid JSON. Returning [] here would report
        # "no resources" for a workload that has plenty (the recurring silent-zero bug), so
        # salvage every complete object before the cut instead.
        from app.exec.command_runner import parse_kql_rows

        return parse_kql_rows(stdout)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _rg_of(arm_id: str) -> tuple[str, str]:
    """(subscriptionId, resourceGroup) lowercased from an ARM id, or ('','')."""
    aid = (arm_id or "").lower()
    sub = _sub_guid(aid)
    rg = ""
    m = re.search(r"/resourcegroups/([^/]+)", aid)
    if m:
        rg = m.group(1)
    return (sub if sub != aid else "", rg)


async def live_resources_in_diagram_scope(
    arm_ids: list[str], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Drift fallback for an architecture NOT linked to a workload: query live resources in
    every (subscription, resource group) the diagram's ARM-id nodes already live in. Lets a
    reverse-engineered diagram be diffed against Azure without a workload link. Read-only.

    Returns {resources: [{id,name,type,resourceGroup,subscriptionId}], error}."""
    pairs = {_rg_of(a) for a in arm_ids if a}
    pairs = {(s, rg) for (s, rg) in pairs if s and rg}
    if not pairs:
        return {"resources": [], "error": "The diagram has no Azure-linked resources to compare."}
    clauses = [
        f"(subscriptionId =~ '{_esc(s)}' and resourceGroup =~ '{_esc(rg)}')" for (s, rg) in sorted(pairs)
    ]
    predicate = " or ".join(clauses)
    config_dir, sess_err = await open_sp_session(connection)
    if sess_err:
        return {"resources": [], "error": sess_err}
    try:
        kql = (
            f"Resources | where {predicate} "
            "| project id, name, type, resourceGroup, subscriptionId | limit 1000"
        )
        cap = await run_kql_capture(kql, connection, output="json", session_config_dir=config_dir)
        if not cap.ok:
            return {"resources": [], "error": cap.error or "Resource Graph query failed."}
        rows = _parse_rows(cap.stdout)
        return {
            "resources": [
                {
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "type": r.get("type", ""),
                    "resourceGroup": r.get("resourceGroup", ""),
                    "subscriptionId": r.get("subscriptionId", ""),
                }
                for r in rows
                if r.get("id")
            ],
            "error": "",
        }
    finally:
        close_sp_session(config_dir)


def _trim_properties(props: Any) -> Any:
    """Trim an oversized ``properties`` blob, keeping relationship-relevant keys."""
    if not isinstance(props, dict):
        return props
    blob = json.dumps(props, separators=(",", ":"))
    if len(blob) <= _PER_RESOURCE_PROPS:
        return props
    # Keep only keys whose name hints at a relationship; summarize the rest.
    kept: dict[str, Any] = {}
    for k, v in props.items():
        if any(rk in k.lower() for rk in _REL_KEYS):
            kept[k] = v
    kept["_trimmed"] = True
    out = json.dumps(kept, separators=(",", ":"))
    if len(out) > _PER_RESOURCE_PROPS:
        # Still too big — hard truncate the relationship subset.
        return {"_truncated": out[:_PER_RESOURCE_PROPS]}
    return kept


async def collect_workload_inventory(
    workload: dict[str, Any], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Collect the exact light workload inventory with paging and completeness metadata."""
    scope = await resolve_scope(workload, connection)
    empty = {
        "resources": [], "count": 0, "known_total": 0, "complete": False,
        "partial": False, "pages": 0, "query_batches": 0, "failed_batches": 0,
        "truncated": False, "limit_reason": "", "warnings": [], "predicate": "",
        "scope": scope,
    }
    if scope["error"]:
        return {**empty, "error": scope["error"]}
    predicates = _resource_predicates(scope)
    if not predicates:
        return {**empty, "error": "Workload scope could not be resolved to a query."}

    max_rows = _bounded_env("MISSION_INVENTORY_MAX_ROWS", _DEFAULT_INVENTORY_ROWS, 1000, 100_000)
    projection = "id, name, type, kind, location, resourceGroup, subscriptionId, sku, identity, zones, tags"
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    query_batches = 0
    failed_batches = 0
    known_total = 0
    total_known = True
    complete = True
    warnings: list[str] = []

    config_dir, sess_err = await open_sp_session(connection)
    if sess_err:
        return {**empty, "failed_batches": 1, "predicate": " or ".join(predicates), "error": sess_err}
    try:
        for predicate in predicates:
            remaining = max_rows - len(resources)
            query_batches += 1
            result = await run_kql_collect(
                f"Resources | where {predicate} | project {projection} | order by id asc",
                connection, session_config_dir=config_dir, max_rows=max(1, remaining),
            )
            pages += result.pages
            if not result.ok:
                failed_batches += 1
                return {
                    **empty, "pages": pages, "query_batches": query_batches,
                    "failed_batches": failed_batches, "known_total": None,
                    "predicate": " or ".join(predicates),
                    "error": result.error or "Resource Graph query failed.",
                }
            if result.total is None:
                total_known = False
            else:
                known_total += int(result.total)
            complete = complete and result.complete
            for row in result.rows[:max(0, remaining)]:
                if not _row_in_membership(row, scope):
                    continue
                rid = str(row.get("id") or "")
                key = rid.rstrip("/").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                resources.append({
                    "id": rid, "name": row.get("name", ""), "type": row.get("type", ""),
                    "kind": row.get("kind"), "location": row.get("location"),
                    "resourceGroup": row.get("resourceGroup"), "subscriptionId": row.get("subscriptionId"),
                    "sku": row.get("sku"), "identity": row.get("identity"),
                    "zones": row.get("zones"), "tags": row.get("tags"), "properties": None,
                })

        # Resource groups are taggable containers in ResourceContainers rather than Resources.
        for predicate in _resource_group_predicates(scope):
            remaining = max_rows - len(resources)
            query_batches += 1
            result = await run_kql_collect(
                "resourcecontainers "
                "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
                f"| where {predicate} "
                "| project id, name, type, location, subscriptionId, tags | order by id asc",
                connection, session_config_dir=config_dir, max_rows=max(1, remaining),
            )
            pages += result.pages
            if not result.ok:
                failed_batches += 1
                complete = False
                total_known = False
                warnings.append(f"Resource groups: {str(result.error or 'query failed')[:160]}")
                continue
            if result.total is None:
                total_known = False
            else:
                known_total += int(result.total)
            complete = complete and result.complete
            for row in result.rows[:max(0, remaining)]:
                if not _row_in_membership(row, scope):
                    continue
                rid = str(row.get("id") or "")
                key = rid.rstrip("/").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                resources.append({
                    "id": rid, "name": row.get("name", ""), "type": row.get("type", ""),
                    "kind": None, "location": row.get("location"),
                    "resourceGroup": row.get("name", ""), "subscriptionId": row.get("subscriptionId"),
                    "sku": None, "identity": None, "zones": None, "tags": row.get("tags"),
                    "properties": None,
                })
    finally:
        close_sp_session(config_dir)

    limit_reason = ""
    if len(resources) >= max_rows and not complete:
        limit_reason = f"Workload inventory retained the first {max_rows:,} rows."
    filtered_known_total = (
        len(resources) if complete and not warnings
        else (known_total if total_known and not scope.get("has_excludes") else None)
    )
    return {
        "resources": resources, "count": len(resources),
        "known_total": filtered_known_total,
        "complete": complete and not warnings, "partial": not complete or bool(warnings),
        "pages": pages, "query_batches": query_batches, "failed_batches": failed_batches,
        "truncated": bool(limit_reason), "limit_reason": limit_reason, "warnings": warnings,
        "predicate": " or ".join(predicates), "scope": scope, "error": "",
    }


def _topology_priority(resource: dict[str, Any]) -> tuple[int, str]:
    resource_type = str(resource.get("type") or "").lower()
    priority = 0 if any(hint in resource_type for hint in _TOPOLOGY_TYPE_HINTS) else 1
    return priority, str(resource.get("id") or "").lower()


def _compact_arch_resource(resource: dict[str, Any]) -> dict[str, Any]:
    compact = dict(resource)
    tags = resource.get("tags")
    if isinstance(tags, dict):
        compact["tags"] = {str(k)[:80]: str(v)[:160] for k, v in list(sorted(tags.items()))[:12]}
    identity = resource.get("identity")
    if isinstance(identity, dict):
        compact["identity"] = {
            key: identity.get(key) for key in ("type", "principalId", "tenantId") if identity.get(key)
        }
    compact["properties"] = None
    return compact


def _select_architecture_resources(resources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_resources = _bounded_env(
        "MISSION_ARCHITECTURE_CONTEXT_RESOURCES", _DEFAULT_ARCH_CONTEXT_RESOURCES, 20, 2000
    )
    if len(resources) <= max_resources:
        selected = [_compact_arch_resource(resource) for resource in sorted(resources, key=_topology_priority)]
        total = len(resources)
        return selected, {
            "mode": "detailed", "total_resource_count": total,
            "direct_resource_count": total, "represented_resource_count": total,
            "aggregated_resource_count": 0, "omitted_resource_count": 0,
            "resource_group_count": len({str(r.get('resourceGroup') or '').lower() for r in resources if r.get('resourceGroup')}),
            "type_count": len({str(r.get('type') or '').lower() for r in resources if r.get('type')}),
        }
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for resource in resources:
        sku = resource.get("sku")
        sku_name = str(sku.get("name") or sku.get("tier") or "") if isinstance(sku, dict) else str(sku or "")
        key = (
            str(resource.get("type") or "").lower(), str(resource.get("resourceGroup") or "").lower(),
            str(resource.get("location") or "").lower(), sku_name.lower(),
        )
        groups.setdefault(key, []).append(resource)

    selected: list[dict[str, Any]] = []
    represented = 0
    for rows in sorted(groups.values(), key=lambda values: _topology_priority(values[0]))[:max_resources]:
        representative = _compact_arch_resource(sorted(rows, key=lambda row: str(row.get("id") or "").lower())[0])
        representative["_aggregate_count"] = len(rows)
        selected.append(representative)
        represented += len(rows)
    total = len(resources)
    return selected, {
        "mode": "summarized",
        "total_resource_count": total, "direct_resource_count": len(selected),
        "represented_resource_count": represented,
        "aggregated_resource_count": max(0, represented - len(selected)),
        "omitted_resource_count": max(0, total - represented),
        "resource_group_count": len({str(r.get('resourceGroup') or '').lower() for r in resources if r.get('resourceGroup')}),
        "type_count": len({str(r.get('type') or '').lower() for r in resources if r.get('type')}),
    }


def _property_id_chunks(ids: list[str], budget: int = _PREDICATE_BUDGET) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 64
    for resource_id in ids:
        added = len(resource_id) + 4
        if current and current_len + added > budget:
            chunks.append(current)
            current, current_len = [], 64
        current.append(resource_id)
        current_len += added
    if current:
        chunks.append(current)
    return chunks


async def build_architecture_context(
    inventory: dict[str, Any], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Build a deterministic, byte-bounded AI context from a light inventory result."""
    if inventory.get("error"):
        return {**inventory, "resources": []}
    selected, context = _select_architecture_resources(list(inventory.get("resources") or []))
    by_id = {str(row.get("id") or "").lower(): row for row in selected if row.get("id")}
    ids = [row["id"] for row in selected if row.get("id") and "/providers/" in str(row.get("id")).lower()]
    used = 0
    property_enriched = 0
    warnings = list(inventory.get("warnings") or [])

    config_dir, sess_err = await open_sp_session(connection)
    if sess_err:
        return {**inventory, "resources": [], "error": sess_err}
    try:
        async def enrich(chunk: list[str]) -> None:
            nonlocal used, property_enriched
            if not chunk:
                return
            joined = ", ".join(f"'{_esc(value)}'" for value in chunk)
            result = await run_kql_capture(
                f"Resources | where id in~ ({joined}) | project id, properties",
                connection, output="json", session_config_dir=config_dir,
                max_bytes=KQL_RESOURCE_CAPTURE_BYTES,
            )
            if not result.ok:
                if "truncat" in str(result.error or "").lower() and len(chunk) > 1:
                    midpoint = len(chunk) // 2
                    await enrich(chunk[:midpoint])
                    await enrich(chunk[midpoint:])
                    return
                for resource_id in chunk:
                    target = by_id.get(resource_id.lower())
                    if target is not None:
                        target["properties"] = {"_omitted": "capture failure"}
                warnings.append(f"Architecture properties: {str(result.error or 'query failed')[:160]}")
                return
            for row in _parse_rows(result.stdout):
                target = by_id.get(str(row.get("id") or "").lower())
                if target is None:
                    continue
                trimmed = _trim_properties(row.get("properties"))
                size = len(json.dumps(trimmed, separators=(",", ":")))
                if used + size > _TOTAL_BUDGET:
                    target["properties"] = {"_omitted": "property budget"}
                    continue
                target["properties"] = trimmed
                used += size
                property_enriched += 1

        for chunk in _property_id_chunks(ids):
            await enrich(chunk)
    finally:
        close_sp_session(config_dir)

    context["property_enriched_count"] = property_enriched
    context["property_omitted_count"] = max(0, len(ids) - property_enriched)
    context["inventory_complete"] = bool(inventory.get("complete"))
    context["warnings"] = warnings
    max_context_bytes = _bounded_env(
        "MISSION_ARCHITECTURE_CONTEXT_BYTES", _DEFAULT_ARCH_CONTEXT_BYTES, 50_000, 2_000_000
    )
    while selected and len(json.dumps(selected, separators=(",", ":"))) > max_context_bytes:
        removed = selected.pop()
        removed_count = int(removed.get("_aggregate_count") or 1)
        context["omitted_resource_count"] += removed_count
        context["represented_resource_count"] -= removed_count
        context["direct_resource_count"] = len(selected)
        context["mode"] = "summarized"
    context["serialized_bytes"] = len(json.dumps(selected, separators=(",", ":")))

    return {
        **inventory, "resources": selected, "count": len(selected),
        "partial": bool(inventory.get("partial")) or context["mode"] == "summarized" or bool(warnings),
        "complete": bool(inventory.get("complete")) and context["mode"] == "detailed" and not warnings,
        "warnings": warnings, "context": context,
    }


async def dump_resources(
    workload: dict[str, Any], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Compatibility wrapper returning a bounded architecture context for a workload."""
    inventory = await collect_workload_inventory(workload, connection)
    return await build_architecture_context(inventory, connection)

