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
from app.workloads import discovery, grouping_memory, sculpt
from app.workloads import seed as seed_mod
from app.workloads import seed_links
from app.workloads.summarize import friendly_type, type_breakdown

logger = logging.getLogger("app.workloads.autopilot")

_MAX_RESOURCES = 5000  # whole-estate cap on resources we enumerate / consider
_AI_BATCH = 500        # per-LLM-call resource budget; larger estates map-reduce in batches

# Short-lived cache of the enumerated estate per (tenant, connection, scope) so the SURVEY
# pre-flight, live cost RE-ESTIMATION, and the final DISCOVER run don't each re-enumerate
# thousands of resources. Survey populates it; estimate + discover reuse it within the TTL.
_SURVEY_TTL = 600.0  # seconds
_survey_cache: dict[str, tuple[float, list[dict[str, Any]], bool]] = {}


def _survey_key(tenant_id: str, connection_id: str, scope_kind: str, scope_id: str) -> str:
    return f"{tenant_id or 'default'}::{connection_id or ''}::{scope_kind}::{scope_id}"


def _survey_cache_get(key: str) -> tuple[list[dict[str, Any]], bool] | None:
    import time

    hit = _survey_cache.get(key)
    if not hit:
        return None
    ts, resources, truncated = hit
    if time.monotonic() - ts > _SURVEY_TTL:
        _survey_cache.pop(key, None)
        return None
    return resources, truncated


def _survey_cache_put(key: str, resources: list[dict[str, Any]], truncated: bool) -> None:
    import time

    _survey_cache[key] = (time.monotonic(), resources, truncated)
    # Bound the cache: keep only the few most-recent scopes.
    if len(_survey_cache) > 8:
        oldest = sorted(_survey_cache.items(), key=lambda kv: kv[1][0])[:-8]
        for k, _ in oldest:
            _survey_cache.pop(k, None)


# Sculpt presets — each maps to a coherent set of controls. The UI's Fast/Balanced/Thorough
# buttons just apply one of these (the user can still override any individual control after).
PRESETS: dict[str, dict[str, Any]] = {
    "fast": {"granularity": "resource_group", "exclude_noise": True, "exclude_system_rgs": True, "confidence_floor": 0.55},
    "balanced": {"granularity": "resource", "exclude_noise": True, "exclude_system_rgs": True, "confidence_floor": 0.0},
    "thorough": {"granularity": "resource", "exclude_noise": False, "exclude_system_rgs": False, "confidence_floor": 0.0},
}


async def _resolve_subs(connection: dict | None, scope_kind: str, scope_id: str) -> tuple[list[str], str]:
    """Resolve a scope into the list of subscription ids to scan (no status streaming)."""
    if scope_kind == "mg":
        subs = await discovery.subscriptions_under_mg(connection, scope_id)
        if not subs:
            return [], "No subscriptions found under this management group (or no access)."
        return subs, ""
    if scope_kind == "subscription":
        return [scope_id], ""
    if scope_kind == "resource":
        # Seed mode: the subscription is embedded in the seed's ARM id.
        sub = seed_mod.arm_id_parts(scope_id).get("subscription_id", "")
        if not sub:
            return [], "That doesn't look like an Azure resource id."
        return [sub], ""
    return [], "Autopilot runs at the subscription or management-group level."


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


def _sub_grouping(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic template: one workload per subscription."""
    by_sub: dict[str, list[dict]] = {}
    for r in resources:
        by_sub.setdefault(r.get("subscription_id", "") or "unknown", []).append(r)
    groups: list[dict[str, Any]] = []
    for sub, items in by_sub.items():
        groups.append({
            "name": f"Subscription {sub[:8]}" if sub != "unknown" else "Ungrouped",
            "description": f"All resources in subscription {sub}.",
            "reasoning": "Grouped by subscription boundary (template).",
            "confidence": 0.5, "members": items,
            "workload_type": _classify_type(items), "environment": _classify_env("", items),
            "criticality": "", "data_classification": "",
        })
    groups.sort(key=lambda g: -len(g["members"]))
    return groups


def _tag_grouping(resources: list[dict[str, Any]], *, tag_key: str = "") -> list[dict[str, Any]]:
    """Deterministic template: group by a tag value. When ``tag_key`` is empty, the most common
    grouping-signal tag key present across the estate is used."""
    from app.core.ai_prompts import get_list

    signal_keys = {k.lower() for k in get_list("workload_discovery_tag_signals")}

    def _tag_of(r: dict[str, Any]) -> tuple[str, str]:
        tags = r.get("tags") or {}
        if not isinstance(tags, dict):
            return "", ""
        if tag_key:
            for k, v in tags.items():
                if k.lower() == tag_key.lower():
                    return k, str(v)
            return "", ""
        # Auto: pick the first signal-list tag the resource carries.
        for k, v in tags.items():
            if k.lower() in signal_keys:
                return k, str(v)
        return "", ""

    by_val: dict[str, list[dict]] = {}
    untagged: list[dict] = []
    used_key = tag_key
    for r in resources:
        k, v = _tag_of(r)
        if not v:
            untagged.append(r)
            continue
        used_key = used_key or k
        by_val.setdefault(v, []).append(r)
    groups: list[dict[str, Any]] = []
    for val, items in by_val.items():
        groups.append({
            "name": val,
            "description": f"Resources tagged {used_key}={val}.",
            "reasoning": f"Grouped by tag '{used_key}' (template).",
            "confidence": 0.6, "members": items,
            "workload_type": _classify_type(items), "environment": _classify_env(val, items),
            "criticality": "", "data_classification": "",
        })
    if untagged:
        groups.append({
            "name": "Untagged", "description": f"Resources with no '{used_key or 'grouping'}' tag.",
            "reasoning": "Resources lacking the grouping tag (template).", "confidence": 0.3,
            "members": untagged, "workload_type": _classify_type(untagged),
            "environment": "unknown", "criticality": "", "data_classification": "",
        })
    groups.sort(key=lambda g: -len(g["members"]))
    return groups
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


def _classify_ask() -> str:
    return (
        "\n\nFor EACH workload also classify it: \"workload_type\" (one of: web_app, "
        "data_pipeline, ai_ml, networking, storage, identity, integration, other), "
        "\"environment\" (production, staging, development, test, dr, shared, unknown), "
        "\"criticality\" (critical, high, medium, low) and \"data_classification\" "
        "(confidential, internal, public, unknown). Base criticality on environment + "
        "data sensitivity (prod data stores = high/critical; dev = low)."
    )


def _naming_clause(naming_hint: str) -> str:
    """A prompt fragment teaching the model the estate's naming convention so it can parse
    workload identity from resource names."""
    if not naming_hint:
        return ""
    return (
        f"\n\nNaming convention: resource names in this estate generally follow the pattern "
        f"`{naming_hint}`. Use the workload/app token in the name as a STRONG grouping signal — "
        f"resources sharing the same app token almost always belong to the same workload, even "
        f"across resource groups."
    )


async def _ai_group_batch(
    resources: list[dict[str, Any]],
    signals: dict[str, Any],
    memory_hint: str,
    base: int = 0,
    naming_hint: str = "",
) -> list[dict[str, Any]] | None:
    """One LLM grouping call over a batch. Returns groups (members are actual resources)
    or None on failure. Indices in the model's reply are GLOBAL (offset by ``base``)."""
    from app.core.ai_prompts import get_full_prompt

    sys = get_full_prompt("workload_discovery_guidance")
    parts = [sys, _classify_ask()]
    if naming_hint:
        parts.append(_naming_clause(naming_hint))
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
    *,
    naming_hint: str = "",
    max_calls: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Group resources into workloads with the LLM, map-reducing large estates in batches.
    Returns (groups, used_ai). Falls back to deterministic RG grouping on total failure.
    ``max_calls`` caps the number of LLM calls — the remainder falls back to RG grouping."""
    if not resources:
        return [], False

    # Small estate: one call.
    if len(resources) <= _AI_BATCH:
        groups = await _ai_group_batch(resources, signals, memory_hint, base=0, naming_hint=naming_hint)
        if groups:
            return groups, True
        return _rg_grouping(resources), False

    # Large estate: batch (tiling by the natural subscription/RG order from enumeration),
    # group each batch, then merge cross-batch duplicates.
    all_groups: list[dict[str, Any]] = []
    any_ai = False
    calls = 0
    for start in range(0, len(resources), _AI_BATCH):
        batch = resources[start : start + _AI_BATCH]
        if max_calls and calls >= max_calls:
            # Budget exhausted — deterministically group the remainder (no more AI spend).
            all_groups.extend(_rg_grouping(resources[start:]))
            break
        g = await _ai_group_batch(batch, signals, memory_hint, base=start, naming_hint=naming_hint)
        calls += 1
        if g:
            any_ai = True
            all_groups.extend(g)
        else:
            all_groups.extend(_rg_grouping(batch))
    if not all_groups:
        return _rg_grouping(resources), False
    merged = _merge_cross_batch(all_groups, signals)
    return merged, any_ai


# --------------------------------------------------------------- aggregated grouping modes
def _dominant_tags(members: list[dict[str, Any]], top: int = 3) -> str:
    """The most common grouping-signal tag values across a unit's members."""
    from app.core.ai_prompts import get_list

    interesting = {k.lower() for k in get_list("workload_discovery_tag_signals")}
    counts: dict[str, int] = {}
    for m in members:
        tags = m.get("tags") or {}
        if isinstance(tags, dict):
            for k, v in tags.items():
                if k.lower() in interesting and str(v).strip():
                    counts[f"{k}={v}"] = counts.get(f"{k}={v}", 0) + 1
    return ", ".join(k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top])


