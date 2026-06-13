"""AI Workload Autopilot: discover candidate workloads under a scope.

Given a scope (management group or subscription), enumerate the resources beneath it and
use the LLM to group them into distinct *workloads* (applications/products) based on tags,
naming conventions, resource-group boundaries and resource co-location. Streams detailed
progress and yields candidate workloads (with reasoning, confidence, and a type
breakdown) for the user to review and save.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

from app.agent.factory import build_provider_for
from app.workloads import discovery
from app.workloads.summarize import friendly_type, type_breakdown

logger = logging.getLogger("app.workloads.autopilot")

_MAX_RESOURCES = 1000  # hard cap on resources we enumerate / consider
_AI_INPUT_CAP = 600  # above this, fall back to deterministic RG grouping


def _status(phase: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": "status", "phase": phase, "message": message, **extra}


def _key_tags(tags: dict[str, Any]) -> str:
    """Surface the tags most useful for grouping (admin-tunable signal list)."""
    if not isinstance(tags, dict):
        return ""
    from app.core.ai_prompts import get_list

    interesting = set(get_list("workload_discovery_tag_signals"))
    picked = []
    for k, v in tags.items():
        if k.lower() in interesting:
            picked.append(f"{k}={v}")
    return ", ".join(picked[:6])


async def _complete(messages: list[dict[str, Any]]) -> str:
    provider = build_provider_for(None, None)
    parts: list[str] = []
    try:
        async for ev in provider.stream(messages, None):
            if ev.type == "token":
                parts.append(ev.text)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return "".join(parts)


def _extract_json(text: str) -> Any:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = min(
            [i for i in (text.find("{"), text.find("[")) if i != -1] or [-1]
        )
        if start != -1:
            candidate = text[start:]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Trim trailing junk after the last closing bracket/brace.
        for end in range(len(candidate) - 1, -1, -1):
            if candidate[end] in "}]":
                try:
                    return json.loads(candidate[: end + 1])
                except json.JSONDecodeError:
                    continue
        return None


def _rg_grouping(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic fallback: group resources by resource group."""
    by_rg: dict[tuple[str, str], list[dict]] = {}
    for r in resources:
        key = (r.get("subscription_id", ""), r.get("resource_group", ""))
        by_rg.setdefault(key, []).append(r)
    groups: list[dict[str, Any]] = []
    for (sub, rg), items in by_rg.items():
        if not rg:
            continue
        groups.append(
            {
                "name": rg,
                "description": f"All resources in resource group '{rg}'.",
                "reasoning": "Grouped by resource group boundary (deterministic fallback).",
                "confidence": 0.5,
                "members": items,
            }
        )
    groups.sort(key=lambda g: -len(g["members"]))
    return groups


