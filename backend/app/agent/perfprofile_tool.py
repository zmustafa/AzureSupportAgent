"""Agent tool: run the Performance Profiler for a workload during an investigation.

Lets a (deep or normal) investigation launch the same AMBA-threshold performance profile the
Performance Profiler screen runs, then feed its scorecard + ranked bottlenecks back to the
model as evidence. The run is also saved to the profiler's run history, so it shows up on the
Performance Profiler screen afterwards.

Registered onto the per-turn ConnectorToolset (like ``vm_tools``); the connection + default
workload are bound at registration so the model can call it with no arguments.
"""
from __future__ import annotations

import logging
from typing import Any

from app.connectors.base import ConnectorTool, ConnectorToolset, err, ok

log = logging.getLogger("app.agent.perfprofile_tool")


def _summarize(snap: dict[str, Any]) -> str:
    """Render a profile snapshot into a compact, LLM-friendly evidence summary."""
    sc = snap.get("scorecard", {}) or {}
    lines: list[str] = [
        f"Performance profile for '{snap.get('scope_name') or snap.get('scope_id')}' "
        f"(window {snap.get('window', 'P1D')}{', demo data' if snap.get('demo') else ''}):",
        f"- Workload score: {sc.get('workload_score', 'n/a')}/100",
        f"- Resources profiled: {sc.get('resources_profiled', 0)} "
        f"({sc.get('breaching', 0)} breaching, {sc.get('approaching', 0)} approaching, "
        f"{sc.get('healthy', 0)} healthy)",
    ]
    top = snap.get("top_bottleneck") or {}
    if top:
        lines.append(
            f"- Top bottleneck: {top.get('resource_name')} "
            f"({top.get('resource_type')}) — {top.get('metric_name') or top.get('metric')} "
            f"is {top.get('state')} at {top.get('pct_of_threshold')}% of threshold "
            f"(observed {top.get('observed')}{top.get('unit', '')}, threshold "
            f"{top.get('threshold')}{top.get('unit', '')}, trend {top.get('trend_pct')}%)."
        )
    bottlenecks = snap.get("bottlenecks") or []
    if bottlenecks:
        lines.append(f"- Bottlenecks ({len(bottlenecks)}):")
        for b in bottlenecks[:8]:
            lines.append(
                f"  • {b.get('resource_name')} — {b.get('metric_name') or b.get('metric')}: "
                f"{b.get('state')} at {b.get('pct_of_threshold')}% of threshold "
                f"({b.get('severity')})"
            )
    else:
        lines.append("- No breaching or approaching metrics — all profiled resources are healthy.")
    if snap.get("error"):
        lines.append(f"- Note: {snap['error']}")
    return "\n".join(lines)


async def _run_performance_profile(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Profile a workload against AMBA performance thresholds and return a summary."""
    from app.perfprofile import demo
    from app.perfprofile.collector import profile_workload
    from app.workloads.registry import get_workload

    workload_id = str(args.get("workload_id") or config.get("workload_id") or "").strip()
    if not workload_id:
        return err("No workload in scope. Provide workload_id, or run the investigation against a workload.")

    timespan = str(args.get("window") or config.get("window") or "P1D").strip() or "P1D"
    interval = str(config.get("interval") or "PT15M").strip() or "PT15M"
    scan_cap = int(config.get("scan_cap") or 100)
    connection = config.get("connection")
    tenant_id = str(config.get("tenant_id") or "default")
    actor = str(config.get("actor") or "investigation")

    try:
        if demo.is_demo_scope("workload", workload_id):
            snap = demo.build_demo_snapshot(scope_id=workload_id)
            snap["window"] = timespan
        else:
            workload = get_workload(workload_id)
            if workload is None:
                return err(f"Workload '{workload_id}' not found.")
            snap = await profile_workload(
                connection,
                scope_kind="workload",
                scope_id=workload_id,
                workload=workload,
                timespan=timespan,
                interval=interval,
                scan_cap=scan_cap,
            )
    except Exception as exc:  # noqa: BLE001 - tool failures are reported, never crash the turn
        log.info("run_performance_profile failed: %s", exc)
        return err(f"Performance profile failed: {str(exc)[:300]}")

    # Persist to run history so it surfaces on the Performance Profiler screen.
    try:
        from app.perfprofile import runs

        runs.save_run(tenant_id, "workload", workload_id, snap, actor=actor)
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        log.info("run_performance_profile: save_run failed: %s", exc)

    return ok(_summarize(snap))


def _tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="run_performance_profile",
            description=(
                "Run the Performance Profiler for the in-scope Azure workload: it queries Azure "
                "Monitor metrics for every resource and evaluates them against the AMBA "
                "performance thresholds, returning a workload score, the ranked bottlenecks "
                "(breaching / approaching metrics), and the single worst bottleneck. Use this "
                "when investigating performance, latency, throughput, saturation, scaling, or "
                "reliability questions to get hard metric evidence. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workload_id": {
                        "type": "string",
                        "description": "Workload to profile (optional; defaults to the investigation's workload).",
                    },
                    "window": {
                        "type": "string",
                        "description": "ISO-8601 lookback window: PT1H, PT6H, P1D (default), P7D, P30D.",
                    },
                },
                "required": [],
            },
            kind="read",
            handler=_run_performance_profile,
        )
    ]


def register_profiler_tool(
    toolset: ConnectorToolset,
    *,
    workload_id: str | None,
    connection: dict[str, Any] | None,
    tenant_id: str = "",
    actor: str = "",
    window: str = "P1D",
) -> None:
    """Add the ``run_performance_profile`` tool to a toolset, bound to the turn's workload."""
    from app.core.app_settings import load_settings

    if not bool(load_settings().get("perfprofile_tool_enabled", True)):
        return
    config = {
        "workload_id": workload_id or "",
        "connection": connection,
        "tenant_id": tenant_id,
        "actor": actor,
        "window": window,
    }
    toolset.add_connector(config, _tools())
