"""Reason stage — ask the active LLM to interpret the gathered data and return a
structured, materiality-graded result.

The model is given the pack's (placeholder-filled) instructions plus a compact rendering
of the gathered events, and must return ONLY a JSON object:

    {
      "verdict": "nothing_notable" | "notable" | "urgent",
      "headline": "one-line summary",
      "bullets": ["finding 1", "finding 2", ...],
      "table": [ {time, workload, change, risk, owner, recommended_action}, ... ]
    }

No tools; a single grounded JSON completion (same pattern as the agent/workbook designers).
The model interprets — it never sees, and cannot request, data beyond the pack's sources.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.utils import safe_json_parse
from app.insights import packfile

log = logging.getLogger("app.insights.reason")

_MAX_EVENTS_TO_MODEL = 120

SYSTEM_PROMPT = """\
You are an Azure operations analyst producing a daily "insight pack" digest. You are given \
a set of INSTRUCTIONS describing what to watch for, and a batch of already-collected, \
deterministic change/telemetry data for a specific scope. Interpret the data strictly \
according to the instructions.

Decide a materiality VERDICT:
- "urgent"          — something clearly needs attention now (e.g. new public exposure, \
                      privileged grant, security control removed).
- "notable"         — worth a human glance; real but not an emergency.
- "nothing_notable" — routine/expected only; nothing worth pinging someone about.

Be honest and specific. Do not inflate importance to seem useful — if the data is quiet, \
say so with "nothing_notable". Ground every finding in the provided data; never invent \
resources, actors, or changes that are not present.

Respond with ONLY a JSON object of this exact shape (no prose, no code fence):
{
  "verdict": "nothing_notable" | "notable" | "urgent",
  "headline": "one concise sentence a busy owner can read at a glance",
  "bullets": ["short, specific finding with the resource + why it matters + action", "..."],
  "table": [
    {
      "time": "ISO-ish timestamp from the data",
      "workload": "workload/scope name",
      "change": "what changed",
      "risk": "critical|high|medium|low",
      "owner": "actor who made the change, or 'unknown'",
      "recommended_action": "one concrete next step"
    }
  ]
}
Keep bullets to at most 8. Only include table rows that matter; omit noise. When the \
verdict is "nothing_notable", return an empty table and a single reassuring headline.
"""


def fill_placeholders(instructions: str, *, scope_label: str, lookback_hours: int) -> str:
    out = instructions or ""
    out = out.replace("{{scope_label}}", scope_label).replace("{{lookback_hours}}", str(lookback_hours))
    return out


def _render_bundles(bundles: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    total = 0
    for b in bundles:
        src = b.get("source", "?")
        if not b.get("ok"):
            lines.append(f"### Source: {src} — UNAVAILABLE ({b.get('note') or 'no data'})")
            continue
        counts = b.get("counts") or {}
        note = f" — {b['note']}" if b.get("note") else ""
        lines.append(f"### Source: {src} — {counts.get('total', 0)} change(s){note}")
        for e in (b.get("events") or [])[:_MAX_EVENTS_TO_MODEL]:
            total += 1
            flags = f" [flags: {', '.join(e['flags'])}]" if e.get("flags") else ""
            new = "NEW " if e.get("new") else ""
            lines.append(
                f"- {new}{e.get('time','')} | {e.get('risk','').upper()} | {e.get('category','')} | "
                f"{e.get('resource','')} ({e.get('resource_type','')}) | by {e.get('actor','unknown')} | "
                f"{e.get('change','')}{flags}"
            )
    if total == 0:
        lines.append("(no changes matched the pack's scope and filters in this window)")
    return "\n".join(lines)


def _coerce(parsed: Any, *, fallback_headline: str) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {"verdict": "notable", "headline": fallback_headline, "bullets": [], "table": []}
    verdict = str(parsed.get("verdict", "notable")).lower()
    if verdict not in packfile.VERDICTS:
        verdict = "notable"
    bullets = [str(b)[:500] for b in (parsed.get("bullets") or []) if str(b).strip()][:8]
    table: list[dict[str, str]] = []
    for row in (parsed.get("table") or [])[:200]:
        if not isinstance(row, dict):
            continue
        table.append({
            "time": str(row.get("time", ""))[:40],
            "workload": str(row.get("workload", ""))[:120],
            "change": str(row.get("change", ""))[:400],
            "risk": str(row.get("risk", "")).lower()[:12],
            "owner": str(row.get("owner", "") or "unknown")[:160],
            "recommended_action": str(row.get("recommended_action", ""))[:400],
        })
    return {
        "verdict": verdict,
        "headline": str(parsed.get("headline", "") or fallback_headline)[:300],
        "bullets": bullets,
        "table": table,
    }


async def reason(*, instructions: str, bundles: list[dict[str, Any]], output: dict[str, Any]) -> dict[str, Any]:
    """Run the reasoning completion and return the coerced structured result."""
    from app.agent.factory import build_provider

    total = sum(len(b.get("events") or []) for b in bundles if b.get("ok"))
    fallback = f"{total} change(s) collected." if total else "No changes in this window."
    user = (
        f"INSTRUCTIONS:\n{instructions}\n\n"
        f"REQUESTED OUTPUT: {', '.join(output.get('format') or ['bullets', 'table'])}\n\n"
        f"COLLECTED DATA:\n{_render_bundles(bundles)}\n"
    )
    provider = build_provider()
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}], None
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001 — degrade to a deterministic result
        log.warning("Insight pack reasoning failed: %s", exc)
        verdict = "notable" if total else "nothing_notable"
        return {"verdict": verdict, "headline": fallback, "bullets": [], "table": [],
                "ai_error": str(exc)[:300]}

    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    return _coerce(safe_json_parse(t, default=None), fallback_headline=fallback)
