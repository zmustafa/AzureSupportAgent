"""Inventory collection: query every resource the connection can see (Azure Resource
Graph, paged per-subscription to stay under the 1000-row / 256 KB caps), attribute each
resource to the Azure Workload(s) it belongs to, and compute facets for filtering.

Read-only. The workload attribution reuses the same scope resolution the policy + reverse
modules use, so membership stays consistent across the app.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.exec.command_runner import (
    KQL_MAX_ROWS,
    close_sp_session,
    open_sp_session,
    run_kql_capture,
)
from app.workloads.registry import list_workloads

logger = logging.getLogger("app.inventory.service")

# Compact projection keeps each row small so a subscription's worth of resources fits well
# under the 256 KB capture cap. ``size`` surfaces VM hardware profile so NL search like
# "D-series VMs" can match; ``sku``/``tier`` cover the rest. The trailing fields power
# deterministic hygiene/orphan detection (unattached disk, orphaned NIC, idle public IP).
_PROJECT = (
    "id, name, type, kind, location, resourceGroup, subscriptionId, tags, "
    "sku=tostring(sku.name), tier=tostring(sku.tier), "
    "size=tostring(properties.hardwareProfile.vmSize), managedBy, "
    "diskState=tostring(properties.diskState), "
    "nicVm=tostring(properties.virtualMachine.id), "
    "pipAssoc=tostring(properties.ipConfiguration.id)"
)


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


async def _arg(kql: str, connection: dict[str, Any] | None, session_dir: str | None) -> tuple[list[dict[str, Any]], str]:
    """Run a Resource Graph query, return (rows, error)."""
    res = await run_kql_capture(kql, connection, output="json", session_config_dir=session_dir)
    if not res.ok:
        return [], (res.error or res.stderr or "Query failed.").strip()[:400]
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return [], "Could not parse Resource Graph output."
    if isinstance(data, dict):
        data = data.get("data", data.get("value", []))
    return (data if isinstance(data, list) else []), ""


async def _subscriptions(connection: dict[str, Any] | None, session_dir: str | None) -> list[dict[str, str]]:
    """Subscriptions visible to the connection: [{id, name}]."""
    rows, _ = await _arg(
        "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' "
        "| project subscriptionId, name | order by name asc | limit 500",
        connection, session_dir,
    )
    return [{"id": r.get("subscriptionId", ""), "name": r.get("name", "") or r.get("subscriptionId", "")} for r in rows if r.get("subscriptionId")]


async def _workload_scopes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """For every defined workload, resolve its membership scopes (subscriptions, RG pairs,
    individual resource ids). Used to attribute each resource to its workload(s)."""
    from app.architectures.reverse import resolve_scope

    out: list[dict[str, Any]] = []
    for wl in list_workloads():
        try:
            sc = await resolve_scope(wl, connection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scope resolution failed for workload %s: %s", wl.get("id"), exc)
            continue
        out.append({
            "id": wl.get("id", ""),
            "name": wl.get("name", "workload"),
            "subs": {s.lower() for s in (sc.get("subs") or set())},
            "rg_pairs": {(s.lower(), (rg or "").lower()) for s, rg in (sc.get("rg_pairs") or set())},
            "resource_ids": {r.lower() for r in (sc.get("resource_ids") or set())},
        })
    return out


def _resource_workloads(
    resource_id: str, sub_id: str, rg: str, wl_scopes: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Which workload(s) a resource belongs to — by whole-subscription, whole-RG, or
    individual-resource membership. A resource may belong to several (overlap allowed)."""
    rid = (resource_id or "").lower()
    sub = (sub_id or "").lower()
    rgl = (rg or "").lower()
    hits: list[dict[str, str]] = []
    for w in wl_scopes:
        if sub in w["subs"] or (sub, rgl) in w["rg_pairs"] or rid in w["resource_ids"]:
            hits.append({"id": w["id"], "name": w["name"]})
    return hits


def _hygiene_flags(row: dict[str, Any], rtype: str, managed_by: str, tags: dict[str, Any]) -> list[str]:
    """Deterministic cleanup/hygiene flags for a resource (orphans + tag gaps). Each flag is a
    short machine key the UI maps to a friendly label."""
    flags: list[str] = []
    if not tags:
        flags.append("untagged")
    if rtype == "microsoft.compute/disks":
        state = (row.get("diskState") or "").lower()
        if state == "unattached" or (not managed_by and state not in ("attached", "reserved")):
            flags.append("unattached_disk")
    elif rtype == "microsoft.network/networkinterfaces":
        if not (row.get("nicVm") or "").strip():
            flags.append("orphaned_nic")
    elif rtype == "microsoft.network/publicipaddresses":
        if not (row.get("pipAssoc") or "").strip():
            flags.append("idle_public_ip")
    return flags


