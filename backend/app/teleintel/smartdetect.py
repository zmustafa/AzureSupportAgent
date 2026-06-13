"""Smart Detection aggregator.

Azure App Insights Smart Detection fires per-resource emails on the total failure rate and
"will not alert per API or application", so signals stay fragmented. This module pulls the
proactive-detection results across ALL of the workload's App Insights components into one
ranked, deduplicated inbox so the agent can reason over them together.

The proactive-detection feed is read via a gated ``az rest`` call to the App Insights
ProactiveDetection API; it degrades gracefully (returns an empty list + a note) when
command execution is disabled or the API returns nothing. Demo data always populates it."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("app.teleintel.smartdetect")

_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


async def _detectors_for_component(component: dict[str, Any], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.core.app_settings import load_settings
    from app.exec.command_runner import run_az_json_capture

    if not load_settings().get("command_execution_enabled"):
        return []
    comp_id = component.get("id") or ""
    if not comp_id:
        return []
    # ProactiveDetection configurations + last-fired state for the component.
    url = f"https://management.azure.com{comp_id}/ProactiveDetectionConfigs?api-version=2018-05-01-preview"
    cap = await run_az_json_capture(["rest", "--method", "get", "--url", url, "--output", "json"], connection, label="az rest proactive-detection")
    if not cap.ok:
        return []
    try:
        data = json.loads(cap.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    items = data if isinstance(data, list) else data.get("value", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for it in items:
        props = it.get("properties", it) if isinstance(it, dict) else {}
        if not props.get("enabled", True):
            continue
        out.append(
            {
                "component_id": comp_id,
                "component_name": component.get("name", ""),
                "rule_name": props.get("name") or it.get("name", ""),
                "display_name": props.get("ruleDefinitions", {}).get("displayName", "") if isinstance(props.get("ruleDefinitions"), dict) else props.get("name", ""),
                "severity": _map_severity(props.get("ruleDefinitions", {})),
                "enabled": True,
            }
        )
    return out


def _map_severity(rule_def: Any) -> str:
    name = ""
    if isinstance(rule_def, dict):
        name = str(rule_def.get("displayName", "")).lower()
    if "failure" in name or "exception" in name:
        return "error"
    if "performance" in name or "latency" in name:
        return "warning"
    return "info"


def dedupe_rank(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by (display_name, severity) and rank by severity then component count."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for d in detections:
        key = (str(d.get("display_name") or d.get("rule_name") or "").lower(), d.get("severity", "info"))
        g = grouped.get(key)
        if g is None:
            g = {**d, "components": [], "occurrences": 0}
            grouped[key] = g
        if d.get("component_name") and d["component_name"] not in g["components"]:
            g["components"].append(d["component_name"])
        g["occurrences"] += 1
    ranked = sorted(
        grouped.values(),
        key=lambda g: (_SEVERITY_RANK.get(g.get("severity", "info"), 3), -len(g["components"]), -g["occurrences"]),
    )
    return ranked


async def aggregate(components: list[dict[str, Any]], connection: dict[str, Any] | None) -> dict[str, Any]:
    """Aggregate Smart Detection across all components into one ranked inbox."""
    all_detections: list[dict[str, Any]] = []
    for c in components:
        all_detections.extend(await _detectors_for_component(c, connection))
    ranked = dedupe_rank(all_detections)
    note = ""
    if not ranked:
        note = (
            "No Smart Detection results available (requires command execution + configured "
            "App Insights proactive detection). Demo scope shows a populated inbox."
        )
    return {
        "items": ranked,
        "component_count": len(components),
        "detection_count": len(all_detections),
        "note": note,
    }
