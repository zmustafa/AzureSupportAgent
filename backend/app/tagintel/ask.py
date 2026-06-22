"""Natural-language tag console (F10).

Deterministic-first: parses common tag questions and answers them directly from the already
computed census / resource list, always returning the equivalent read-only Azure Resource
Graph query for transparency. This keeps the console fast, free, and reliable (no LLM round
trip needed for the frequent questions), while still exposing the KQL a user can run in the
portal or az CLI.
"""
from __future__ import annotations

import re
from typing import Any

from app.tagintel.analysis import norm_key

_ALL_KEYS_RE = re.compile(r"\b(all|every|list|show|what)\b.*\b(tag )?keys?\b", re.I)
_VALUES_RE = re.compile(r"\b(values?|distinct|unique)\b.*?\bfor\b\s+([A-Za-z0-9 _\-./]+)$|\bvalues?\s+of\s+([A-Za-z0-9 _\-./]+)$", re.I)
_UNTAGGED_RE = re.compile(r"\b(untagged|without (any )?tags?|no tags?)\b", re.I)
_MISSING_RE = re.compile(r"\bmissing\b\s+([A-Za-z0-9 _\-./]+)$", re.I)
_HIGHCARD_RE = re.compile(r"\bhigh[- ]?cardinality\b", re.I)
# Signals a compound/complex question the simple regex templates can't handle (e.g. a tag
# condition AND a resource-type condition) — these are routed to the AI NL→ARG path.
_COMPOUND_RE = re.compile(r"\b(and|or|type|of\s+\w+\s+type|in\s+\w+|where|with)\b", re.I)


def _key_lookup(census: dict[str, Any], name: str) -> dict[str, Any] | None:
    target = norm_key(name)
    for k in census.get("keys", []):
        if norm_key(k["key"]) == target:
            return k
    # Partial contains match as a fallback.
    for k in census.get("keys", []):
        if target and target in norm_key(k["key"]):
            return k
    return None


