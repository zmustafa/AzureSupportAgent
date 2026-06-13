"""AI'fication: turn raw command/KQL output into structured intelligence.

Given a workbook's raw stdout, an LLM produces — in a single call — a human ``narrative``
summary, an optional ``structured`` extraction (when the workbook requests it), and a
normalized ``severity`` (info|warning|error|critical). Always returns both a structured
object and a narrative so downstream consumers (tiles, events, agents) have machine- and
human-readable forms. A "raw" fallback is returned if the model is unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agent.factory import build_provider_for

logger = logging.getLogger("app.workbooks.aify")

SEVERITIES = ("info", "warning", "error", "critical")
_MAX_INPUT = 24_000  # cap raw output fed to the model (token cost control)


def _coerce_severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in SEVERITIES else "info"


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response (handles code fences)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def _complete(messages: list[dict[str, Any]]) -> str:
    """Run a single non-streaming completion by draining the provider stream."""
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


async def aify_output(
    *,
    workbook_name: str,
    description: str,
    runtime: str,
    raw_output: str,
    modes: list[str],
    schema_hint: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Post-process raw output. Returns {narrative, structured, severity}.

    ``modes`` selects which transforms to request (summary / extract / severity). ``diff``
    is computed by the executor, not here. Never raises — on failure returns a raw view.
    """
    raw = (raw_output or "").strip()
    truncated = raw[:_MAX_INPUT]
    wants_extract = "extract" in modes
    wants_severity = "severity" in modes

    fields = ['"narrative": "<2-4 sentence plain-English summary of what the output shows>"']
    if wants_severity:
        fields.append(
            '"severity": "<one of: info, warning, error, critical — based on whether the '
            'result indicates a problem, risk, or healthy state>"'
        )
    if wants_extract:
        hint = schema_hint.strip() or "the key entities, counts and notable values"
        fields.append(
            f'"structured": <a JSON object extracting {hint} from the output; '
            "use concise keys and primitive/array values>"
        )
    shape = "{\n  " + ",\n  ".join(fields) + "\n}"

    from app.core.ai_prompts import get_contract, get_guidance

    # Editable guidance + the dynamically-computed JSON shape + the locked grounding rule.
    guidance = get_guidance("workbook_aify_guidance")
    contract = get_contract("workbook_aify_guidance")
    sys = f"{guidance}\n{shape}\n{contract}".rstrip()
    ctx = (
        f"Workbook: {workbook_name}\nPurpose: {description or '(none)'}\nRuntime: {runtime}\n"
    )
    if error:
        ctx += f"\nThe run reported an ERROR:\n{error[:1500]}\n"
    ctx += f"\nRaw output:\n{truncated or '(empty output)'}"

    fallback = {
        "narrative": (error or (raw[:500] if raw else "No output.")),
        "structured": None,
        "severity": "error" if error else "info",
    }
    try:
        text = await _complete(
            [{"role": "system", "content": sys}, {"role": "user", "content": ctx}]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI'fication failed for %s: %s", workbook_name, exc)
        return fallback

    obj = _extract_json(text)
    if obj is None:
        # Use whatever prose the model produced as the narrative.
        fallback["narrative"] = (text.strip() or fallback["narrative"])[:2000]
        return fallback

    return {
        "narrative": str(obj.get("narrative", "") or fallback["narrative"])[:2000],
        "structured": obj.get("structured") if wants_extract else None,
        "severity": _coerce_severity(obj.get("severity")) if wants_severity else (
            "error" if error else "info"
        ),
    }


def compute_diff(
    previous: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Shallow diff of two structured extractions for "what changed since last run"."""
    if not isinstance(current, dict):
        return None
    prev = previous if isinstance(previous, dict) else {}
    changed: dict[str, Any] = {}
    for key, val in current.items():
        old = prev.get(key)
        if old != val:
            changed[key] = {"from": old, "to": val}
    added = [k for k in current if k not in prev]
    removed = [k for k in prev if k not in current]
    if not (changed or added or removed):
        return {"changed": {}, "added": [], "removed": [], "has_changes": False}
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "has_changes": True,
    }
