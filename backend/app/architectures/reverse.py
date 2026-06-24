"""Reverse-engineer an architecture from a workload via Azure Resource Graph.

Resolves a workload's nodes to a KQL scope predicate, then pulls every member resource
WITH its full ``properties`` (the real configuration), which is what lets the AI infer
relationships (NIC→subnet→VNet, app→plan, private endpoint→target, etc.). Read-only.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.exec.command_runner import (
    close_sp_session,
    open_sp_session,
    run_kql_capture,
)
from app.workloads import discovery

logger = logging.getLogger("app.architectures.reverse")

# Per-resource cap on the serialized ``properties`` blob (chars) and an overall budget so
# a huge estate can't blow the LLM context window. The most relationship-relevant keys are
# kept when a blob is trimmed.
_PER_RESOURCE_PROPS = 6000
_TOTAL_BUDGET = 120_000
_REL_KEYS = (
    "networkprofile", "ipconfigurations", "subnet", "privatelinkserviceconnections",
    "privateendpoint", "serverfarmid", "storageprofile", "agentpoolprofiles",
    "addonprofiles", "vnetsubnetid", "backendpools", "backendaddresspools",
    "routingrules", "frontendipconfigurations", "networkacls", "virtualnetworkrules",
    "privateendpointconnections", "keyvaultproperties", "connectionstrings",
    "siteconfig", "outboundipaddresses", "hostnames", "primaryendpoints",
)


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

    for node in workload.get("nodes", []):
        kind = node.get("kind")
        if kind == "subscription":
            guid = _sub_guid(node.get("id", "")) or _sub_guid(node.get("subscription_id", ""))
            if guid:
                subs.add(guid)
        elif kind == "mg":
            mg_id = node.get("id", "")
            try:
                for s in await discovery.subscriptions_under_mg(connection, mg_id):
                    subs.add(_sub_guid(s))
            except Exception as exc:  # noqa: BLE001
                logger.warning("MG expansion failed for %s: %s", mg_id, exc)
        elif kind == "resource_group":
            guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(node.get("id", ""))
            rg = node.get("resource_group") or node.get("name", "")
            if guid and rg:
                rg_pairs.add((guid, rg))
        elif kind == "resource":
            rid = node.get("id", "")
            if rid:
                resource_ids.add(rid.lower())
                guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(rid)
                m = re.search(r"/resourcegroups/([^/]+)", rid, re.IGNORECASE)
                rg = node.get("resource_group") or (m.group(1) if m else "")
                if guid and rg:
                    resource_rgs.add((guid, rg))

    has_scope = bool(subs or rg_pairs or resource_ids)
    return {
        "subs": subs,
        "rg_pairs": rg_pairs,
        "resource_ids": resource_ids,
        "resource_rgs": resource_rgs,
        "error": "" if has_scope else "Workload has no resolvable scope (empty membership).",
    }


def _query_predicate(scope: dict[str, Any]) -> str:
    """A SHORT KQL predicate: whole subs + RG pairs (incl. resource nodes' RGs).

    Individual resource ids are NOT listed (that overflows the query); we query their
    resource groups and post-filter the rows to exact membership instead."""
    clauses: list[str] = []
    subs: set[str] = scope["subs"]
    if subs:
        joined = ", ".join(f"'{_esc(s)}'" for s in sorted(subs))
        clauses.append(f"subscriptionId in~ ({joined})")
    rg_all = set(scope["rg_pairs"]) | set(scope["resource_rgs"])
    # Drop RG pairs already covered by a whole-subscription clause.
    rg_all = {(g, rg) for (g, rg) in rg_all if g not in subs}
    for guid, rg in sorted(rg_all):
        clauses.append(f"(subscriptionId =~ '{_esc(guid)}' and resourceGroup =~ '{_esc(rg)}')")
    return " or ".join(clauses)


def _in_membership(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    """True if a returned row is actually a member of the workload (exact)."""
    sub = (row.get("subscriptionId") or "").lower()
    rg = (row.get("resourceGroup") or "").lower()
    rid = (row.get("id") or "").lower()
    if sub and sub in {s.lower() for s in scope["subs"]}:
        return True
    if any(sub == g.lower() and rg == r.lower() for (g, r) in scope["rg_pairs"]):
        return True
    if rid in scope["resource_ids"]:
        return True
    return False



def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
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
    config_dir, _sess_err = await open_sp_session(connection)
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


async def dump_resources(
    workload: dict[str, Any], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Pull all member resources with full properties for AI architecture inference.

    Two-pass to stay within Resource Graph's query-length and the runner's output caps:
    (1) list members WITHOUT properties (small even for hundreds of resources, then
    filtered to exact membership); (2) fetch trimmed ``properties`` in small id-chunks
    within an overall budget. Returns {resources, count, predicate, error}."""
    scope = await resolve_scope(workload, connection)
    if scope["error"]:
        return {"resources": [], "count": 0, "predicate": "", "error": scope["error"]}
    predicate = _query_predicate(scope)
    if not predicate:
        return {"resources": [], "count": 0, "predicate": "", "error": "Workload scope could not be resolved to a query."}

    config_dir, _sess_err = await open_sp_session(connection)
    try:
        # --- Pass 1: lightweight inventory (no properties) ---
        list_kql = (
            f"Resources | where {predicate} "
            "| project id, name, type, kind, location, resourceGroup, subscriptionId, sku, identity, zones, tags "
            "| limit 1000"
        )
        cap = await run_kql_capture(list_kql, connection, output="json", session_config_dir=config_dir)
        if not cap.ok:
            return {"resources": [], "count": 0, "predicate": predicate, "error": cap.error or "Resource Graph query failed."}
        rows = [r for r in _parse_rows(cap.stdout) if _in_membership(r, scope)]

        resources: list[dict[str, Any]] = [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "kind": r.get("kind"),
                "location": r.get("location"),
                "resourceGroup": r.get("resourceGroup"),
                "subscriptionId": r.get("subscriptionId"),
                "sku": r.get("sku"),
                "identity": r.get("identity"),
                "zones": r.get("zones"),
                "tags": r.get("tags"),
                "properties": None,
            }
            for r in rows
        ]
        by_id = {r["id"].lower(): r for r in resources if r["id"]}

        # --- Resource GROUPS in scope (taggable containers) ---
        # Resource groups live in the `resourcecontainers` table, NOT `Resources`, so the pass-1
        # query above never returns them. Include the RGs this workload touches so tag operations
        # (apply / remove / revert) cover RG-level tags alongside their resources. In scope:
        #   * every RG in a whole-subscription member,
        #   * explicit resource-group nodes (rg_pairs),
        #   * the RGs that contain the workload's member resources (resource_rgs).
        rg_targets = set(scope["rg_pairs"]) | set(scope["resource_rgs"])
        sub_set = {s.lower() for s in scope["subs"]}
        rg_pred_clauses: list[str] = []
        if scope["subs"]:
            joined_subs = ", ".join(f"'{_esc(s)}'" for s in sorted(scope["subs"]))
            rg_pred_clauses.append(f"subscriptionId in~ ({joined_subs})")
        for guid, rg in sorted({(g, r) for (g, r) in rg_targets if g.lower() not in sub_set}):
            rg_pred_clauses.append(f"(subscriptionId =~ '{_esc(guid)}' and name =~ '{_esc(rg)}')")
        if rg_pred_clauses:
            rg_kql = (
                "resourcecontainers "
                "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
                f"| where {' or '.join(rg_pred_clauses)} "
                "| project id, name, type, location, subscriptionId, tags | limit 1000"
            )
            rg_cap = await run_kql_capture(rg_kql, connection, output="json", session_config_dir=config_dir)
            if rg_cap.ok:
                rg_pair_set = {(g.lower(), r.lower()) for (g, r) in rg_targets}
                for rr in _parse_rows(rg_cap.stdout):
                    rsub = (rr.get("subscriptionId") or "").lower()
                    rname = (rr.get("name") or "").lower()
                    # Membership: whole-sub RG, or an explicitly-targeted (sub, rg) pair.
                    if rsub not in sub_set and (rsub, rname) not in rg_pair_set:
                        continue
                    rid = rr.get("id", "")
                    if not rid or rid.lower() in by_id:
                        continue
                    rg_entry = {
                        "id": rid, "name": rr.get("name", ""), "type": rr.get("type", ""),
                        "kind": None, "location": rr.get("location"),
                        "resourceGroup": rr.get("name", ""), "subscriptionId": rr.get("subscriptionId"),
                        "sku": None, "identity": None, "zones": None, "tags": rr.get("tags"),
                        "properties": None,
                    }
                    resources.append(rg_entry)
                    by_id[rid.lower()] = rg_entry

        # --- Pass 2: fetch trimmed properties in small id-chunks, within budget ---
        ids = [r["id"] for r in resources if r["id"]]
        used = sum(len(json.dumps(r, separators=(",", ":"))) for r in resources)
        for chunk in _chunks(ids, 30):
            if used >= _TOTAL_BUDGET:
                break
            joined = ", ".join(f"'{_esc(i)}'" for i in chunk)
            prop_kql = f"Resources | where id in~ ({joined}) | project id, properties"
            pcap = await run_kql_capture(prop_kql, connection, output="json", session_config_dir=config_dir)
            if not pcap.ok:
                continue
            for pr in _parse_rows(pcap.stdout):
                target = by_id.get((pr.get("id") or "").lower())
                if target is None:
                    continue
                trimmed = _trim_properties(pr.get("properties"))
                size = len(json.dumps(trimmed, separators=(",", ":")))
                if used + size > _TOTAL_BUDGET:
                    target["properties"] = {"_omitted": "budget"}
                    continue
                used += size
                target["properties"] = trimmed
    finally:
        close_sp_session(config_dir)

    return {"resources": resources, "count": len(resources), "predicate": predicate, "error": ""}


def _chunks(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]