def answer(question: str, census: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Answer a natural-language tag question from the census + resource list."""
    q = (question or "").strip()
    if not q:
        return {"kind": "empty", "answer": "Ask a question about your tags.", "generated_query": ""}

    # All tag keys.
    if _ALL_KEYS_RE.search(q):
        keys = census.get("keys", [])
        return {
            "kind": "keys",
            "answer": f"{len(keys)} distinct tag keys across the selected scope.",
            "data": [{"key": k["key"], "count": k["count"], "category": k["category"]} for k in keys],
            "generated_query": "resources\n| mv-expand bag=tags\n| extend key=tostring(bag)\n| summarize resources=count() by key=tostring(bagkeys(tags)[0])\n| order by resources desc",
        }

    # Distinct values for a key.
    m = _VALUES_RE.search(q)
    if m:
        name = (m.group(2) or m.group(3) or "").strip()
        k = _key_lookup(census, name)
        if k:
            return {
                "kind": "values",
                "answer": f"'{k['key']}' has {k['distinct_values']} distinct values across {k['count']} resources.",
                "data": k.get("top_values", []),
                "key": k["key"],
                "generated_query": f"resources\n| where isnotempty(tags['{k['key']}'])\n| summarize resources=count() by value=tostring(tags['{k['key']}'])\n| order by resources desc",
            }
        return {"kind": "not_found", "answer": f"No tag key matching '{name}' was found.", "generated_query": ""}

    # Missing a specific key.
    m = _MISSING_RE.search(q)
    if m and not _UNTAGGED_RE.search(q):
        name = m.group(1).strip()
        k = _key_lookup(census, name)
        # Only answer deterministically when the captured phrase resolves to a REAL tag key AND
        # the question is simple (no compound 'and'/'or'/'type' clause). A phrase like
        # "Owner and of virtual machine type" must NOT be treated as a single tag key — defer it
        # to the AI path which understands compound conditions.
        if k is not None and not _COMPOUND_RE.search(q):
            key_name = k["key"]
            hits = [r for r in resources if not any(kk.lower() == key_name.lower() and str(vv).strip() for kk, vv in (r.get("tags") or {}).items())]
            return {
                "kind": "missing",
                "answer": f"{len(hits)} resources are missing the '{key_name}' tag.",
                "data": [{"id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", ""),
                          "resource_group": r.get("resource_group", "")} for r in hits[:200]],
                "key": key_name,
                "generated_query": f"resources\n| where isempty(tags['{key_name}'])\n| project id, name, type, resourceGroup",
            }
        # Couldn't resolve to a single known key (or it's compound) → let the AI handle it.
        return {"kind": "unknown", "needs_ai": True, "answer": "", "data": [], "generated_query": ""}

    # Untagged resources.
    if _UNTAGGED_RE.search(q):
        sample = census.get("untagged_sample", [])
        return {
            "kind": "untagged",
            "answer": f"{census.get('untagged_count', 0)} of {census.get('total_resources', 0)} resources have no tags.",
            "data": sample,
            "generated_query": "resources\n| where isnull(tags) or array_length(bagkeys(tags)) == 0\n| project id, name, type, resourceGroup",
        }

    # High-cardinality tags.
    if _HIGHCARD_RE.search(q):
        hc = [k for k in census.get("keys", []) if k.get("high_cardinality")]
        return {
            "kind": "high_cardinality",
            "answer": f"{len(hc)} tag keys look high-cardinality (many distinct values relative to usage).",
            "data": [{"key": k["key"], "distinct_values": k["distinct_values"], "count": k["count"]} for k in hc],
            "generated_query": "resources\n| mv-expand tags\n| summarize values=dcount(tostring(tags)) by key=tostring(bagkeys(tags)[0])\n| where values > 50\n| order by values desc",
        }

    # Fallback: keyword match across keys.
    target = norm_key(q)
    matches = [k for k in census.get("keys", []) if target and target in norm_key(k["key"])]
    if matches:
        return {
            "kind": "keys",
            "answer": f"{len(matches)} tag keys match '{q}'.",
            "data": [{"key": k["key"], "count": k["count"], "category": k["category"]} for k in matches],
            "generated_query": f"resources\n| where isnotempty(tags)\n| project id, name, tags",
        }
    return {
        "kind": "unknown",
        "needs_ai": True,
        "answer": "I couldn't map that to a tag query. Try: 'show all tag keys', 'values for Environment', "
                  "'resources missing Owner', 'untagged resources', or 'high-cardinality tags'.",
        "data": [],
        "generated_query": "",
    }


# =========================================================================== AI NL → ARG
# For compound / free-form questions the deterministic templates can't parse (e.g. "resources
# missing Owner AND of virtual machine type"), the LLM produces a STRUCTURED filter spec. We
# evaluate that spec over the already-loaded resource list (fast, no extra Azure call) and build
# the equivalent read-only Resource Graph KQL deterministically — so the displayed query always
# matches the results exactly. Degrades gracefully to the deterministic answer if no provider.
import asyncio  # noqa: E402
import json as _json  # noqa: E402
import logging  # noqa: E402

log = logging.getLogger("app.tagintel.ask")

_AI_TIMEOUT_SECONDS = 14.0

_FILTER_SYS = (
    "You translate a natural-language question about Azure resource TAGS into a STRUCTURED JSON "
    "filter over the Azure Resource Graph 'resources' table. Use ONLY the provided tag keys and "
    "resource types; resolve casing/aliases to the exact provided value (e.g. 'virtual machine' "
    "→ 'microsoft.compute/virtualmachines', 'owner' → the exact tag key). Combine multiple "
    "conditions (they are ANDed). Return STRICT JSON with this shape (omit empty fields):\n"
    "{\n"
    '  "explanation": "one short sentence",\n'
    '  "missing_all_tags": ["TagKey", ...],   // resources LACKING every listed tag key\n'
    '  "present_all_tags": ["TagKey", ...],    // resources that HAVE every listed tag key\n'
    '  "tag_equals": [{"key":"Env","value":"prod"}],\n'
    '  "tag_not_equals": [{"key":"Env","value":"prod"}],\n'
    '  "types": ["microsoft.compute/virtualmachines", ...],  // exact ARM types (lowercase)\n'
    '  "name_contains": "",\n'
    '  "resource_group_contains": "",\n'
    '  "locations": ["eastus", ...]\n'
    "}\n"
    "Return ONLY the JSON object — no prose, no markdown."
)


async def _complete_json(system: str, user: str) -> Any:
    """Stream a completion and parse the JSON object out of it. Returns None on any failure."""
    from app.agent.factory import build_provider
    from app.core.utils import safe_json_parse

    provider = build_provider()
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    if "```" in t:
        mm = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if mm:
            t = mm.group(1).strip()
    if not t.startswith("{"):
        mm = re.search(r"(\{.*\})", t, re.DOTALL)
        if mm:
            t = mm.group(1)
    return safe_json_parse(t, default=None)


def _norm_pairs(raw: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in raw or []:
        if isinstance(p, dict) and p.get("key"):
            out.append((str(p["key"]), str(p.get("value", ""))))
    return out


def _eval_filters(f: dict[str, Any], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate a structured filter over the cached resource list. All clauses are ANDed."""
    missing = [str(k) for k in (f.get("missing_all_tags") or []) if str(k).strip()]
    present = [str(k) for k in (f.get("present_all_tags") or []) if str(k).strip()]
    eq = _norm_pairs(f.get("tag_equals"))
    neq = _norm_pairs(f.get("tag_not_equals"))
    types = [str(t).lower() for t in (f.get("types") or []) if str(t).strip()]
    name_sub = str(f.get("name_contains") or "").strip().lower()
    rg_sub = str(f.get("resource_group_contains") or "").strip().lower()
    locs = [str(loc).lower() for loc in (f.get("locations") or []) if str(loc).strip()]

    def _tag(r: dict[str, Any], key: str) -> str | None:
        for kk, vv in (r.get("tags") or {}).items():
            if kk.lower() == key.lower():
                return str(vv)
        return None

    out: list[dict[str, Any]] = []
    for r in resources:
        if types and (r.get("type", "") or "").lower() not in types:
            continue
        if name_sub and name_sub not in (r.get("name", "") or "").lower():
            continue
        if rg_sub and rg_sub not in (r.get("resource_group", "") or "").lower():
            continue
        if locs and (r.get("location", "") or "").lower() not in locs:
            continue
        if missing and any((_tag(r, k) or "").strip() for k in missing):
            continue  # has at least one of the keys that should be missing
        if present and not all((_tag(r, k) or "").strip() for k in present):
            continue
        if eq and not all((_tag(r, k) or "").strip().lower() == v.strip().lower() for k, v in eq):
            continue
        if neq and any((_tag(r, k) or "").strip().lower() == v.strip().lower() for k, v in neq):
            continue
        out.append(r)
    return out


def _build_kql(f: dict[str, Any]) -> str:
    """Build the equivalent read-only Resource Graph KQL from a structured filter (for display)."""
    lines = ["resources"]
    for k in (f.get("missing_all_tags") or []):
        if str(k).strip():
            lines.append(f"| where isempty(tostring(tags['{k}']))")
    for k in (f.get("present_all_tags") or []):
        if str(k).strip():
            lines.append(f"| where isnotempty(tostring(tags['{k}']))")
    for p in _norm_pairs(f.get("tag_equals")):
        lines.append(f"| where tostring(tags['{p[0]}']) =~ '{p[1]}'")
    for p in _norm_pairs(f.get("tag_not_equals")):
        lines.append(f"| where tostring(tags['{p[0]}']) !~ '{p[1]}'")
    types = [str(t).lower() for t in (f.get("types") or []) if str(t).strip()]
    if len(types) == 1:
        lines.append(f"| where type =~ '{types[0]}'")
    elif types:
        joined = ", ".join(f"'{t}'" for t in types)
        lines.append(f"| where type in~ ({joined})")
    if str(f.get("name_contains") or "").strip():
        lines.append(f"| where name contains '{f['name_contains']}'")
    if str(f.get("resource_group_contains") or "").strip():
        lines.append(f"| where resourceGroup contains '{f['resource_group_contains']}'")
    locs = [str(loc).lower() for loc in (f.get("locations") or []) if str(loc).strip()]
    if locs:
        joined = ", ".join(f"'{loc}'" for loc in locs)
        lines.append(f"| where location in~ ({joined})")
    lines.append("| project id, name, type, resourceGroup, tags")
    return "\n".join(lines)


def _filter_has_signal(f: dict[str, Any]) -> bool:
    return any(f.get(k) for k in (
        "missing_all_tags", "present_all_tags", "tag_equals", "tag_not_equals",
        "types", "name_contains", "resource_group_contains", "locations",
    ))


async def answer_ai(question: str, census: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """AI NL→ARG for compound/free-form tag questions. Returns a result dict (with a real
    generated KQL + the matching rows), or None when the AI is unavailable / produced nothing
    usable so the caller can fall back to the deterministic answer."""
    keys = sorted({k["key"] for k in census.get("keys", [])})
    types = sorted({(r.get("type", "") or "").lower() for r in resources if r.get("type")})
    user = (
        f"Question: {question}\n\n"
        f"Available tag keys: {_json.dumps(keys[:200])}\n"
        f"Available resource types (lowercase ARM): {_json.dumps(types[:200])}\n"
        "Return only the JSON filter object."
    )
    try:
        parsed = await asyncio.wait_for(_complete_json(_FILTER_SYS, user), timeout=_AI_TIMEOUT_SECONDS)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 — any provider error degrades to deterministic
        log.info("tagintel ask AI unavailable: %s", exc)
        return None
    if not isinstance(parsed, dict) or not _filter_has_signal(parsed):
        return None

    rows = _eval_filters(parsed, resources)
    kql = _build_kql(parsed)
    explanation = str(parsed.get("explanation") or "").strip()
    answer = explanation or f"{len(rows)} resource(s) match your question."
    if not explanation:
        answer = f"Found {len(rows)} matching resource(s)."
    else:
        answer = f"{explanation} ({len(rows)} match)"
    return {
        "kind": "ai_query",
        "answer": answer,
        "data": [{"id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", ""),
                  "resource_group": r.get("resource_group", "")} for r in rows[:200]],
        "generated_query": kql,
        "source": "ai",
    }