async def _ai_group(resources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Group resources into workloads with the LLM. Returns (groups, used_ai)."""
    if len(resources) > _AI_INPUT_CAP:
        return _rg_grouping(resources), False

    lines = []
    for i, r in enumerate(resources):
        tg = _key_tags(r.get("tags", {}))
        tg = f" | tags: {tg}" if tg else ""
        lines.append(
            f"[{i}] {r.get('name', '?')} | {friendly_type(r.get('resource_type'))} "
            f"| rg={r.get('resource_group', '?')}{tg}"
        )
    catalog = "\n".join(lines)
    from app.core.ai_prompts import get_full_prompt

    sys = get_full_prompt("workload_discovery_guidance")
    user = f"Resources ({len(resources)}):\n{catalog}"
    try:
        text = await _complete(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Autopilot AI grouping failed: %s", exc)
        return _rg_grouping(resources), False

    obj = _extract_json(text)
    workloads = (obj or {}).get("workloads") if isinstance(obj, dict) else None
    if not isinstance(workloads, list) or not workloads:
        return _rg_grouping(resources), False

    groups: list[dict[str, Any]] = []
    for w in workloads:
        if not isinstance(w, dict):
            continue
        members_raw = w.get("members") or []
        members = []
        for idx in members_raw:
            try:
                ii = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= ii < len(resources):
                members.append(resources[ii])
        if not members:
            continue
        try:
            conf = float(w.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        groups.append(
            {
                "name": str(w.get("name", "Workload"))[:120] or "Workload",
                "description": str(w.get("description", ""))[:400],
                "reasoning": str(w.get("reasoning", ""))[:800],
                "confidence": max(0.0, min(1.0, conf)),
                "members": members,
            }
        )
    if not groups:
        return _rg_grouping(resources), False
    return groups, True


def _candidate(group: dict[str, Any]) -> dict[str, Any]:
    members: list[dict] = group["members"]
    nodes = [
        {
            "kind": "resource",
            "id": m.get("id", ""),
            "name": m.get("name", ""),
            "resource_type": m.get("resource_type", ""),
            "location": m.get("location", ""),
            "resource_group": m.get("resource_group", ""),
            "subscription_id": m.get("subscription_id", ""),
            "excludes": [],
        }
        for m in members
        if m.get("id")
    ]
    rgs = sorted({m.get("resource_group", "") for m in members if m.get("resource_group")})
    return {
        "name": group["name"],
        "description": group.get("description", ""),
        "reasoning": group.get("reasoning", ""),
        "confidence": group.get("confidence", 0.6),
        "resource_count": len(nodes),
        "types": type_breakdown(members),
        "resource_groups": rgs,
        "nodes": nodes,
    }


async def discover_workloads(
    connection: dict | None, scope_kind: str, scope_id: str, scope_name: str
) -> AsyncIterator[dict[str, Any]]:
    """Stream discovery progress, then yield candidate workloads under the scope."""
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return

    conn_label = connection.get("display_name") or connection.get("tenant_id") or "Azure"
    yield _status("connecting", f"Connecting to {conn_label}…")

    # 1. Resolve the scope into a list of subscriptions to scan.
    sub_ids: list[str] = []
    if scope_kind == "mg":
        yield _status("scope", f"Resolving management group “{scope_name}”…")
        sub_ids = await discovery.subscriptions_under_mg(connection, scope_id)
        if not sub_ids:
            yield {"type": "error", "message": "No subscriptions found under this management group (or no access)."}
            return
        yield _status("scope", f"Found {len(sub_ids)} subscription(s) under the management group.", count=len(sub_ids))
    elif scope_kind == "subscription":
        sub_ids = [scope_id]
        yield _status("scope", f"Scoped to subscription “{scope_name}”.")
    else:
        yield {"type": "error", "message": "Autopilot runs at the subscription or management-group level."}
        return

    # 2. Enumerate resources per subscription (with progress), capped.
    resources: list[dict[str, Any]] = []
    truncated = False
    for i, sub in enumerate(sub_ids, start=1):
        if len(resources) >= _MAX_RESOURCES:
            truncated = True
            break
        yield _status("enumerating", f"Scanning subscription {i}/{len(sub_ids)}…", index=i, total=len(sub_ids))
        remaining = _MAX_RESOURCES - len(resources)
        chunk = await discovery.resources_in_subscriptions(connection, [sub], cap=remaining)
        resources.extend(chunk)
        yield _status(
            "enumerating",
            f"Subscription {i}/{len(sub_ids)}: found {len(chunk)} resources "
            f"({len(resources)} total so far).",
            index=i,
            total=len(sub_ids),
            found=len(resources),
        )

    if not resources:
        yield {"type": "done", "candidates": [], "meta": {"resource_count": 0, "ungrouped": 0, "used_ai": False, "truncated": truncated}}
        return

    if truncated:
        yield _status("enumerating", f"Reached the {_MAX_RESOURCES}-resource limit; analyzing the first {_MAX_RESOURCES}.", truncated=True)

    yield _status("analyzing", f"Collected {len(resources)} resources. Examining tags, naming patterns, resource groups and composition…")

    # 3. Group with AI (or deterministic fallback).
    yield _status("grouping", "Identifying distinct workloads with AI…")
    groups, used_ai = await _ai_group(resources)
    yield _status(
        "grouping",
        f"Identified {len(groups)} candidate workload(s) "
        f"{'using AI analysis' if used_ai else 'by resource-group boundaries'}.",
        used_ai=used_ai,
    )

    # 4. Emit each candidate (with a brief per-candidate scan summary).
    grouped_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for g in groups:
        cand = _candidate(g)
        for n in cand["nodes"]:
            grouped_ids.add(n["id"].lower())
        candidates.append(cand)
        types_str = ", ".join(f"{t['label']} ({t['count']})" for t in cand["types"][:6])
        yield {
            "type": "candidate",
            "candidate": cand,
            "message": f"“{cand['name']}” — {cand['resource_count']} resources: {types_str}",
        }

    ungrouped = sum(1 for r in resources if r.get("id", "").lower() not in grouped_ids)
    yield {
        "type": "done",
        "candidates": candidates,
        "meta": {
            "resource_count": len(resources),
            "ungrouped": ungrouped,
            "used_ai": used_ai,
            "truncated": truncated,
            "subscriptions": len(sub_ids),
        },
    }
