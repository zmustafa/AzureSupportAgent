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
from app.workloads import discovery, grouping_memory
from app.workloads.summarize import friendly_type, type_breakdown

logger = logging.getLogger("app.workloads.autopilot")

_MAX_RESOURCES = 5000  # whole-estate cap on resources we enumerate / consider
_AI_BATCH = 500        # per-LLM-call resource budget; larger estates map-reduce in batches

VALID_TYPES = ("web_app", "data_pipeline", "ai_ml", "networking", "storage", "identity", "integration", "other")
VALID_ENVS = ("production", "staging", "development", "test", "dr", "shared", "unknown")
VALID_CRIT = ("critical", "high", "medium", "low")


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
                "workload_type": _classify_type(items),
                "environment": _classify_env(rg, items),
                "criticality": "",
                "data_classification": "",
            }
        )
    groups.sort(key=lambda g: -len(g["members"]))
    return groups


# --------------------------------------------------------------- classification heuristics
_TYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("ai_ml", ("cognitiveservices", "machinelearningservices", "search/searchservices")),
    ("data_pipeline", ("datafactory", "synapse", "databricks", "streamanalytics", "eventhub", "kusto")),
    ("networking", ("network/virtualnetworks", "network/azurefirewalls", "network/applicationgateways", "network/loadbalancers", "network/vpngateways")),
    ("storage", ("storage/storageaccounts", "documentdb", "sql/servers", "dbforpostgresql", "dbformysql", "cache/redis")),
    ("identity", ("keyvault/vaults", "managedidentity")),
    ("integration", ("logic/workflows", "servicebus", "apimanagement", "eventgrid")),
    ("web_app", ("web/sites", "web/serverfarms", "app/containerapps", "containerservice/managedclusters")),
]


def _classify_type(members: list[dict[str, Any]]) -> str:
    """Infer a workload type from the mix of resource types (heuristic fallback)."""
    types = [str(m.get("resource_type", "")).lower() for m in members]
    scores: dict[str, int] = {}
    for label, needles in _TYPE_HINTS:
        scores[label] = sum(1 for t in types for n in needles if n in t)
    best = max(scores, key=lambda k: scores[k]) if scores else "other"
    return best if scores.get(best, 0) > 0 else "other"


_ENV_TOKENS: list[tuple[str, tuple[str, ...]]] = [
    ("production", ("prod", "prd", "live")),
    ("staging", ("stag", "stg", "uat", "preprod", "pre-prod")),
    ("development", ("dev", "sandbox", "sbx")),
    ("test", ("test", "qa", "tst")),
    ("dr", ("dr", "failover", "secondary")),
]


def _classify_env(name: str, members: list[dict[str, Any]]) -> str:
    """Infer environment from naming + tags."""
    hay = (name or "").lower()
    for m in members[:30]:
        hay += " " + str(m.get("name", "")).lower()
        tags = m.get("tags") or {}
        if isinstance(tags, dict):
            for k in ("environment", "env", "stage"):
                if tags.get(k):
                    hay += " " + str(tags[k]).lower()
    for env, tokens in _ENV_TOKENS:
        for tok in tokens:
            # token as a delimited word fragment, e.g. "-prod", "prod-", "_prd"
            if tok in hay:
                return env
    return "unknown"


def _norm_class(value: Any, allowed: tuple[str, ...], default: str = "") -> str:
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return v if v in allowed else default


def _evidence_for_group(members: list[dict[str, Any]], signals: dict[str, Any]) -> list[dict[str, str]]:
    """Concrete grouping evidence (shared VNet, private-endpoint links, deployment
    provenance) for the resources in a group — surfaced to build user trust."""
    out: list[dict[str, str]] = []
    member_ids = {str(m.get("id", "")).lower() for m in members if m.get("id")}
    rgs = sorted({m.get("resource_group", "") for m in members if m.get("resource_group")})

    # Shared deployment provenance (azd/app/cost-center tag).
    prov = signals.get("provenance") or {}
    markers: dict[str, int] = {}
    for rid in member_ids:
        mk = prov.get(rid)
        if mk:
            markers[mk] = markers.get(mk, 0) + 1
    for mk, n in sorted(markers.items(), key=lambda kv: -kv[1])[:2]:
        if n >= 2:
            out.append({"kind": "provenance", "detail": f"{n} resources share deployment marker '{mk}'"})

    # Private-endpoint links into this group's resources.
    pe_links = 0
    for link in signals.get("private_endpoints") or []:
        if link.get("target", "") in member_ids or link.get("pe", "") in member_ids:
            pe_links += 1
    if pe_links:
        out.append({"kind": "network", "detail": f"{pe_links} private-endpoint link(s) within this workload"})

    if len(rgs) == 1 and rgs[0]:
        out.append({"kind": "scope", "detail": f"All resources in resource group '{rgs[0]}'"})
    elif len(rgs) > 1:
        out.append({"kind": "scope", "detail": f"Spans {len(rgs)} resource groups"})
    return out