def _normalize(row: dict[str, Any], wl_scopes: list[dict[str, Any]]) -> dict[str, Any]:
    rid = row.get("id", "")
    sub = row.get("subscriptionId", "")
    rg = row.get("resourceGroup", "")
    tags = row.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    rtype = (row.get("type", "") or "").lower()
    managed_by = row.get("managedBy", "") or ""
    return {
        "id": rid,
        "name": row.get("name", ""),
        "type": rtype,
        "kind": row.get("kind", "") or "",
        "location": (row.get("location", "") or "").lower(),
        "resource_group": rg,
        "subscription_id": sub,
        "tags": tags,
        "tag_count": len(tags),
        "sku": row.get("sku", "") or "",
        "tier": row.get("tier", "") or "",
        "size": row.get("size", "") or "",
        "managed_by": managed_by,
        "flags": _hygiene_flags(row, rtype, managed_by, tags),
        "workloads": _resource_workloads(rid, sub, rg, wl_scopes),
    }


def _facets(resources: list[dict[str, Any]], sub_names: dict[str, str], wl_scopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by type / location / subscription / resource group / workload, for filters."""
    types: dict[str, int] = {}
    locations: dict[str, int] = {}
    subs: dict[str, int] = {}
    rgs: dict[str, int] = {}
    wl_counts: dict[str, int] = {}
    unassigned = 0
    for r in resources:
        types[r["type"]] = types.get(r["type"], 0) + 1
        if r["location"]:
            locations[r["location"]] = locations.get(r["location"], 0) + 1
        subs[r["subscription_id"]] = subs.get(r["subscription_id"], 0) + 1
        if r["resource_group"]:
            rgs[r["resource_group"]] = rgs.get(r["resource_group"], 0) + 1
        if r["workloads"]:
            for w in r["workloads"]:
                wl_counts[w["id"]] = wl_counts.get(w["id"], 0) + 1
        else:
            unassigned += 1
    wl_name = {w["id"]: w["name"] for w in wl_scopes}

    def _sorted(d: dict[str, int]) -> list[dict[str, Any]]:
        return [{"key": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "types": _sorted(types),
        "locations": _sorted(locations),
        "subscriptions": [{"key": k, "name": sub_names.get(k, k), "count": v} for k, v in sorted(subs.items(), key=lambda kv: (-kv[1], kv[0]))],
        "resource_groups": _sorted(rgs),
        "workloads": [{"id": wid, "name": wl_name.get(wid, wid), "count": c} for wid, c in sorted(wl_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        "unassigned_count": unassigned,
    }


async def run_id_query(kql: str, connection: dict[str, Any] | None) -> tuple[list[str], str]:
    """Run a validated read-only KQL query and return the matching resource ids (for the
    NL-search KQL path)."""
    rows, err = await _arg(kql, connection, None)
    if err:
        return [], err
    return [r.get("id", "") for r in rows if r.get("id")], ""


async def collect(connection: dict[str, Any] | None) -> dict[str, Any]:
    """Collect the full resource inventory for a connection, attributed to workloads.

    Queries per-subscription (so each query stays under the row/byte caps) and aggregates.
    Returns {resources, facets, summary, subscriptions, errors}."""
    session_dir, _ = await open_sp_session(connection)
    errors: list[str] = []
    truncated_subs: list[str] = []
    resources: list[dict[str, Any]] = []
    sub_names: dict[str, str] = {}
    wl_scopes: list[dict[str, Any]] = []
    try:
        subs = await _subscriptions(connection, session_dir)
        sub_names = {s["id"]: s["name"] for s in subs}
        wl_scopes = await _workload_scopes(connection)
        if not subs:
            errors.append("No subscriptions visible to this connection.")
        for s in subs:
            kql = (
                "resources "
                f"| where subscriptionId =~ '{_esc(s['id'])}' "
                f"| project {_PROJECT} | order by name asc"
            )
            rows, err = await _arg(kql, connection, session_dir)
            if err:
                errors.append(f"{s['name'][:24]}: {err}")
                continue
            if len(rows) >= KQL_MAX_ROWS:
                truncated_subs.append(s["name"] or s["id"])
            resources.extend(_normalize(r, wl_scopes) for r in rows)
    finally:
        close_sp_session(session_dir)

    facets = _facets(resources, sub_names, wl_scopes)
    # Tag coverage + hygiene roll-ups for the overview/insights surfaces.
    tagged = sum(1 for r in resources if r["tag_count"] > 0)
    tag_keys: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    for r in resources:
        for k in r["tags"]:
            tag_keys[k] = tag_keys.get(k, 0) + 1
        for f in r["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    top_tag_keys = [{"key": k, "count": v} for k, v in sorted(tag_keys.items(), key=lambda kv: (-kv[1], kv[0]))[:15]]
    total = len(resources)
    summary = {
        "total_resources": total,
        "type_count": len(facets["types"]),
        "subscription_count": len(facets["subscriptions"]),
        "resource_group_count": len(facets["resource_groups"]),
        "location_count": len(facets["locations"]),
        "workload_count": len(facets["workloads"]),
        "unassigned_count": facets["unassigned_count"],
        "truncated_subscriptions": truncated_subs,
        "tagged_count": tagged,
        "tag_coverage_pct": round(tagged / total * 100) if total else 0,
        "top_tag_keys": top_tag_keys,
        "flag_counts": flag_counts,
    }
    return {
        "resources": resources,
        "facets": facets,
        "summary": summary,
        "errors": errors,
    }
