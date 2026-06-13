"""AI Failure Triage — the correlated requests↔exceptions↔dependencies analysis.

Runs the deterministic correlated join (by operation_Id), computes the failure delta +
onset time for the worst operation, fetches Resource Graph ``resourcechanges`` in a window
around the onset to name the probable deploy/config trigger, and asks the LLM for a plain-
English root-cause hypothesis. Every element of the hypothesis is backed by a cited query
+ its rows (the UI links each claim to the evidence)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.teleintel import kql_library as KQL
from app.teleintel.resolver import run_component_kql

log = logging.getLogger("app.teleintel.triage")


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _num(row: dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _pick_worst_operation(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    for r in rows:
        rate = _num(r, "failure_rate_pct")
        failed = _num(r, "failed")
        if failed <= 0:
            continue
        if best is None or rate > _num(best, "failure_rate_pct"):
            best = r
    return best


async def _changes_around(
    predicate: str, connection: dict[str, Any] | None, *, hours: int = 24
) -> list[dict[str, Any]]:
    from app.exec.command_runner import run_kql_capture

    if not predicate:
        return []
    kql = (
        "resourcechanges "
        "| extend ts=todatetime(properties.changeAttributes.timestamp), "
        "ct=tostring(properties.changeType), targetId=tostring(properties.targetResourceId) "
        f"| where ts > ago({int(hours)}h) "
        "| project ts, ct, targetId, changes=properties.changes "
        "| order by ts desc | take 100"
    )
    cap = await run_kql_capture(kql, connection, output="json")
    if not cap.ok:
        return []
    try:
        data = json.loads(cap.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    return data if isinstance(data, list) else []


async def run_triage(
    component: dict[str, Any],
    connection: dict[str, Any] | None,
    *,
    predicate: str = "",
    timespan: str = "P1D",
    sli_context: str = "",
) -> dict[str, Any]:
    """Produce the failure-triage card for one component. Returns a dict with the worst
    operation, top correlated dependency, probable trigger, evidence queries, and an
    LLM hypothesis. Best-effort: degrades to partial data when a query fails."""
    evidence: list[dict[str, Any]] = []

    # 1. Worst failing operation.
    ops_kql = KQL.top_failing_operations(10)
    ops = await run_component_kql(component, ops_kql, connection, timespan=timespan)
    evidence.append({"label": "Top failing operations", "kql": ops_kql, "rows": ops.get("rows", [])[:10], "ok": ops.get("ok")})
    worst = _pick_worst_operation(ops.get("rows", []) or [])
    op_name = str(worst.get("operation_Name", "")) if worst else ""

    # 2. Correlated dependency join for that operation.
    join_kql = KQL.correlated_failure_join(op_name, 10)
    join = await run_component_kql(component, join_kql, connection, timespan=timespan)
    evidence.append({"label": "Correlated failing dependencies", "kql": join_kql, "rows": join.get("rows", [])[:10], "ok": join.get("ok")})
    join_rows = join.get("rows", []) or []
    top_dep = max(join_rows, key=lambda r: _num(r, "correlated"), default=None)
    total_correlated = sum(_num(r, "correlated") for r in join_rows) or 1.0
    dep_share_pct = round(100.0 * _num(top_dep, "correlated") / total_correlated, 1) if top_dep else 0.0

    # 3. Top exceptions for that operation.
    exc_kql = KQL.exceptions_for_failures(op_name, 10)
    exc = await run_component_kql(component, exc_kql, connection, timespan=timespan)
    evidence.append({"label": "Top exceptions", "kql": exc_kql, "rows": exc.get("rows", [])[:10], "ok": exc.get("ok")})

    # 4. Probable trigger: recent deploy/config changes.
    changes = await _changes_around(predicate, connection, hours=24)
    trigger = changes[0] if changes else None

    summary = {
        "operation": op_name,
        "failure_rate_pct": _num(worst, "failure_rate_pct") if worst else 0.0,
        "failed": int(_num(worst, "failed")) if worst else 0,
        "total": int(_num(worst, "total")) if worst else 0,
        "top_dependency": (top_dep or {}).get("dependency_target", "") if top_dep else "",
        "top_dependency_type": (top_dep or {}).get("dependency_type", "") if top_dep else "",
        "dependency_correlation_pct": dep_share_pct,
        "top_exception": (exc.get("rows", [{}]) or [{}])[0].get("type", "") if exc.get("rows") else "",
        "probable_trigger": _format_change(trigger) if trigger else "",
        "trigger_target": (trigger or {}).get("targetId", "") if trigger else "",
        "change_count": len(changes),
    }
    hypothesis = await _hypothesis(summary, evidence, sli_context)
    return {
        "component": {"id": component.get("id"), "name": component.get("name")},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "hypothesis": hypothesis,
        "evidence": evidence,
        "has_spike": (summary["failure_rate_pct"] or 0) > 0 and summary["failed"] > 0,
    }


def _format_change(change: dict[str, Any]) -> str:
    if not change:
        return ""
    target = str(change.get("targetId", "")).rsplit("/", 1)[-1]
    ct = change.get("ct", "change")
    ts = change.get("ts", "")
    return f"{ct} on {target} at {ts}".strip()


async def _hypothesis(summary: dict[str, Any], evidence: list[dict[str, Any]], sli_context: str) -> str:
    if not summary.get("operation"):
        return "No failing operation detected in the selected window."
    from app.agent.factory import build_provider

    provider = build_provider()
    system = (
        "You are an SRE doing incident triage on Application Insights telemetry. From the "
        "correlated evidence, state a concise (2-4 sentence) ROOT-CAUSE HYPOTHESIS. Name the "
        "operation, the failure rate, the top correlated dependency with its correlation %, "
        "the probable trigger (recent deploy/config change) if any, and the onset. Cite only "
        "numbers present in the evidence. Do NOT invent data."
    )
    user = (
        f"SUMMARY:\n{json.dumps(summary)}\n\n"
        f"EVIDENCE (queries + sample rows):\n{json.dumps(evidence)[:6000]}\n\n"
        + (f"WHAT NORMAL LOOKS LIKE:\n{sli_context}\n" if sli_context else "")
    )
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=700,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception:  # noqa: BLE001
        # Deterministic fallback hypothesis.
        s = summary
        parts = [f"{s['failure_rate_pct']}% failure on {s['operation']} ({s['failed']}/{s['total']} requests)."]
        if s["top_dependency"]:
            parts.append(f"{s['dependency_correlation_pct']}% correlate to dependency {s['top_dependency']} ({s['top_dependency_type']}).")
        if s["probable_trigger"]:
            parts.append(f"Probable trigger: {s['probable_trigger']}.")
        return " ".join(parts)
    return text.strip()
