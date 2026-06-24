"""AI Tag Generator: plain-English instruction → a concrete, grounded tag change-set.

Where :mod:`app.tagintel.ask` answers *questions* about tags (read-only NL→ARG), this module
turns a tagging *instruction* ("tag everything in this workload with environment=prod and add a
cost-center to anything missing it") into a list of :class:`TagRemediationOp`-shaped operations
that the existing Remediate flow can dry-run, preview, apply and roll back.

Design guarantees:
* **Propose-only.** This never writes to Azure. It returns a plan the user reviews and hands off
  to the Remediate builder, where the audited approve→apply→rollback path takes over.
* **Grounded.** The LLM only sees the real estate context (existing tag keys/values, resource
  types, canonical catalog). Its output is re-validated against that context: hallucinated ARM
  types are dropped, and each op's target is evaluated over the ACTUAL loaded resources so the
  ``resource_ids`` it carries are real. Anything that resolves to zero resources is dropped.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any

from app.tagintel.ask import _complete_json, _eval_filters

log = logging.getLogger("app.tagintel.generate")

_AI_TIMEOUT_SECONDS = 20.0
_MAX_OPS = 40

# The op types the Remediate builder understands. Generation is restricted to the additive /
# normalizing kinds (it should not propose deletes from a vague instruction).
_ALLOWED_OPS = {"add_tag", "set_tag", "rename_key", "normalize_value", "remove_key"}

_GEN_SYS = (
    "You are an Azure tagging assistant. Turn the user's plain-English tagging INSTRUCTION into a "
    "concrete set of tag operations over the Azure Resource Graph 'resources' table. Use ONLY the "
    "provided existing tag keys, values, resource types and canonical catalog keys; resolve "
    "casing/aliases to the exact provided spelling (e.g. 'owner' -> the exact existing key, "
    "'virtual machine' -> 'microsoft.compute/virtualmachines'). Prefer existing/canonical keys "
    "over inventing new ones. Return STRICT JSON with this shape:\n"
    "{\n"
    '  "summary": "one short sentence describing the change-set",\n'
    '  "operations": [\n'
    "    {\n"
    '      "type": "add_tag|set_tag|rename_key|normalize_value|remove_key",\n'
    '      "key": "TagKey",                  // the key to add/set/remove, or the FROM key for rename\n'
    '      "value": "value",                 // for add_tag/set_tag\n'
    '      "to_key": "NewKey",               // for rename_key\n'
    '      "from_value": "PRD", "to_value": "Production",  // for normalize_value\n'
    '      "rationale": "why this op",\n'
    '      "target": {                        // which resources this op applies to (ANDed)\n'
    '        "types": ["microsoft.compute/virtualmachines"],\n'
    '        "name_contains": "", "resource_group_contains": "",\n'
    '        "locations": ["eastus"],\n'
    '        "missing_all_tags": ["Owner"],   // only resources LACKING these keys\n'
    '        "present_all_tags": ["Env"],     // only resources that HAVE these keys\n'
    '        "tag_equals": [{"key":"Env","value":"prod"}],\n'
    '        "tag_not_equals": [{"key":"Env","value":"prod"}]\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}\n"
    "When the instruction says 'everything' / 'all resources' with no qualifier, use an empty "
    "target ({}) so it applies estate-wide. For add_tag use it to fill gaps (missing key); use "
    "set_tag to overwrite. Return ONLY the JSON object - no prose, no markdown."
)


def _clean_pairs(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for p in raw or []:
        if isinstance(p, dict) and str(p.get("key", "")).strip():
            out.append({"key": str(p["key"]).strip(), "value": str(p.get("value", "")).strip()})
    return out


def _clean_target(raw: Any, known_types: set[str]) -> dict[str, Any]:
    """Sanitize a proposed target: drop hallucinated ARM types, keep grounded clauses only."""
    t = raw if isinstance(raw, dict) else {}
    types = [str(x).lower() for x in (t.get("types") or []) if str(x).strip()]
    # Ground types against what actually exists; silently drop unknown ones.
    types = [x for x in types if x in known_types]
    out: dict[str, Any] = {}
    if types:
        out["types"] = types
    for list_key in ("missing_all_tags", "present_all_tags", "locations"):
        vals = [str(x).strip() for x in (t.get(list_key) or []) if str(x).strip()]
        if vals:
            out[list_key] = vals
    for pair_key in ("tag_equals", "tag_not_equals"):
        pairs = _clean_pairs(t.get(pair_key))
        if pairs:
            out[pair_key] = pairs
    for s_key in ("name_contains", "resource_group_contains"):
        s = str(t.get(s_key) or "").strip()
        if s:
            out[s_key] = s
    return out


def _op_is_complete(op: dict[str, Any]) -> bool:
    """A proposed op must carry the fields its type needs (mirrors the FE/remediation rules)."""
    t = op.get("type")
    key = str(op.get("key", "")).strip()
    if t in ("add_tag", "set_tag"):
        return bool(key and str(op.get("value", "")).strip())
    if t == "rename_key":
        return bool(key and str(op.get("to_key", "")).strip())
    if t == "normalize_value":
        return bool(key and str(op.get("to_value", "")).strip())
    if t == "remove_key":
        return bool(key)
    return False


async def propose(
    question: str, census: dict[str, Any], resources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Translate a tagging instruction into a grounded change-set.

    Returns ``{available, summary, operations, notes}`` where each operation is a
    ``TagRemediationOp``-shaped dict carrying a resolved ``resource_ids`` list, or ``None`` when
    the AI is unavailable / produced nothing usable (caller falls back to a helpful message)."""
    keys = sorted({k["key"] for k in census.get("keys", [])})
    # A compact value sample per key keeps the prompt grounded without blowing the context.
    values_by_key: dict[str, list[str]] = {}
    for k in census.get("keys", []):
        vals = [str(v.get("value", "")) for v in (k.get("top_values") or [])][:8]
        if vals:
            values_by_key[k["key"]] = vals
    types = sorted({(r.get("type", "") or "").lower() for r in resources if r.get("type")})
    known_types = set(types)

    user = (
        f"Instruction: {question}\n\n"
        f"Existing tag keys: {_json.dumps(keys[:200])}\n"
        f"Existing values (sample, per key): {_json.dumps(values_by_key)[:4000]}\n"
        f"Existing resource types (lowercase ARM): {_json.dumps(types[:200])}\n"
        "Return only the JSON object."
    )
    try:
        parsed = await asyncio.wait_for(_complete_json(_GEN_SYS, user), timeout=_AI_TIMEOUT_SECONDS)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 — any provider error -> None
        log.info("tagintel generate AI unavailable: %s", exc)
        return None
    if not isinstance(parsed, dict):
        return None

    raw_ops = parsed.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        return None

    operations: list[dict[str, Any]] = []
    notes: list[str] = []
    for raw in raw_ops[:_MAX_OPS]:
        if not isinstance(raw, dict):
            continue
        op_type = str(raw.get("type", "")).strip()
        if op_type not in _ALLOWED_OPS:
            continue
        op: dict[str, Any] = {
            "type": op_type,
            "key": str(raw.get("key", "")).strip(),
        }
        if op_type in ("add_tag", "set_tag"):
            op["value"] = str(raw.get("value", "")).strip()
        elif op_type == "rename_key":
            op["to_key"] = str(raw.get("to_key", "")).strip()
        elif op_type == "normalize_value":
            op["from_value"] = str(raw.get("from_value", "")).strip()
            op["to_value"] = str(raw.get("to_value", "")).strip()
        if not _op_is_complete(op):
            continue

        target = _clean_target(raw.get("target"), known_types)
        matched = _eval_filters(target, resources) if target else list(resources)
        if not matched:
            notes.append(f"Skipped '{_describe(op)}' — it matched 0 resources in this scope.")
            continue
        op["resource_ids"] = [r.get("id", "") for r in matched if r.get("id")]
        op["rationale"] = str(raw.get("rationale", "")).strip()
        op["match_count"] = len(op["resource_ids"])
        operations.append(op)

    if not operations:
        return {"available": True, "summary": "", "operations": [], "notes": notes or [
            "The AI did not produce any operations that apply to resources in this scope."]}

    return {
        "available": True,
        "summary": str(parsed.get("summary") or "").strip(),
        "operations": operations,
        "notes": notes,
    }


def _describe(op: dict[str, Any]) -> str:
    t = op.get("type")
    if t == "add_tag":
        return f"add {op.get('key')}={op.get('value')}"
    if t == "set_tag":
        return f"set {op.get('key')}={op.get('value')}"
    if t == "rename_key":
        return f"rename {op.get('key')}→{op.get('to_key')}"
    if t == "normalize_value":
        return f"normalize {op.get('key')} {op.get('from_value')}→{op.get('to_value')}"
    if t == "remove_key":
        return f"remove {op.get('key')}"
    return str(t)
