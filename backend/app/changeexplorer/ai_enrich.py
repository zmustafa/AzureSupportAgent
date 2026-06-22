"""AI enrichment for the Change Explorer.

For each (already-normalized) change, ask the configured LLM to determine the category, a precise
plain-English description of WHAT was done, the possible impact, why it's risky, and a 0-100 risk
hint — reading the actual property diffs (e.g. "an inbound NSG rule was added allowing traffic
from the Internet"). This is what lets the tool go beyond a bare resource-type guess.

Deterministic-first + graceful: events the deterministic classifier already understands are kept;
the AI is used to (a) resolve the ones that came back ``Unknown`` and (b) write a sharper
narrative + risk hint for the highest-impact changes. If no AI provider is configured or it
fails/times out, this is a no-op and the deterministic results stand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

from app.changeexplorer.models import CATEGORIES
from app.core.utils import safe_json_parse

log = logging.getLogger("app.changeexplorer.ai_enrich")

_AI_TIMEOUT_SECONDS = 25.0
_BATCH = 10          # events per LLM call
_MAX_EVENTS = 60     # whole-run cap on AI-analysed events (bounded cost/latency)
_AI_CONCURRENCY = 10  # parallel LLM calls (batches run 10-at-a-time, not sequentially)
_VALID = set(CATEGORIES)

_SYSTEM = (
    "You are a senior Azure change-analysis expert reviewing control-plane changes. For each "
    "change you are given its resource type, operation and the exact properties that changed "
    "(before -> after). Determine:\n"
    "- category: ONE of [" + ", ".join(CATEGORIES) + "].\n"
    "- summary: a precise, plain-English sentence of WHAT was actually done (name the concrete "
    "effect, e.g. 'Added an inbound NSG rule allowing TCP 3389 from the Internet (0.0.0.0/0)').\n"
    "- impact: the possible workload impact in one sentence (use 'could' for inferred impact).\n"
    "- why: one sentence on why this is risky or benign.\n"
    "- risk: an integer 0-100 (90-100 critical, 70-89 high, 40-69 medium, 10-39 low, 0-9 info).\n"
    "Be specific and factual; do NOT invent values not present in the diff. Return ONLY a JSON "
    "array of objects: [{\"i\": <index>, \"category\": \"...\", \"summary\": \"...\", "
    "\"impact\": \"...\", \"why\": \"...\", \"risk\": <int>}]."
)


def _compact_event(i: int, e: dict[str, Any]) -> dict[str, Any]:
    diffs = []
    for d in (e.get("details") or [])[:10]:
        diffs.append({"path": d.get("propertyPath", ""), "before": _trim(d.get("beforeValue")),
                      "after": _trim(d.get("afterValue"))})
    return {
        "i": i,
        "resourceType": e.get("resourceType", "") or "(unknown type)",
        "resourceName": e.get("resourceName", ""),
        "operation": e.get("operation", ""),
        "changes": diffs or "(no property-level diff available)",
    }


def _trim(v: Any) -> Any:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= 240 else s[:240] + "…"


def _select(events: list[dict[str, Any]]) -> list[int]:
    """Indices to send to the AI: every Unknown, plus the highest-risk known ones, up to the cap."""
    unknown = [i for i, e in enumerate(events) if e.get("category") in ("", "Unknown")]
    known = [i for i, e in enumerate(events) if e.get("category") not in ("", "Unknown")]
    known.sort(key=lambda i: -int(events[i].get("riskScore", 0)))
    picked = unknown + known
    seen: set[int] = set()
    out: list[int] = []
    for i in picked:
        if i not in seen:
            seen.add(i)
            out.append(i)
        if len(out) >= _MAX_EVENTS:
            break
    return out


async def _ask(provider: Any, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = "Analyze these Azure changes:\n" + json.dumps(batch, default=str)
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}], None
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        log.warning("AI enrich batch failed: %s", exc)
        return []
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("["):
        m = re.search(r"(\[.*\])", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = safe_json_parse(t, default=None)
    return parsed if isinstance(parsed, list) else []


async def enrich_stream(events: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Stream enrichment: yields progress dicts and finally an ``{"result": {idx: {...}}}`` map.

    The result maps event index -> {category, summary, impact, why, risk}. Empty when AI is
    unavailable (the caller then keeps the deterministic results)."""
    indices = _select(events)
    if not indices:
        yield {"result": {}}
        return

    try:
        from app.agent.factory import build_provider
        provider = build_provider()
    except Exception as exc:  # noqa: BLE001
        log.info("No AI provider for change enrichment: %s", exc)
        yield {"result": {}}
        return

    result: dict[int, dict[str, Any]] = {}
    batches = [indices[i:i + _BATCH] for i in range(0, len(indices), _BATCH)]
    total = len(batches)

    # Run the batches with bounded concurrency (_AI_CONCURRENCY parallel LLM calls) instead of
    # one-at-a-time — the AI step is the slowest phase, and the provider's HTTP client is safe to
    # call concurrently. Progress is yielded as each batch completes (order-independent).
    sem = asyncio.Semaphore(_AI_CONCURRENCY)

    async def _run_batch(batch_idx: list[int]) -> list[dict[str, Any]]:
        async with sem:
            payload = [_compact_event(j, events[j]) for j in batch_idx]
            try:
                return await asyncio.wait_for(_ask(provider, payload), timeout=_AI_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warning("AI enrich batch timed out")
                return []

    def _absorb(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            gi = row.get("i")
            if not isinstance(gi, int) or gi < 0 or gi >= len(events):
                continue
            cat = str(row.get("category", "")).strip()
            result[gi] = {
                "category": cat if cat in _VALID else "",
                "summary": str(row.get("summary", "")).strip(),
                "impact": str(row.get("impact", "")).strip(),
                "why": str(row.get("why", "")).strip(),
                "risk": int(row["risk"]) if isinstance(row.get("risk"), (int, float)) else None,
            }

    tasks = [asyncio.create_task(_run_batch(b)) for b in batches]
    try:
        yield {"phase": "ai",
               "message": f"AI analyzing changes… ({total} batch(es), {_AI_CONCURRENCY} in parallel)",
               "done": 0, "total": len(indices)}
        completed = 0
        for fut in asyncio.as_completed(tasks):
            _absorb(await fut)
            completed += 1
            yield {"phase": "ai", "message": f"AI analyzing changes… (batch {completed}/{total})",
                   "done": min(completed * _BATCH, len(indices)), "total": len(indices)}
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    yield {"result": result}
