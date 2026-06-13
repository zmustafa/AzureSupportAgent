"""Explain this transaction — end-to-end reconstruction by operation_Id.

Runs the union-by-operation_Id query, builds an ordered span list with per-step duration,
flags the failing step, and asks the LLM to narrate where time went and where it failed."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.teleintel import kql_library as KQL
from app.teleintel.resolver import run_component_kql

log = logging.getLogger("app.teleintel.transaction")


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _build_spans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for r in rows:
        success = r.get("success")
        is_fail = success is False or (str(success).lower() == "false") or r.get("itemType") == "exception"
        spans.append(
            {
                "timestamp": r.get("timestamp", ""),
                "kind": r.get("itemType", ""),
                "name": r.get("name", ""),
                "target": r.get("target", ""),
                "result_code": r.get("resultCode", ""),
                "duration_ms": round(_num(r.get("duration")), 1) if r.get("duration") is not None else None,
                "failed": bool(is_fail),
                "id": r.get("id", ""),
                "parent_id": r.get("operation_ParentId", ""),
            }
        )
    return spans


async def explain_transaction(
    component: dict[str, Any],
    operation_id: str,
    connection: dict[str, Any] | None,
    *,
    timespan: str = "P1D",
) -> dict[str, Any]:
    kql = KQL.transaction_by_operation(operation_id)
    res = await run_component_kql(component, kql, connection, timespan=timespan)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "Query failed."), "kql": kql, "spans": []}
    spans = _build_spans(res.get("rows", []) or [])
    if not spans:
        return {"ok": True, "operation_id": operation_id, "kql": kql, "spans": [], "narration": "No telemetry found for that operation_Id in the selected window.", "total_ms": 0, "failing_step": ""}

    total_ms = round(sum(s["duration_ms"] or 0 for s in spans if s["kind"] in ("request", "dependency")), 1)
    failing = next((s for s in spans if s["failed"]), None)
    narration = await _narrate(operation_id, spans, total_ms, failing)
    return {
        "ok": True,
        "operation_id": operation_id,
        "kql": kql,
        "spans": spans,
        "total_ms": total_ms,
        "failing_step": (failing or {}).get("name", "") if failing else "",
        "narration": narration,
    }


async def _narrate(operation_id: str, spans: list[dict[str, Any]], total_ms: float, failing: dict[str, Any] | None) -> str:
    from app.agent.factory import build_provider

    provider = build_provider()
    system = (
        "You narrate a distributed transaction reconstructed from Application Insights for an "
        "operator. In 2-4 sentences explain where time went (slowest spans) and where/why it "
        "failed (if it failed). Cite span names and durations from the data only."
    )
    user = f"operation_Id: {operation_id}\ntotal_ms: {total_ms}\nfailing_step: {(failing or {}).get('name', 'none')}\n\nspans (JSON):\n{json.dumps(spans)[:6000]}"
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=600,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception:  # noqa: BLE001
        slow = max(spans, key=lambda s: s["duration_ms"] or 0, default=None)
        parts = [f"Transaction spans {len(spans)} steps over ~{total_ms}ms."]
        if slow and slow.get("duration_ms"):
            parts.append(f"Slowest: {slow['name']} ({slow['duration_ms']}ms).")
        if failing:
            parts.append(f"Failed at: {failing['name']} ({failing.get('result_code', '')}).")
        return " ".join(parts)
    return text.strip()