def _catalog_lines(resources: list[dict[str, Any]], signals: dict[str, Any], base: int = 0) -> str:
    """Render a compact resource catalog (indexed from ``base``) with inline signal hints."""
    prov = signals.get("provenance") or {}
    lines = []
    for i, r in enumerate(resources):
        idx = base + i
        tg = _key_tags(r.get("tags", {}))
        tg = f" | tags: {tg}" if tg else ""
        mk = prov.get(str(r.get("id", "")).lower())
        sig = f" | deploy:{mk}" if mk else ""
        lines.append(
            f"[{idx}] {r.get('name', '?')} | {friendly_type(r.get('resource_type'))} "
            f"| rg={r.get('resource_group', '?')}{tg}{sig}"
        )
    return "\n".join(lines)


async def _ai_group_batch(
    resources: list[dict[str, Any]],
    signals: dict[str, Any],
    memory_hint: str,
    base: int = 0,
) -> list[dict[str, Any]] | None:
    """One LLM grouping call over a batch. Returns groups (members are actual resources)
    or None on failure. Indices in the model's reply are GLOBAL (offset by ``base``)."""
    from app.core.ai_prompts import get_full_prompt

    sys = get_full_prompt("workload_discovery_guidance")
    classify_ask = (
        "\n\nFor EACH workload also classify it: \"workload_type\" (one of: web_app, "
        "data_pipeline, ai_ml, networking, storage, identity, integration, other), "
        "\"environment\" (production, staging, development, test, dr, shared, unknown), "
        "\"criticality\" (critical, high, medium, low) and \"data_classification\" "
        "(confidential, internal, public, unknown). Base criticality on environment + "
        "data sensitivity (prod data stores = high/critical; dev = low)."
    )
    parts = [sys, classify_ask]
    if memory_hint:
        parts.append("\n\n" + memory_hint)
    catalog = _catalog_lines(resources, signals, base=base)
    user = f"Resources ({len(resources)}, indices {base}..{base + len(resources) - 1}):\n{catalog}"
    try:
        text = await _complete([{"role": "system", "content": "\n".join(parts)}, {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Autopilot AI grouping failed: %s", exc)
        return None

    obj = _extract_json(text)
    workloads = (obj or {}).get("workloads") if isinstance(obj, dict) else None
    if not isinstance(workloads, list) or not workloads:
        return None

    # Map global indices back to the batch's resources.
    by_index = {base + i: r for i, r in enumerate(resources)}
    groups: list[dict[str, Any]] = []
    for w in workloads:
        if not isinstance(w, dict):
            continue
        members = [by_index[int(idx)] for idx in (w.get("members") or []) if _safe_int(idx) in by_index]
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
                "workload_type": _norm_class(w.get("workload_type"), VALID_TYPES, _classify_type(members)),
                "environment": _norm_class(w.get("environment"), VALID_ENVS, _classify_env(str(w.get("name", "")), members)),
                "criticality": _norm_class(w.get("criticality"), VALID_CRIT, ""),
                "data_classification": _norm_class(w.get("data_classification"), ("confidential", "internal", "public", "unknown"), ""),
            }
        )
    return groups or None


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _merge_cross_batch(groups: list[dict[str, Any]], signals: dict[str, Any]) -> list[dict[str, Any]]:
    """Second-pass merge: combine groups from different batches that clearly belong to the
    same workload (identical normalized name, or the same dominant deployment marker)."""
    prov = signals.get("provenance") or {}

    def _dominant_marker(members: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for m in members:
            mk = prov.get(str(m.get("id", "")).lower())
            if mk:
                counts[mk] = counts.get(mk, 0) + 1
        if not counts:
            return ""
        mk, n = max(counts.items(), key=lambda kv: kv[1])
        return mk if n >= max(2, len(members) // 2) else ""

    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for g in groups:
        marker = _dominant_marker(g["members"])
        key = f"mk::{marker}" if marker else f"nm::{g['name'].strip().lower()}"
        if key in buckets:
            tgt = buckets[key]
            seen = {str(m.get("id", "")).lower() for m in tgt["members"]}
            tgt["members"].extend(m for m in g["members"] if str(m.get("id", "")).lower() not in seen)
            tgt["confidence"] = max(tgt["confidence"], g["confidence"])
            if not tgt.get("criticality"):
                tgt["criticality"] = g.get("criticality", "")
        else:
            buckets[key] = dict(g)
            order.append(key)
    return [buckets[k] for k in order]


async def _ai_group(
    resources: list[dict[str, Any]],
    signals: dict[str, Any],
    memory_hint: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Group resources into workloads with the LLM, map-reducing large estates in batches.
    Returns (groups, used_ai). Falls back to deterministic RG grouping on total failure."""
    if not resources:
        return [], False

    # Small estate: one call.
    if len(resources) <= _AI_BATCH:
        groups = await _ai_group_batch(resources, signals, memory_hint, base=0)
        if groups:
            return groups, True
        return _rg_grouping(resources), False

    # Large estate: batch (tiling by the natural subscription/RG order from enumeration),
    # group each batch, then merge cross-batch duplicates.
    all_groups: list[dict[str, Any]] = []
    any_ai = False
    for start in range(0, len(resources), _AI_BATCH):
        batch = resources[start : start + _AI_BATCH]
        g = await _ai_group_batch(batch, signals, memory_hint, base=start)
        if g:
            any_ai = True
            all_groups.extend(g)
        else:
            all_groups.extend(_rg_grouping(batch))
    if not all_groups:
        return _rg_grouping(resources), False
    merged = _merge_cross_batch(all_groups, signals)
    return merged, any_ai



def _candidate(group: dict[str, Any], signals: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "workload_type": group.get("workload_type", "") or _classify_type(members),
        "environment": group.get("environment", "") or _classify_env(group.get("name", ""), members),
        "criticality": group.get("criticality", ""),
        "data_classification": group.get("data_classification", ""),
        "evidence": _evidence_for_group(members, signals or {}),
    }


async def discover_workloads(
    connection: dict | None, scope_kind: str, scope_id: str, scope_name: str
) -> AsyncIterator[dict[str, Any]]:
    """Stream discovery progress, then yield candidate workloads under the scope."""
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return

    conn_label = connection.get("display_name") or connection.get("tenant_id") or "Azure"
    tenant_id = connection.get("tenant_id", "") or "default"
    connection_id = connection.get("id", "") or ""
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

    # 2. Enumerate ALL resources across the scope, paged up to _MAX_RESOURCES.
    yield _status("enumerating", f"Scanning {len(sub_ids)} subscription(s) for resources…", total=len(sub_ids))
    resources, truncated = await discovery.enumerate_resources_paged(connection, sub_ids, cap=_MAX_RESOURCES)
    yield _status("enumerating", f"Found {len(resources)} resources across the scope.", found=len(resources))

    if not resources:
        yield {"type": "done", "candidates": [], "meta": {"resource_count": 0, "ungrouped": 0, "organized_pct": 100, "used_ai": False, "truncated": truncated}}
        return

    if truncated:
        yield _status("enumerating", f"Reached the {_MAX_RESOURCES}-resource limit; analyzing the first {_MAX_RESOURCES}.", truncated=True)

    # 3. Gather dependency/topology/provenance signals (best-effort) to strengthen grouping.
    yield _status("analyzing", "Examining tags, naming, deployment markers, private-endpoint links and topology…")
    try:
        signals = await discovery.gather_signals(connection, sub_ids)
    except Exception:  # noqa: BLE001
        logger.warning("Signal gathering failed; grouping on names+tags only", exc_info=True)
        signals = {"network": {}, "private_endpoints": [], "provenance": {}}

    # 4. Group with AI (map-reducing large estates), honoring prior user corrections.
    memory_hint = grouping_memory.prompt_hint(tenant_id, connection_id)
    batches = (len(resources) + _AI_BATCH - 1) // _AI_BATCH
    grp_msg = "Identifying distinct workloads with AI…" if batches <= 1 else f"Identifying workloads with AI across {batches} batches…"
    yield _status("grouping", grp_msg)
    groups, used_ai = await _ai_group(resources, signals, memory_hint)
    yield _status(
        "grouping",
        f"Identified {len(groups)} candidate workload(s) "
        f"{'using AI analysis' if used_ai else 'by resource-group boundaries'}.",
        used_ai=used_ai,
    )

    # 5. Emit each candidate (with a brief per-candidate scan summary).
    grouped_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for g in groups:
        cand = _candidate(g, signals)
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
    organized = len(resources) - ungrouped
    organized_pct = round(100 * organized / len(resources)) if resources else 100
    yield {
        "type": "done",
        "candidates": candidates,
        "meta": {
            "resource_count": len(resources),
            "ungrouped": ungrouped,
            "organized_pct": organized_pct,
            "used_ai": used_ai,
            "truncated": truncated,
            "subscriptions": len(sub_ids),
        },
    }

