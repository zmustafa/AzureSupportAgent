"""Shared execution/persistence service for Performance Profiler attempts.

Every entry point uses this service so failed/partial attempts have the same contract:
failures are retained in history for diagnosis, but only complete successes replace the cache
or add a trend point.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.perfprofile import cache, demo, runs

log = logging.getLogger("app.perfprofile.service")


def _log_count(value: Any) -> int:
    """Constrain collection telemetry to an integer before it enters text logs."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _demo_collection(snapshot: dict[str, Any]) -> dict[str, Any]:
    cells = [cell for row in snapshot.get("resources", []) for cell in row.get("cells", [])]
    return {
        "status": "succeeded",
        "resources_discovered": len(snapshot.get("all_resources") or snapshot.get("resources") or []),
        "resources_eligible": len(snapshot.get("resources") or []),
        "resources_selected": len(snapshot.get("resources") or []),
        "resources_completed": len(snapshot.get("resources") or []),
        "scan_cap_reached": False,
        "metric_checks_total": len(cells),
        "metric_checks_succeeded": sum(1 for cell in cells if cell.get("state") != "no_data"),
        "metric_checks_no_data": sum(1 for cell in cells if cell.get("state") == "no_data"),
        "metric_checks_failed": 0,
        "metric_requests_total": 0,
        "metric_request_attempts": 0,
        "metric_requests_succeeded": 0,
        "metric_requests_failed": 0,
        "metric_requests_retried": 0,
        "metric_requests_throttled": 0,
        "metric_requests_timed_out": 0,
        "completeness_pct": 100,
        "errors": [],
    }


def _failed_snapshot(
    *,
    scope_kind: str,
    scope_id: str,
    scope_name: str,
    connection_configured: bool,
    window: str,
    start_time: str,
    end_time: str,
    interval: str,
    message: str,
    code: str,
) -> dict[str, Any]:
    from app.perfprofile.collector import _empty

    snap = _empty(scope_kind, scope_id, error=message)
    snap.update(
        {
            "scope_name": scope_name or scope_id,
            "connection_configured": connection_configured,
            "window": f"{start_time} → {end_time}" if start_time and end_time else window,
            "requested_window": "" if start_time and end_time else window,
            "requested_start": start_time,
            "requested_end": end_time,
            "interval": interval,
        }
    )
    snap["collection"]["errors"] = [{"code": code, "message": message[:1000]}]
    return snap


async def execute_profile(
    *,
    tenant_id: str,
    actor: str,
    scope_kind: str,
    scope_id: str,
    connection: dict[str, Any] | None,
    workload: dict[str, Any] | None,
    window: str,
    interval: str,
    scan_cap: int,
    start_time: str = "",
    end_time: str = "",
    progress=None,
    sli_context: str = "",
    trigger: str = "manual",
) -> dict[str, Any]:
    """Run and persist one attempt, returning the stored run with a terminal status."""
    from app.core.app_settings import load_settings
    from app.perfprofile.collector import profile_workload
    from app.perfprofile.narrative import narrate

    tenant = tenant_id or "default"
    scope_name = str((workload or {}).get("name") or scope_id)
    timeout_s = max(
        60, int(load_settings().get("perfprofile_workload_timeout_s", 1200) or 1200)
    )
    try:
        if demo.is_demo_scope(scope_kind, scope_id):
            snap = demo.build_demo_snapshot(scope_id=scope_id)
            if start_time and end_time:
                snap["window"] = f"{start_time} → {end_time}"
                snap["requested_start"] = start_time
                snap["requested_end"] = end_time
                snap["requested_window"] = ""
            else:
                snap["window"] = window
                snap["requested_window"] = window
            snap.update(
                {
                    "status": "succeeded",
                    "warning": "",
                    "error": "",
                    "collection": _demo_collection(snap),
                }
            )
            if progress is not None:
                for resource in snap.get("resources", []):
                    await progress(
                        resource.get("resource_name", ""), resource.get("resource_type", "")
                    )
        else:
            async with asyncio.timeout(timeout_s):
                snap = await profile_workload(
                    connection,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    workload=workload,
                    timespan=window,
                    interval=interval,
                    scan_cap=scan_cap,
                    start_time=start_time,
                    end_time=end_time,
                    progress=progress,
                )
    except TimeoutError:
        message = f"Performance profile timed out after {timeout_s} seconds."
        snap = _failed_snapshot(
            scope_kind=scope_kind,
            scope_id=scope_id,
            scope_name=scope_name,
            connection_configured=connection is not None,
            window=window,
            start_time=start_time,
            end_time=end_time,
            interval=interval,
            message=message,
            code="workload_timeout",
        )
    except Exception as exc:  # noqa: BLE001 - persist a terminal attempt; log the traceback
        # Do not put request-derived scope identifiers into exception logs. The persisted run
        # carries the complete scope context; the traceback here is only for engineering triage.
        log.exception("Performance profile attempt failed")
        message = str(exc)[:1000] or "Unexpected performance profile failure."
        snap = _failed_snapshot(
            scope_kind=scope_kind,
            scope_id=scope_id,
            scope_name=scope_name,
            connection_configured=connection is not None,
            window=window,
            start_time=start_time,
            end_time=end_time,
            interval=interval,
            message=message,
            code="unexpected_failure",
        )

    snap = dict(snap)
    status = str(snap.get("status") or (snap.get("collection") or {}).get("status") or "succeeded")
    snap["status"] = status
    snap["trigger"] = trigger
    if status != "failed":
        snap["narrative"] = await narrate(snap, sli_context=sli_context)
    else:
        snap["narrative"] = "Profile collection failed; no performance conclusion was produced."

    # A failed or incomplete attempt is valuable diagnostic history, but never trusted current
    # posture.  Only a complete success replaces cache and contributes to trends.
    if status == "succeeded":
        cache.write_snapshot(tenant, scope_kind, scope_id, snap)
    stored = runs.save_run(
        tenant,
        scope_kind,
        scope_id,
        snap,
        actor=actor,
        record_trend=status == "succeeded",
    )
    collection = snap.get("collection") or {}
    log.info(
        "Performance profile terminal resources=%s/%s metric_checks=%s/%s "
        "failed=%s requests=%s attempts=%s "
        "throttled=%s timed_out=%s",
        _log_count(collection.get("resources_completed", 0)),
        _log_count(collection.get("resources_selected", 0)),
        _log_count(collection.get("metric_checks_succeeded", 0))
        + _log_count(collection.get("metric_checks_no_data", 0)),
        _log_count(collection.get("metric_checks_total", 0)),
        _log_count(collection.get("metric_checks_failed", 0)),
        _log_count(collection.get("metric_requests_total", 0)),
        _log_count(collection.get("metric_request_attempts", 0)),
        _log_count(collection.get("metric_requests_throttled", 0)),
        _log_count(collection.get("metric_requests_timed_out", 0)),
    )
    return stored or snap
