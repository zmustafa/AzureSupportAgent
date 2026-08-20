"""Recovery Readiness agent tools — read-only, cache-only.

Every response carries ``basis`` and ``confidence`` alongside the numbers, and a
``how_to_read`` note. That is not decoration: given a bare figure a model will restate it as
fact, and the whole point of this module is that a derived recovery time is not a fact until
a drill proves it. Handing the basis over is how the honesty contract survives the agent.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.resiliency import model, snapshot as snapshot_store

HOW_TO_READ = (
    "Every figure is DERIVED from configuration, not proven by a recovery drill. "
    "`rto_class: none` means no recovery path exists for that failure — it is worse than "
    "slow, not a degree of slow. `unknown` means a source could not be read and is NOT a "
    "claim that the resource is unprotected. Redundancy (zones, GRS, multi-region) does "
    "nothing for data_corruption or accidental_delete because it replicates the damage. "
    "Always quote the `basis` when stating a number."
)

# Only the two that answer the questions people actually ask are on by default; the combined
# catalog is already trimmed for request size, so every extra tool costs every turn.
TOOL_DEFAULTS: dict[str, bool] = {
    "recovery_posture": True,
    "recovery_gaps": True,
    "recovery_breaches": False,
}

#: Kept for callers that imported the old name.
DEFAULT_ENABLED = TOOL_DEFAULTS


def _enabled(settings: dict[str, Any], name: str) -> bool:
    configured = settings.get("resiliency_tools") or {}
    if isinstance(configured, dict) and name in configured:
        return bool(configured[name])
    return TOOL_DEFAULTS.get(name, False)


def _err(message: str) -> dict[str, Any]:
    return err(message)


def _json(payload: dict[str, Any], summary: str) -> dict[str, Any]:
    return ok(json.dumps(payload, default=str), summary)


def _snapshot(tenant_id: str, connection_id: str, workload_id: str) -> dict[str, Any]:
    return snapshot_store.read(tenant_id, connection_id, "workload", workload_id)


def _slim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"], "type": row["type"], "id": row["id"],
        "protection": row["protection"]["state"],
        "backup_frequency": row["protection"]["frequency"],
        "scenarios": {
            scenario: {
                "rpo_minutes": v["rpo_minutes"], "rpo_state": v["rpo_state"],
                "rto_class": v["rto_class"], "confidence": v["confidence"],
                "basis": [e["detail"] for e in v.get("basis", [])],
            }
            for scenario, v in row["verdicts"].items() if v.get("applicable")
        },
    }


def make_recovery_posture(tenant_id: str, principal: Any, connection_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not (getattr(principal, "is_admin", False) or principal.has("resiliency.read")):
            return _err("You do not have the 'resiliency.read' permission.")
        workload_id = str(args.get("workload") or "").strip()
        needle = str(args.get("resource") or "").strip().lower()
        if not workload_id:
            return _err("A workload is required.")

        snap = await asyncio.to_thread(_snapshot, tenant_id, connection_id, workload_id)
        if not snap.get("report_exists"):
            return _err("Recovery Readiness has not analyzed this workload yet. Nothing is "
                        "reported rather than reporting zeros.")
        rows = snap.get("resources") or []
        if needle:
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["id"].lower()]
            if not rows:
                return _err(f"No resource matching '{needle}' is in the current analysis.")
        return _json(
            {"generated_at": snap.get("generated_at"),
             "resources": [_slim(r) for r in rows[:60]],
             "how_to_read": HOW_TO_READ},
            f"Recovery posture for {len(rows)} resource(s)")

    return _handler


def make_recovery_gaps(tenant_id: str, principal: Any, connection_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not (getattr(principal, "is_admin", False) or principal.has("resiliency.read")):
            return _err("You do not have the 'resiliency.read' permission.")
        workload_id = str(args.get("workload") or "").strip()
        scenario = str(args.get("scenario") or "").strip()
        if not workload_id:
            return _err("A workload is required.")
        if scenario and scenario not in model.SCENARIOS:
            return _err(f"Unknown scenario. Valid values: {', '.join(model.SCENARIOS)}.")

        snap = await asyncio.to_thread(_snapshot, tenant_id, connection_id, workload_id)
        if not snap.get("report_exists"):
            return _err("Recovery Readiness has not analyzed this workload yet.")

        gaps: list[dict[str, Any]] = []
        for row in snap.get("resources") or []:
            for name, v in row["verdicts"].items():
                if scenario and name != scenario:
                    continue
                if v.get("applicable") and v.get("rto_class") == model.RTO_NONE:
                    gaps.append({"name": row["name"], "type": row["type"], "scenario": name,
                                 "why": [e["detail"] for e in v.get("basis", [])]})
        summary = snap.get("summary") or {}
        return _json(
            {"generated_at": snap.get("generated_at"), "no_recovery_path": gaps[:100],
             "counts": summary.get("by_scenario", {}),
             "undetermined_note": (
                 "Resources whose protection could not be read are reported as unknown and "
                 "are NOT included above."),
             "how_to_read": HOW_TO_READ},
            f"{len(gaps)} resource/scenario pair(s) with no recovery path")

    return _handler


def make_recovery_breaches(tenant_id: str, principal: Any, connection_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not (getattr(principal, "is_admin", False) or principal.has("resiliency.read")):
            return _err("You do not have the 'resiliency.read' permission.")
        workload_id = str(args.get("workload") or "").strip()
        if not workload_id:
            return _err("A workload is required.")
        snap = await asyncio.to_thread(_snapshot, tenant_id, connection_id, workload_id)
        if not snap.get("report_exists"):
            return _err("Recovery Readiness has not analyzed this workload yet.")
        rows = (snap.get("breaches") or [])[:100]
        return _json(
            {"generated_at": snap.get("generated_at"), "breaches": rows,
             "objectives_acknowledged": snap.get("targets_acknowledged", False),
             "how_to_read": HOW_TO_READ},
            f"{len(rows)} objective breach(es), worst consequence first")

    return _handler


def tool_specs(tenant_id: str, principal: Any, connection_id: str) -> list[tuple]:
    return [
        (
            "recovery_posture",
            "Per-scenario RTO and RPO for the resources in a workload, with the "
            "configuration that produced each answer. Answers 'what is our RTO for X, and "
            "what makes it that'.",
            {"type": "object",
             "properties": {"workload": {"type": "string"},
                            "resource": {"type": "string",
                                         "description": "Optional name or id filter."}},
             "required": ["workload"]},
            make_recovery_posture(tenant_id, principal, connection_id),
        ),
        (
            "recovery_gaps",
            "Resources with NO recovery path for a failure scenario — not slow, none. "
            "The most actionable output of Recovery Readiness.",
            {"type": "object",
             "properties": {"workload": {"type": "string"},
                            "scenario": {"type": "string", "enum": list(model.SCENARIOS)}},
             "required": ["workload"]},
            make_recovery_gaps(tenant_id, principal, connection_id),
        ),
        (
            "recovery_breaches",
            "Resources that miss their recovery objectives, ordered by consequence.",
            {"type": "object", "properties": {"workload": {"type": "string"}},
             "required": ["workload"]},
            make_recovery_breaches(tenant_id, principal, connection_id),
        ),
    ]


def build_recovery_tools(
    tenant_id: str, principal: Any, connection_id: str = "",
) -> list[ConnectorTool]:
    from app.core.app_settings import load_settings

    s = load_settings()
    return [
        ConnectorTool(name=n, description=d, parameters=p, kind="read", handler=h)
        for n, d, p, h in tool_specs(tenant_id, principal, connection_id)
        if _enabled(s, n)
    ]


def register_recovery_tools(
    toolset, *, tenant_id: str, principal: Any, connection: dict[str, Any] | None = None,
) -> None:
    """Add the Recovery Readiness tools to a connector toolset.

    Given the CALLER's principal, not the agent's: these re-check the same permission the
    HTTP routes check, so a question answered in chat is exactly as permissioned as the same
    question answered by clicking."""
    from app.core.app_settings import load_settings

    if not bool(load_settings().get("resiliency_tools_enabled", True)):
        return
    try:
        tools = build_recovery_tools(tenant_id, principal, str((connection or {}).get("id") or ""))
        if tools:
            toolset.add_connector({"tenant_id": tenant_id}, tools)
    except Exception:  # noqa: BLE001 - never let tool registration break a turn
        pass


__all__ = ["tool_specs", "build_recovery_tools", "register_recovery_tools", "HOW_TO_READ",
           "TOOL_DEFAULTS", "DEFAULT_ENABLED",
           "make_recovery_posture", "make_recovery_gaps", "make_recovery_breaches"]