def _unit_summary(members: list[dict[str, Any]], label: str) -> str:
    """A one-line aggregate summary of a unit (resource group or naming cluster)."""
    hist = ", ".join(f"{t['label']}({t['count']})" for t in type_breakdown(members)[:5])
    tags = _dominant_tags(members)
    tag_str = f" | tags: {tags}" if tags else ""
    return f"{label} | {len(members)} resources | types: {hist}{tag_str}"


def _build_units(resources: list[dict[str, Any]], granularity: str) -> list[dict[str, Any]]:
    """Aggregate resources into coarse units for cheaper grouping.

    ``resource_group`` → one unit per (subscription, resource group). ``sample`` → one unit
    per (subscription, naming-prefix) cluster, so a workload spanning many RGs but sharing a
    name prefix collapses to a single representative line."""
    units: dict[Any, dict[str, Any]] = {}
    naming = sculpt.detect_naming_convention(resources)
    delim = naming.get("delimiter") or "-"
    for r in resources:
        sub = str(r.get("subscription_id", ""))
        if granularity == "sample":
            name = str(r.get("name", ""))
            prefix = name.split(delim)[0].lower() if delim in name else (name.lower() or "misc")
            key = (sub, prefix)
            label = f"name-prefix '{prefix}' sub={sub[:8]}"
        else:  # resource_group
            rg = str(r.get("resource_group", ""))
            key = (sub, rg)
            label = f"rg={rg or '(none)'} sub={sub[:8]}"
        u = units.setdefault(key, {"members": [], "label": label})
        u["members"].append(r)
    out = list(units.values())
    out.sort(key=lambda u: -len(u["members"]))
    return out


def _unit_fallback_group(unit: dict[str, Any]) -> dict[str, Any]:
    members = unit["members"]
    return {
        "name": unit["label"].split("|")[0].strip() or "Workload",
        "description": f"Resources in {unit['label']}.",
        "reasoning": "Grouped by aggregate boundary (AI budget exhausted — deterministic).",
        "confidence": 0.5,
        "members": members,
        "workload_type": _classify_type(members),
        "environment": _classify_env(unit["label"], members),
        "criticality": "",
        "data_classification": "",
    }


