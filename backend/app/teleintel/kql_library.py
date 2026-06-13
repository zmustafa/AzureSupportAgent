"""Curated, parameterized, READ-ONLY KQL templates for App Insights telemetry.

These are the deterministic backbone of Telemetry Intelligence — the correlated joins that
are notoriously hard to write by hand (requests ↔ exceptions ↔ dependencies on
operation_Id, union-by-operation transaction reconstruction). They need no LLM; the AI
layer only narrates the results and drafts ad-hoc NL queries on top.

Every template is a single tabular query over the standard App Insights tables and ends
with a bounded ``take``/``top``. Placeholders are substituted with validated values."""
from __future__ import annotations

# Standard App Insights tables (the read-only allowlist shared with the NL→KQL validator).
ALLOWED_TABLES = (
    "requests",
    "dependencies",
    "exceptions",
    "traces",
    "customEvents",
    "customMetrics",
    "performanceCounters",
    "availabilityResults",
    "pageViews",
)


def failure_rate_timeseries(bin_minutes: int = 5) -> str:
    return (
        "requests "
        f"| summarize total=count(), failed=countif(success == false) by bin(timestamp, {bin_minutes}m) "
        "| extend failure_rate_pct = round(100.0 * failed / total, 2) "
        "| project timestamp, total, failed, failure_rate_pct "
        "| order by timestamp asc"
    )


def latency_p95_timeseries(bin_minutes: int = 5) -> str:
    return (
        "requests "
        f"| summarize p95_ms = percentile(duration, 95), p50_ms = percentile(duration, 50) by bin(timestamp, {bin_minutes}m) "
        "| project timestamp, p95_ms = round(p95_ms, 1), p50_ms = round(p50_ms, 1) "
        "| order by timestamp asc"
    )


def exception_volume_timeseries(bin_minutes: int = 5) -> str:
    return (
        "exceptions "
        f"| summarize exceptions = count() by bin(timestamp, {bin_minutes}m) "
        "| order by timestamp asc"
    )


def dependency_health_timeseries(bin_minutes: int = 5) -> str:
    return (
        "dependencies "
        f"| summarize calls=count(), failures=countif(success == false) by bin(timestamp, {bin_minutes}m) "
        "| extend dep_failure_pct = round(100.0 * failures / calls, 2) "
        "| project timestamp, calls, failures, dep_failure_pct "
        "| order by timestamp asc"
    )


def top_failing_operations(top: int = 10) -> str:
    return (
        "requests "
        "| summarize total=count(), failed=countif(success == false) by operation_Name "
        "| extend failure_rate_pct = round(100.0 * failed / total, 2) "
        "| where failed > 0 "
        f"| top {top} by failure_rate_pct desc"
    )


def correlated_failure_join(operation_name: str = "", top: int = 10) -> str:
    """The headline join: failing requests ↔ their exceptions ↔ their dependency calls,
    correlated by operation_Id, ranked by the dependency target most associated with the
    failures. This is the query most operators can't write."""
    op_filter = f"| where operation_Name == '{_esc(operation_name)}' " if operation_name else ""
    return (
        "let failed_reqs = requests "
        f"{op_filter}"
        "| where success == false "
        "| project operation_Id, op=operation_Name, req_duration=duration, resultCode; "
        "dependencies "
        "| where success == false "
        "| join kind=inner (failed_reqs) on operation_Id "
        "| summarize correlated=count(), avg_dep_ms=round(avg(duration),1) "
        "by dependency_target=target, dependency_type=type, dep_resultCode=resultCode "
        f"| top {top} by correlated desc"
    )


def exceptions_for_failures(operation_name: str = "", top: int = 10) -> str:
    op_filter = f"| where operation_Name == '{_esc(operation_name)}' " if operation_name else ""
    return (
        "exceptions "
        f"{op_filter}"
        "| summarize occurrences=count() by problemId, type, outerMessage "
        f"| top {top} by occurrences desc"
    )


def transaction_by_operation(operation_id: str) -> str:
    """Union-by-operation_Id end-to-end transaction reconstruction: every request,
    dependency, exception and trace that shares the operation_Id, ordered in time."""
    oid = _esc(operation_id)
    return (
        f"let oid = '{oid}'; "
        "union "
        "(requests | where operation_Id == oid | extend itemType='request', name=operation_Name, target='', resultCode=resultCode, success=success, duration=duration), "
        "(dependencies | where operation_Id == oid | extend itemType='dependency', name=name, target=target, resultCode=resultCode, success=success, duration=duration), "
        "(exceptions | where operation_Id == oid | extend itemType='exception', name=type, target='', resultCode=tostring(severityLevel), success=false, duration=real(null)), "
        "(traces | where operation_Id == oid | extend itemType='trace', name=message, target='', resultCode=tostring(severityLevel), success=true, duration=real(null)) "
        "| project timestamp, itemType, name, target, resultCode, success, duration, id, operation_ParentId "
        "| order by timestamp asc "
        "| take 500"
    )


def slow_operation_breakdown(operation_name: str, top: int = 10) -> str:
    """Where time went for a slow operation: its dependency calls ranked by total time."""
    op = _esc(operation_name)
    return (
        f"let op_ids = requests | where operation_Name == '{op}' | project operation_Id; "
        "dependencies "
        "| join kind=inner (op_ids) on operation_Id "
        "| summarize calls=count(), total_ms=round(sum(duration),0), avg_ms=round(avg(duration),1), p95_ms=round(percentile(duration,95),1) "
        "by target, type "
        f"| top {top} by total_ms desc"
    )


def _esc(val: str) -> str:
    """Escape a single-quoted KQL string literal."""
    return (val or "").replace("\\", "\\\\").replace("'", "\\'")
