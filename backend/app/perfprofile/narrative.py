"""One-paragraph AI narrative for a performance profile, grounded in the numbers.

Uses the shared provider; always falls back to a deterministic summary so the action
never fails."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.perfprofile.narrative")


def _fallback(snapshot: dict[str, Any]) -> str:
    sc = snapshot.get("scorecard", {})
    top = snapshot.get("top_bottleneck")
    parts = [
        f"Workload performance score {sc.get('workload_score', 100)}/100 across "
        f"{sc.get('resources_profiled', 0)} resource(s): {sc.get('breaching', 0)} breaching, "
        f"{sc.get('approaching', 0)} approaching their AMBA thresholds."
    ]
    if top:
        parts.append(
            f"Top bottleneck: {top['resource_name']} {top['metric_name']} at {top['observed']}{top['unit']} "
            f"({top['pct_of_threshold']}% of its {top['threshold']}{top['unit']} threshold)"
            + (f", trending {top['trend_pct']:+}% over the window." if top.get("trend_pct") else ".")
        )
    return " ".join(parts)


async def narrate(snapshot: dict[str, Any], *, sli_context: str = "") -> str:
    if not snapshot.get("resources"):
        return "No profilable resources in scope."
    try:
        from app.agent.factory import build_provider

        provider = build_provider()
    except Exception:  # noqa: BLE001
        return _fallback(snapshot)

    system = (
        "You are a performance engineer summarizing an Azure workload profile measured "
        "against Azure Monitor Baseline Alert (AMBA) thresholds. In 2-4 sentences: state the "
        "overall posture, name the binding bottleneck and how close it is to its threshold, "
        "note any worrying trends, and give one prioritized recommendation. Cite only numbers "
        "present in the data."
    )
    payload = {
        "scorecard": snapshot.get("scorecard", {}),
        "bottlenecks": snapshot.get("bottlenecks", [])[:8],
    }
    user = f"PROFILE:\n{json.dumps(payload)[:6000]}" + (f"\n\nWHAT NORMAL LOOKS LIKE:\n{sli_context}" if sli_context else "")
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
        return _fallback(snapshot)
    return text.strip() or _fallback(snapshot)