async def _ai_group_units_batch(
    units: list[dict[str, Any]],
    signals: dict[str, Any],
    memory_hint: str,
    naming_hint: str,
    unit_noun: str,
    base: int = 0,
) -> list[dict[str, Any]] | None:
    """One LLM call that groups aggregate UNITS (each an index over many resources) into
    workloads. The model returns unit indices per workload; we expand them to resources."""
    from app.core.ai_prompts import get_full_prompt

    sys = get_full_prompt("workload_discovery_guidance")
    parts = [sys, _classify_ask()]
    if naming_hint:
        parts.append(_naming_clause(naming_hint))
    if memory_hint:
        parts.append("\n\n" + memory_hint)
    lines = "\n".join(f"[{base + i}] {u['label']} | {_unit_summary(u['members'], '')[2:]}" for i, u in enumerate(units))
    user = (
        f"Each line below is a {unit_noun} — an AGGREGATE of related resources, not a single "
        f"resource. Group the {unit_noun}s into distinct workloads, citing each workload's "
        f"member {unit_noun}s by their index. {len(units)} {unit_noun}s, indices "
        f"{base}..{base + len(units) - 1}:\n{lines}"
    )
    try:
        text = await _complete([{"role": "system", "content": "\n".join(parts)}, {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Autopilot aggregated grouping failed: %s", exc)
        return None
    obj = _extract_json(text)
    workloads = (obj or {}).get("workloads") if isinstance(obj, dict) else None
    if not isinstance(workloads, list) or not workloads:
        return None
    by_index = {base + i: u for i, u in enumerate(units)}
    groups: list[dict[str, Any]] = []
    for w in workloads:
        if not isinstance(w, dict):
            continue
        members: list[dict[str, Any]] = []
        for idx in w.get("members") or []:
            u = by_index.get(_safe_int(idx))
            if u:
                members.extend(u["members"])
        if not members:
            continue
        try:
            conf = float(w.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        groups.append({
            "name": str(w.get("name", "Workload"))[:120] or "Workload",
            "description": str(w.get("description", ""))[:400],
            "reasoning": str(w.get("reasoning", ""))[:800],
            "confidence": max(0.0, min(1.0, conf)),
            "members": members,
            "workload_type": _norm_class(w.get("workload_type"), VALID_TYPES, _classify_type(members)),
            "environment": _norm_class(w.get("environment"), VALID_ENVS, _classify_env(str(w.get("name", "")), members)),
            "criticality": _norm_class(w.get("criticality"), VALID_CRIT, ""),
            "data_classification": _norm_class(w.get("data_classification"), ("confidential", "internal", "public", "unknown"), ""),
        })
    return groups or None


async def _ai_group_aggregated(
    resources: list[dict[str, Any]],
    signals: dict[str, Any],
    memory_hint: str,
    *,
    granularity: str,
    naming_hint: str = "",
    max_calls: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Group an estate by coarse UNITS (resource groups or naming clusters) — far fewer LLM
    calls than per-resource grouping on a large estate. Returns (groups, used_ai)."""
    units = _build_units(resources, granularity)
    if not units:
        return [], False
    batch = sculpt.RG_BATCH if granularity == "resource_group" else sculpt.SAMPLE_BATCH
    noun = "resource group" if granularity == "resource_group" else "name cluster"
    all_groups: list[dict[str, Any]] = []
    any_ai = False
    calls = 0
    for start in range(0, len(units), batch):
        chunk = units[start : start + batch]
        if max_calls and calls >= max_calls:
            all_groups.extend(_unit_fallback_group(u) for u in chunk)
            continue
        g = await _ai_group_units_batch(chunk, signals, memory_hint, naming_hint, noun, base=start)
        calls += 1
        if g:
            any_ai = True
            all_groups.extend(g)
        else:
            all_groups.extend(_unit_fallback_group(u) for u in chunk)
    if not all_groups:
        return _rg_grouping(resources), False
    return _merge_cross_batch(all_groups, signals), any_ai




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


def _candidate_size_meta(
    candidates: list[dict[str, Any]], min_candidate_resources: int
) -> dict[str, int]:
    """Describe the review-time minimum without dropping discovery evidence.

    Candidate size is only known after AI grouping and child-resource reattachment. The
    threshold therefore belongs to review policy: every candidate still streams to the client,
    where the operator can lower the threshold or include one undersized workload explicitly
    without paying for another discovery run.
    """
    minimum = max(1, min(5_000, int(min_candidate_resources or 1)))
    undersized = [c for c in candidates if int(c.get("resource_count", 0) or 0) < minimum]
    return {
        "min_candidate_resources": minimum,
        "below_minimum_workloads": len(undersized),
        "below_minimum_resources": sum(
            int(c.get("resource_count", 0) or 0) for c in undersized
        ),
    }


def _filter_config_from(kwargs: dict[str, Any]) -> sculpt.FilterConfig:
    """Build a sculpt FilterConfig from the loose discovery kwargs."""
    return sculpt.FilterConfig(
        exclude_noise=bool(kwargs.get("exclude_noise", True)),
        exclude_system_rgs=bool(kwargs.get("exclude_system_rgs", True)),
        rg_globs=kwargs.get("rg_globs") or None,
        include_types=kwargs.get("include_types") or None,
        exclude_types=kwargs.get("exclude_types") or None,
        environments=kwargs.get("environments") or None,
        regions=kwargs.get("regions") or None,
        subscriptions=kwargs.get("subscriptions") or None,
        name_contains=kwargs.get("name_contains") or "",
    )


def _apply_preset(preset: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Overlay a preset's defaults UNDER any explicit caller value (explicit wins)."""
    base = dict(PRESETS.get(preset or "", {}))
    base.update({k: v for k, v in kwargs.items() if v is not None})
    return base


async def discover_workloads(
    connection: dict | None, scope_kind: str, scope_id: str, scope_name: str,
    *, strategy: str = "ai", mode: str = "full", tag_key: str = "",
    preset: str = "", granularity: str = "resource",
    exclude_noise: bool = True, exclude_system_rgs: bool = True,
    rg_globs: list[str] | None = None,
    tag_seed_keys: list[str] | None = None,
    include_types: list[str] | None = None, exclude_types: list[str] | None = None,
    environments: list[str] | None = None, regions: list[str] | None = None,
    subscriptions: list[str] | None = None, name_contains: str = "",
    confidence_floor: float = 0.0, max_ai_calls: int = 0, naming_hint: str = "",
    min_candidate_resources: int = 1,
) -> AsyncIterator[dict[str, Any]]:
    """Stream discovery progress, then yield candidate workloads under the scope.

    ``strategy``: ``ai`` (LLM grouping, default) | ``resource_group`` | ``subscription`` | ``tag``
    (deterministic templates — fast, no LLM). ``mode``: ``full`` | ``delta`` (skip resources
    already in a saved workload). The remaining params are the Scope-Sculptor controls:

    * ``preset`` — fast | balanced | thorough (overlaid UNDER explicit controls).
    * ``granularity`` — resource | resource_group | sample (coarser = fewer/cheaper AI calls).
    * ``exclude_noise`` / ``exclude_system_rgs`` / ``rg_globs`` — Tier-1 input reduction.
    * ``tag_seed_keys`` — deterministically pre-bucket by authoritative tags; only the
      remainder reaches the LLM.
    * ``include_types`` / ``exclude_types`` / ``environments`` / ``regions`` /
      ``subscriptions`` / ``name_contains`` — scoping filters.
    * ``confidence_floor`` — drop candidates the model isn't confident about.
    * ``max_ai_calls`` — hard budget; the remainder falls back to deterministic grouping.
    * ``naming_hint`` — the estate's naming convention, injected into the grouping prompt.
        * ``min_candidate_resources`` — review-time size policy, reported in metadata only. It
            never suppresses the stream because the operator can override it without another AI run.
    """
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return

    # The frontend applies a preset to the individual controls before calling, so the params
    # arrive fully resolved. ``preset`` is recorded in meta for traceability.
    granularity = granularity or "resource"
    confidence_floor = float(confidence_floor or 0.0)
    min_candidate_resources = max(1, min(5_000, int(min_candidate_resources or 1)))

    conn_label = connection.get("display_name") or connection.get("tenant_id") or "Azure"
    tenant_id = connection.get("tenant_id", "") or "default"
    connection_id = connection.get("id", "") or ""
    yield _status("connecting", f"Connecting to {conn_label}…")

    # 1. Resolve the scope into a list of subscriptions to scan.
    if scope_kind == "mg":
        yield _status("scope", f"Resolving management group “{scope_name}”…")
    sub_ids, scope_err = await _resolve_subs(connection, scope_kind, scope_id)
    if scope_err:
        yield {"type": "error", "message": scope_err}
        return
    if scope_kind == "mg":
        yield _status("scope", f"Found {len(sub_ids)} subscription(s) under the management group.", count=len(sub_ids))
    else:
        yield _status("scope", f"Scoped to subscription “{scope_name}”.")

    # 2. Enumerate the estate — reuse the survey cache if it's warm (avoids a re-enumeration).
    cache_key = _survey_key(tenant_id, connection_id, scope_kind, scope_id)
    cached = _survey_cache_get(cache_key)
    if cached is not None:
        resources, truncated = cached
        resources = list(resources)
        yield _status("enumerating", f"Using the surveyed estate ({len(resources)} resources).", found=len(resources), cached=True)
    else:
        yield _status("enumerating", f"Scanning {len(sub_ids)} subscription(s) for resources…", total=len(sub_ids))
        resources, truncated = await discovery.enumerate_resources_paged(connection, sub_ids, cap=_MAX_RESOURCES)
        _survey_cache_put(cache_key, list(resources), truncated)
        yield _status("enumerating", f"Found {len(resources)} resources across the scope.", found=len(resources))

    total_enumerated = len(resources)
    if not resources:
        yield {
            "type": "done",
            "candidates": [],
            "meta": {
                "resource_count": 0,
                "ungrouped": 0,
                "organized_pct": 100,
                "used_ai": False,
                "truncated": truncated,
                **_candidate_size_meta([], min_candidate_resources),
            },
        }
        return
    if truncated:
        yield _status("enumerating", f"Reached the {_MAX_RESOURCES}-resource limit; analyzing the first {_MAX_RESOURCES}.", truncated=True)

    # 2b. Delta mode: skip resources already organized into a saved workload.
    if mode == "delta":
        from app.workloads.registry import list_workloads

        owned: set[str] = set()
        for wl in list_workloads():
            for n in wl.get("nodes", []) or []:
                if n.get("kind") == "resource" and n.get("id"):
                    owned.add(n["id"].lower())
        if owned:
            before = len(resources)
            resources = [r for r in resources if (r.get("id", "") or "").lower() not in owned]
            skipped = before - len(resources)
            yield _status("enumerating", f"Delta mode: skipped {skipped} resource(s) already in a workload; {len(resources)} unorganized remain.", skipped=skipped, delta=True)
            if not resources:
                yield {
                    "type": "done",
                    "candidates": [],
                    "meta": {
                        "resource_count": 0,
                        "ungrouped": 0,
                        "organized_pct": 100,
                        "used_ai": False,
                        "truncated": truncated,
                        "delta": True,
                        **_candidate_size_meta([], min_candidate_resources),
                    },
                }
                return

    # 2c. SCULPT — apply the input-reduction + scoping filters (Tier 1). The noise/system-RG
    # removals become ORPHANS that re-attach to their parent workload after grouping; the
    # scoped-out ones are dropped.
    cfg = _filter_config_from({
        "exclude_noise": exclude_noise, "exclude_system_rgs": exclude_system_rgs,
        "rg_globs": rg_globs, "include_types": include_types, "exclude_types": exclude_types,
        "environments": environments, "regions": regions, "subscriptions": subscriptions,
        "name_contains": name_contains,
    })
    kept, orphans, reasons = sculpt.apply_filters(resources, cfg)
    removed_total = len(resources) - len(kept)
    if removed_total:
        bits = [f"{v} {k.replace('_', ' ')}" for k, v in reasons.items() if v]
        yield _status("sculpting", f"Sculpted the estate: {len(kept)} resources to group ({removed_total} filtered — {', '.join(bits)}).", kept=len(kept), removed=removed_total, reasons=reasons)
    if not kept:
        yield {
            "type": "done",
            "candidates": [],
            "meta": {
                "resource_count": total_enumerated,
                "ungrouped": total_enumerated,
                "organized_pct": 0,
                "used_ai": False,
                "truncated": truncated,
                **_candidate_size_meta([], min_candidate_resources),
            },
        }
        return

    # 2d. TAG-SEED — deterministically pre-bucket well-tagged resources; only the remainder
    # needs the LLM.
    seeded_groups: list[dict[str, Any]] = []
    remainder = kept
    if tag_seed_keys:
        seeded_groups, remainder = sculpt.tag_seed_partition(kept, tag_seed_keys)
        if seeded_groups:
            yield _status("sculpting", f"Tag-seeded {len(kept) - len(remainder)} resource(s) into {len(seeded_groups)} workload(s) by {', '.join(tag_seed_keys)}; {len(remainder)} remain for AI.", seeded=len(seeded_groups))

    # Priority order so prod / largest groups stream first (Tier 4).
    remainder = sculpt.priority_sort(remainder)

    # 3. Signals (best-effort) to strengthen grouping + cite evidence.
    yield _status("analyzing", "Examining tags, naming, deployment markers, private-endpoint links and topology…")
    try:
        signals = await discovery.gather_signals(connection, sub_ids)
    except Exception:  # noqa: BLE001
        logger.warning("Signal gathering failed; grouping on names+tags only", exc_info=True)
        signals = {"network": {}, "private_endpoints": [], "provenance": {}}

    # Auto-detect the naming convention if the caller didn't pass one.
    if not naming_hint:
        detected = sculpt.detect_naming_convention(remainder)
        if detected.get("pattern") and detected.get("confidence", 0) >= 0.5:
            naming_hint = detected["pattern"]

    # 4. Group the remainder — deterministic template, or an AI pass at the chosen granularity.
    ai_groups: list[dict[str, Any]] = []
    used_ai = False
    if strategy in ("resource_group", "subscription", "tag"):
        tmpl = {"resource_group": "resource group", "subscription": "subscription", "tag": f"tag '{tag_key or 'auto'}'"}[strategy]
        yield _status("grouping", f"Grouping by {tmpl} (template — no AI)…")
        if strategy == "resource_group":
            ai_groups = _rg_grouping(remainder)
        elif strategy == "subscription":
            ai_groups = _sub_grouping(remainder)
        else:
            ai_groups = _tag_grouping(remainder, tag_key=tag_key)
        yield _status("grouping", f"Identified {len(ai_groups)} candidate workload(s) by {tmpl}.", used_ai=False)
    else:
        memory_hint = grouping_memory.prompt_hint(tenant_id, connection_id)
        if granularity in ("resource_group", "sample"):
            noun = "resource groups" if granularity == "resource_group" else "name clusters"
            units = _build_units(remainder, granularity)
            yield _status("grouping", f"Identifying workloads with AI over {len(units)} {noun} (coarse {granularity} pass)…")
            ai_groups, used_ai = await _ai_group_aggregated(remainder, signals, memory_hint, granularity=granularity, naming_hint=naming_hint, max_calls=max_ai_calls)
        else:
            batches = (len(remainder) + _AI_BATCH - 1) // _AI_BATCH
            if max_ai_calls:
                batches = min(batches, max_ai_calls)
            grp_msg = "Identifying distinct workloads with AI…" if batches <= 1 else f"Identifying workloads with AI across {batches} batches…"
            yield _status("grouping", grp_msg)
            ai_groups, used_ai = await _ai_group(remainder, signals, memory_hint, naming_hint=naming_hint, max_calls=max_ai_calls)
        yield _status(
            "grouping",
            f"Identified {len(ai_groups)} candidate workload(s) "
            f"{'using AI analysis' if used_ai else 'by resource-group boundaries'}.",
            used_ai=used_ai,
        )

    groups = seeded_groups + ai_groups

    # 4b. Re-attach the filtered child resources (noise/system-RG) to their parent workload.
    attached = sculpt.reattach_orphans(groups, orphans)
    if attached:
        yield _status("grouping", f"Re-attached {attached} child resource(s) (disks, NICs, alerts) to their parent workload.", attached=attached)

    # 5. Emit candidates — applying the confidence floor (Tier 3).
    grouped_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    below_floor = 0
    for g in groups:
        if confidence_floor and float(g.get("confidence", 0.6)) < confidence_floor:
            below_floor += 1
            continue
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
    if below_floor:
        yield _status("grouping", f"Hid {below_floor} low-confidence candidate(s) below the {int(confidence_floor * 100)}% confidence floor.", below_floor=below_floor)

    # Coverage is measured against the SCULPTED (kept) estate the user asked us to organize.
    considered_ids = {str(r.get("id", "")).lower() for r in kept}
    ungrouped = sum(1 for rid in considered_ids if rid not in grouped_ids)
    organized = len(considered_ids) - ungrouped
    organized_pct = round(100 * organized / len(considered_ids)) if considered_ids else 100
    yield {
        "type": "done",
        "candidates": candidates,
        "meta": {
            "resource_count": total_enumerated,
            "considered": len(kept),
            "filtered": removed_total,
            "ungrouped": ungrouped,
            "organized_pct": organized_pct,
            "used_ai": used_ai,
            "truncated": truncated,
            "subscriptions": len(sub_ids),
            "granularity": granularity,
            "tag_seeded_workloads": len(seeded_groups),
            "reattached": attached,
            "below_floor": below_floor,
            "naming_hint": naming_hint,
            "preset": preset,
            **_candidate_size_meta(candidates, min_candidate_resources),
        },
    }


async def survey_estate(
    connection: dict | None, scope_kind: str, scope_id: str, scope_name: str,
) -> AsyncIterator[dict[str, Any]]:
    """Pre-flight SURVEY (Tier 1/3): enumerate the estate and emit facet tallies + a default
    cost estimate — NO LLM. Caches the enumeration so the subsequent discover run (and live
    re-estimates) reuse it. Streams ``status`` events then a single ``survey`` event."""
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return
    conn_label = connection.get("display_name") or connection.get("tenant_id") or "Azure"
    tenant_id = connection.get("tenant_id", "") or "default"
    connection_id = connection.get("id", "") or ""
    yield _status("connecting", f"Connecting to {conn_label}…")

    sub_ids, scope_err = await _resolve_subs(connection, scope_kind, scope_id)
    if scope_err:
        yield {"type": "error", "message": scope_err}
        return
    yield _status("scope", f"Surveying {len(sub_ids)} subscription(s)…", count=len(sub_ids))
    yield _status("enumerating", "Enumerating resources (read-only, no AI)…")
    resources, truncated = await discovery.enumerate_resources_paged(connection, sub_ids, cap=_MAX_RESOURCES)
    _survey_cache_put(_survey_key(tenant_id, connection_id, scope_kind, scope_id), list(resources), truncated)
    yield _status("enumerating", f"Found {len(resources)} resources.", found=len(resources))

    facets = sculpt.compute_facets(resources)
    # Default-config preview (noise + system RGs filtered) + a resource-group-granularity estimate.
    kept, _orphans, reasons = sculpt.apply_filters(resources, sculpt.FilterConfig())
    units = _build_units(kept, "resource_group") if kept else []
    estimate = sculpt.estimate_cost(len(kept), granularity="resource_group", n_resource_groups=len(units))
    yield {
        "type": "survey",
        "facets": facets,
        "filter_preview": {"kept": len(kept), "removed": len(resources) - len(kept), "reasons": reasons},
        "estimate": estimate,
        "meta": {
            "resource_count": len(resources),
            "truncated": truncated,
            "subscriptions": len(sub_ids),
            "scope_name": scope_name,
        },
    }


def compute_estimate(
    tenant_id: str, connection_id: str, scope_kind: str, scope_id: str, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-estimate cost + filter preview for a sculpt CONFIG against the cached survey estate
    (live, no Azure call). Returns ``None`` when the survey cache has expired (client should
    re-survey)."""
    cached = _survey_cache_get(_survey_key(tenant_id, connection_id, scope_kind, scope_id))
    if cached is None:
        return None
    resources, truncated = cached
    merged = _apply_preset(str(config.get("preset", "")), {
        "granularity": config.get("granularity"),
        "exclude_noise": config.get("exclude_noise"),
        "exclude_system_rgs": config.get("exclude_system_rgs"),
        "confidence_floor": config.get("confidence_floor"),
    })
    granularity = merged.get("granularity") or config.get("granularity") or "resource"
    cfg = _filter_config_from({
        "exclude_noise": merged.get("exclude_noise", config.get("exclude_noise", True)),
        "exclude_system_rgs": merged.get("exclude_system_rgs", config.get("exclude_system_rgs", True)),
        "rg_globs": config.get("rg_globs"),
        "include_types": config.get("include_types"), "exclude_types": config.get("exclude_types"),
        "environments": config.get("environments"), "regions": config.get("regions"),
        "subscriptions": config.get("subscriptions"), "name_contains": config.get("name_contains", ""),
    })
    kept, _orphans, reasons = sculpt.apply_filters(resources, cfg)
    seeded = 0
    remainder = kept
    if config.get("tag_seed_keys"):
        seeded_groups, remainder = sculpt.tag_seed_partition(kept, list(config["tag_seed_keys"]))
        seeded = len(kept) - len(remainder)
    n_rgs = len(_build_units(remainder, "resource_group")) if remainder else 0
    estimate = sculpt.estimate_cost(
        len(remainder), granularity=granularity, n_resource_groups=n_rgs,
        tag_seeded=0, max_ai_calls=int(config.get("max_ai_calls", 0) or 0),
    )
    estimate["tag_seeded"] = seeded
    return {
        "estimate": estimate,
        "filter_preview": {"kept": len(kept), "removed": len(resources) - len(kept), "reasons": reasons, "tag_seeded": seeded},
        "truncated": truncated,
    }


# ============================================================ seed-resource workload trace
# Bottom-up discovery: start from ONE resource id and follow typed relationships outward
# until the workload boundary is reached. Purely additive — the top-down scope flow above is
# untouched; seed mode short-circuits before enumeration/sculpt/grouping ever runs.

_TRACE_TTL = 900.0  # seconds
_trace_cache: dict[str, tuple[float, dict[str, Any], dict[str, dict[str, Any]]]] = {}


def _trace_key(tenant_id: str, connection_id: str, seed_id: str, max_hops: int) -> str:
    return f"{tenant_id or 'default'}::{connection_id or ''}::{(seed_id or '').lower()}::{max_hops}"


def _trace_cache_get(key: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]] | None:
    import time

    hit = _trace_cache.get(key)
    if not hit:
        return None
    ts, raw, resources = hit
    if time.monotonic() - ts > _TRACE_TTL:
        _trace_cache.pop(key, None)
        return None
    return raw, resources


def _trace_cache_put(key: str, raw: dict[str, Any], resources: dict[str, dict[str, Any]]) -> None:
    import time

    _trace_cache[key] = (time.monotonic(), raw, resources)
    if len(_trace_cache) > 8:
        oldest = sorted(_trace_cache.items(), key=lambda kv: kv[1][0])[:-8]
        for k, _ in oldest:
            _trace_cache.pop(k, None)


def _seed_evidence(seedres: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Aggregate the per-node trace evidence into workload-level evidence chips."""
    from collections import Counter

    out: list[dict[str, str]] = [{
        "kind": "seed",
        "detail": f"Traced from “{seedres.get('name', '?')}” ({friendly_type(seedres.get('resource_type'))})",
    }]
    counts: Counter[str] = Counter()
    for n in nodes:
        if n.get("is_seed"):
            continue
        for ev in n.get("evidence") or []:
            counts[ev["kind"]] += 1
    for kind, n in counts.most_common(4):
        label = seed_mod.EDGE_LABELS.get(kind, kind).lower()
        out.append({"kind": kind, "detail": f"{n} resource(s) linked by {label}"})
    hops = max((int(n.get("hop", 0)) for n in nodes), default=0)
    if hops:
        out.append({"kind": "scope", "detail": f"Reached within {hops} relationship hop(s) of the seed"})
    return out


async def _ai_adjudicate_seed(
    seedres: dict[str, Any],
    members: list[dict[str, Any]],
    borderline: list[dict[str, Any]],
    shared: list[dict[str, Any]],
    memory_hint: str = "",
) -> dict[str, Any] | None:
    """ONE LLM call: name + classify the traced workload and rule on the borderline members.

    Grouping has already been decided by the trace, so the model's job is narrow: confirm or
    reject the resources the crawl wasn't sure about, and label the result."""
    def _lines(items: list[dict[str, Any]], base: int) -> str:
        rows = []
        for i, n in enumerate(items):
            ev = "; ".join(e["detail"] for e in (n.get("evidence") or [])[:2])
            rows.append(
                f"[{base + i}] {n.get('name', '?')} | {friendly_type(n.get('resource_type'))} "
                f"| rg={n.get('resource_group', '?')} | score={n.get('score', 0):.2f} "
                f"| hop={n.get('hop', 0)}" + (f" | {ev}" if ev else "")
            )
        return "\n".join(rows)

    sys_parts = [
        "You are an Azure architect. A dependency trace started from ONE seed resource and "
        "followed ARM id references, private endpoints, managed-identity role assignments, "
        "deployment markers, tags, resource-group boundaries and naming to reverse-engineer "
        "the WORKLOAD (application/product) that the seed belongs to.\n\n"
        "The CONFIRMED members are already decided — do not remove them unless a resource is "
        "obviously shared platform infrastructure. Your job is to:\n"
        "1. Decide which BORDERLINE resources genuinely belong to this workload.\n"
        "2. Give the workload a short, human, product-style name (not a resource name and not "
        "a resource-group name if a better one exists).\n"
        "3. Classify it.\n\n"
        "SHARED PLATFORM resources (hub networking, central logging, shared DNS) are listed for "
        "context only — never include them as members.\n\n"
        'Reply with ONLY JSON: {"name": "...", "description": "...", "reasoning": "...", '
        '"confidence": 0.0-1.0, "include_borderline": [indices], "exclude_members": [indices], '
        '"workload_type": "...", "environment": "...", "criticality": "...", '
        '"data_classification": "..."}',
        _classify_ask(),
    ]
    if memory_hint:
        sys_parts.append("\n\n" + memory_hint)

    user = (
        f"SEED: {seedres.get('name', '?')} | {friendly_type(seedres.get('resource_type'))} "
        f"| rg={seedres.get('resource_group', '?')}\n\n"
        f"CONFIRMED MEMBERS ({len(members)}, indices 0..{max(0, len(members) - 1)}):\n"
        f"{_lines(members, 0) or '(none)'}\n\n"
        f"BORDERLINE ({len(borderline)}, indices 1000..{1000 + max(0, len(borderline) - 1)}):\n"
        f"{_lines(borderline, 1000) or '(none)'}\n\n"
        f"SHARED PLATFORM (context only, {len(shared)}):\n"
        f"{_lines(shared, 2000) or '(none)'}"
    )
    try:
        text = await _complete([
            {"role": "system", "content": "\n".join(sys_parts)},
            {"role": "user", "content": user},
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Seed adjudication AI call failed: %s", exc)
        return None
    obj = _extract_json(text)
    if not isinstance(obj, dict) or not obj.get("name"):
        return None
    try:
        conf = float(obj.get("confidence", 0.7))
    except (TypeError, ValueError):
        conf = 0.7
    return {
        "name": str(obj.get("name", ""))[:120],
        "description": str(obj.get("description", ""))[:400],
        "reasoning": str(obj.get("reasoning", ""))[:800],
        "confidence": max(0.0, min(1.0, conf)),
        "include_borderline": [i - 1000 for i in (obj.get("include_borderline") or []) if _safe_int(i) >= 1000],
        "exclude_members": [i for i in (obj.get("exclude_members") or []) if 0 <= _safe_int(i) < len(members)],
        "workload_type": obj.get("workload_type"),
        "environment": obj.get("environment"),
        "criticality": obj.get("criticality"),
        "data_classification": obj.get("data_classification"),
    }


async def _run_trace(
    connection: dict | None,
    seedres: dict[str, Any],
    *,
    max_hops: int,
    tenant_id: str,
    connection_id: str,
    session: str | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    """Collect (or reuse) the raw link graph for a seed. Returns ``(raw, resources, notes)``."""
    key = _trace_key(tenant_id, connection_id, str(seedres.get("id", "")), max_hops)
    cached = _trace_cache_get(key)
    if cached is not None:
        raw, resources = cached
        return raw, resources, ["reused the cached trace"]
    sub_ids, _err = await _resolve_subs(connection, "resource", str(seedres.get("id", "")))
    resources, raw, notes = await seed_links.gather_universe(
        connection, seedres, sub_ids, max_hops=max_hops, session=session
    )
    edges = seed_mod.build_edges(resources, raw)
    raw = {**raw, "edges": edges}
    _trace_cache_put(key, raw, resources)
    return raw, resources, notes


def _score_from_raw(
    seed_id: str,
    resources: dict[str, dict[str, Any]],
    raw: dict[str, Any],
    *,
    max_hops: int,
    threshold: float,
    hub_fanin: int,
    enabled_kinds: list[str] | None,
) -> dict[str, Any]:
    return seed_mod.score_graph(
        seed_id, resources, raw.get("edges") or [],
        max_hops=max_hops, threshold=threshold,
        hub_fanin_limit=hub_fanin, enabled_kinds=enabled_kinds or None,
    )


async def trace_from_seed(
    connection: dict | None,
    resource_id: str,
    *,
    preset: str = "balanced",
    max_hops: int = 0,
    threshold: float = 0.0,
    hub_fanin: int = 0,
    edge_kinds: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a seed trace: status events, then ONE ``trace`` event with the scored graph.

    No LLM is involved — this is the free pre-flight the user sculpts before naming."""
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return

    cfg = seed_mod.preset_config(preset)
    hops = int(max_hops or cfg["max_hops"])
    thresh = float(threshold or cfg["threshold"])
    fanin = int(hub_fanin or seed_mod.DEFAULT_HUB_FANIN)
    tenant_id = connection.get("tenant_id", "") or "default"
    connection_id = connection.get("id", "") or ""

    yield _status("seed", "Resolving the seed resource…")
    # A trace fires ~8-12 Resource Graph queries. ONE service-principal login is opened here
    # and reused by every one of them — otherwise each query re-runs `az login` and the
    # trace spends most of its wall clock authenticating.
    from app.exec.command_runner import close_sp_session, open_sp_session

    session, sess_err = await open_sp_session(connection)
    try:
        if sess_err:
            yield {"type": "error", "message": sess_err}
            return
        seedres, err = await seed_links.resolve_seed(connection, resource_id, session=session)
        if err or not seedres:
            yield {"type": "error", "message": err or "Seed resource not found."}
            return
        yield _status(
            "seed",
            f"Seed: “{seedres.get('name')}” ({friendly_type(seedres.get('resource_type'))}) "
            f"in resource group “{seedres.get('resource_group')}”.",
            seed=seedres.get("id", ""),
        )

        yield _status("tracing", f"Following relationships up to {hops} hop(s) — references, private endpoints, identity grants, tags…")
        raw, resources, notes = await _run_trace(
            connection, seedres, max_hops=hops, tenant_id=tenant_id,
            connection_id=connection_id, session=session,
        )
    finally:
        close_sp_session(session)
    for note in notes:
        yield _status("tracing", f"Candidate universe — {note}.")
    yield _status(
        "tracing",
        f"Built a {len(raw.get('edges') or [])}-edge graph over {len(resources)} candidate resource(s).",
        universe=len(resources), edges=len(raw.get("edges") or []),
    )

    result = _score_from_raw(
        str(seedres.get("id", "")).lower(), resources, raw,
        max_hops=hops, threshold=thresh, hub_fanin=fanin, enabled_kinds=edge_kinds,
    )
    stats = result["stats"]
    yield _status(
        "scoring",
        f"{stats['members']} member(s), {stats['borderline']} borderline, "
        f"{stats['shared']} shared-platform dependency(ies).",
        **{k: v for k, v in stats.items() if isinstance(v, (int, float))},
    )

    # Does the seed (or any proposed member) already live in a saved workload?
    existing = _existing_workload_for(result["nodes"])
    yield {
        "type": "trace",
        "seed": {
            "id": seedres.get("id", ""),
            "name": seedres.get("name", ""),
            "resource_type": seedres.get("resource_type", ""),
            "resource_group": seedres.get("resource_group", ""),
            "subscription_id": seedres.get("subscription_id", ""),
            "location": seedres.get("location", ""),
            "tags": seedres.get("tags") or {},
        },
        "nodes": result["nodes"],
        "edges": result["edges"],
        "stats": stats,
        "edge_kinds": [
            {"kind": k, "label": seed_mod.EDGE_LABELS[k], "weight": seed_mod.EDGE_WEIGHTS[k]}
            for k in seed_mod.EDGE_KINDS
        ],
        "existing_workload": existing,
        "meta": {"max_hops": hops, "threshold": thresh, "preset": preset, "universe": len(resources)},
    }


def _existing_workload_for(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The saved workload that already claims the seed (or most of the trace), if any."""
    from app.workloads.registry import list_workloads

    ids = {str(n.get("id", "")).lower() for n in nodes if n.get("bucket") == "member"}
    seed_ids = {str(n.get("id", "")).lower() for n in nodes if n.get("is_seed")}
    if not ids:
        return None
    best: dict[str, Any] | None = None
    for wl in list_workloads():
        owned = {
            str(n.get("id", "")).lower()
            for n in (wl.get("nodes") or [])
            if n.get("kind") == "resource" and n.get("id")
        }
        overlap = owned & ids
        if not overlap:
            continue
        entry = {
            "id": wl.get("id", ""),
            "name": wl.get("name", ""),
            "overlap": len(overlap),
            "owns_seed": bool(owned & seed_ids),
            "new_resources": len(ids - owned),
        }
        if best is None or entry["overlap"] > best["overlap"] or (entry["owns_seed"] and not best["owns_seed"]):
            best = entry
    return best


async def discover_from_seed(
    connection: dict | None,
    resource_id: str,
    *,
    preset: str = "balanced",
    max_hops: int = 0,
    threshold: float = 0.0,
    hub_fanin: int = 0,
    edge_kinds: list[str] | None = None,
    include_ids: list[str] | None = None,
    use_ai: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Turn a seed trace into ONE workload candidate, optionally named/adjudicated by AI.

    Emits the same ``status`` / ``candidate`` / ``done`` events as the top-down flow, so the
    review + save path in the UI works unchanged."""
    if connection is None:
        yield {"type": "error", "message": "No Azure connection selected."}
        return

    cfg = seed_mod.preset_config(preset)
    hops = int(max_hops or cfg["max_hops"])
    thresh = float(threshold or cfg["threshold"])
    fanin = int(hub_fanin or seed_mod.DEFAULT_HUB_FANIN)
    tenant_id = connection.get("tenant_id", "") or "default"
    connection_id = connection.get("id", "") or ""

    yield _status("seed", "Resolving the seed resource…")
    from app.exec.command_runner import close_sp_session, open_sp_session

    session, sess_err = await open_sp_session(connection)
    try:
        if sess_err:
            yield {"type": "error", "message": sess_err}
            return
        seedres, err = await seed_links.resolve_seed(connection, resource_id, session=session)
        if err or not seedres:
            yield {"type": "error", "message": err or "Seed resource not found."}
            return

        yield _status("tracing", "Rebuilding the dependency trace…")
        raw, resources, _notes = await _run_trace(
            connection, seedres, max_hops=hops, tenant_id=tenant_id,
            connection_id=connection_id, session=session,
        )
    finally:
        close_sp_session(session)
    result = _score_from_raw(
        str(seedres.get("id", "")).lower(), resources, raw,
        max_hops=hops, threshold=thresh, hub_fanin=fanin, enabled_kinds=edge_kinds,
    )
    nodes = result["nodes"]
    by_id = {str(n["id"]).lower(): n for n in nodes}

    # The user's explicit selection (from the trace stage) wins over the scored buckets.
    if include_ids:
        wanted = {str(i).lower() for i in include_ids}
        chosen = [by_id[i] for i in wanted if i in by_id]
        borderline: list[dict[str, Any]] = []
    else:
        chosen = [n for n in nodes if n["bucket"] == "member"]
        borderline = [n for n in nodes if n["bucket"] == "borderline"]
    shared_nodes = [n for n in nodes if n["bucket"] == "shared"]
    # The seed is always part of its own workload.
    if not any(n.get("is_seed") for n in chosen):
        seed_node = next((n for n in nodes if n.get("is_seed")), None)
        if seed_node:
            chosen.insert(0, seed_node)
    if not chosen:
        yield {"type": "done", "candidates": [], "meta": {"resource_count": 0, "seed": seedres.get("id", ""), "used_ai": False}}
        return

    yield _status("grouping", f"{len(chosen)} resource(s) selected for the workload.")

    name = seed_mod.default_name(seedres, chosen)
    description = f"Workload reverse-engineered from “{seedres.get('name')}”."
    reasoning = (
        f"Traced {len(chosen)} resource(s) within {hops} hop(s) of the seed using ARM id "
        f"references, private endpoints, identity grants, deployment markers, tags and naming."
    )
    confidence = 0.75
    workload_type = ""
    environment = ""
    criticality = ""
    data_classification = ""
    used_ai = False

    if use_ai:
        yield _status("grouping", "Naming and classifying the workload with AI…")
        memory_hint = grouping_memory.prompt_hint(tenant_id, connection_id)
        verdict = await _ai_adjudicate_seed(seedres, chosen, borderline, shared_nodes, memory_hint)
        if verdict:
            used_ai = True
            name = verdict["name"] or name
            description = verdict["description"] or description
            reasoning = verdict["reasoning"] or reasoning
            confidence = verdict["confidence"]
            workload_type = _norm_class(verdict.get("workload_type"), VALID_TYPES, "")
            environment = _norm_class(verdict.get("environment"), VALID_ENVS, "")
            criticality = _norm_class(verdict.get("criticality"), VALID_CRIT, "")
            data_classification = _norm_class(
                verdict.get("data_classification"), ("confidential", "internal", "public", "unknown"), ""
            )
            dropped = {i for i in verdict["exclude_members"] if 0 <= i < len(chosen)}
            added = [borderline[i] for i in verdict["include_borderline"] if 0 <= i < len(borderline)]
            if dropped:
                chosen = [n for i, n in enumerate(chosen) if i not in dropped or n.get("is_seed")]
                yield _status("grouping", f"AI excluded {len(dropped)} resource(s) as shared platform or unrelated.", excluded=len(dropped))
            if added:
                chosen = chosen + added
                yield _status("grouping", f"AI confirmed {len(added)} borderline resource(s) as part of the workload.", included=len(added))
        else:
            yield _status("grouping", "AI naming unavailable — using the deterministic name from the seed.")

    members = [
        {
            "id": n["id"], "name": n["name"], "resource_type": n["resource_type"],
            "location": n["location"], "resource_group": n["resource_group"],
            "subscription_id": n["subscription_id"], "tags": resources.get(str(n["id"]).lower(), {}).get("tags") or {},
        }
        for n in chosen
    ]
    group = {
        "name": name, "description": description, "reasoning": reasoning,
        "confidence": confidence, "members": members,
        "workload_type": workload_type, "environment": environment,
        "criticality": criticality, "data_classification": data_classification,
    }
    cand = _candidate(group, {})
    cand["evidence"] = _seed_evidence(seedres, chosen)
    types_str = ", ".join(f"{t['label']} ({t['count']})" for t in cand["types"][:6])
    yield {
        "type": "candidate",
        "candidate": cand,
        "message": f"“{cand['name']}” — {cand['resource_count']} resources: {types_str}",
    }
    yield {
        "type": "done",
        "candidates": [cand],
        "meta": {
            "resource_count": len(resources),
            "considered": len(nodes),
            "ungrouped": 0,
            "organized_pct": 100,
            "used_ai": used_ai,
            "truncated": False,
            "seed": seedres.get("id", ""),
            "seed_name": seedres.get("name", ""),
            "max_hops": hops,
            "threshold": thresh,
            "shared_excluded": len(shared_nodes),
            "preset": preset,
        },
    }


