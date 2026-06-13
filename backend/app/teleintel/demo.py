"""Dummy Telemetry Intelligence data for review/demo without a live App Insights.

Reproduces the spec scenario on the shared demo workload (demo-amba-coverage):
"3.2% → 41% failure on POST /order; 92% correlate to dependency sql-prod-eu timeouts;
began 14:05, 8 min after revision app-v412 deployed." Provides a 5-signal correlation
timeline, a ranked Smart Detection inbox, a sample transaction, an NL→KQL example, and
Code Optimizations. Marked demo everywhere; the API serves this instead of querying Azure
for the demo scope."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.amba.demo import DEMO_WORKLOAD_ID, DEMO_WORKLOAD_NAME, _RG, _SUB

DEMO_COMPONENT_ID = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/microsoft.insights/components/shop-appinsights"
DEMO_COMPONENT = {
    "id": DEMO_COMPONENT_ID,
    "name": "shop-appinsights",
    "app_id": "demo-app-0001",
    "resource_group": _RG,
    "subscription_id": _SUB,
    "location": "eastus",
    "workspace_id": "",
    "mode": "demo",
}
DEMO_OPERATION_ID = "a1b2c3d4e5f600112233445566778899"


def _base() -> datetime:
    # Anchor the scenario at "yesterday 13:30" UTC for a stable demo window.
    d = datetime.now(timezone.utc).replace(hour=13, minute=30, second=0, microsecond=0) - timedelta(days=1)
    return d


def _ts(minutes: int) -> str:
    return (_base() + timedelta(minutes=minutes)).isoformat()


def demo_timeline() -> dict[str, Any]:
    """A 5m-binned timeline where failure rate, p95 latency, exceptions and dependency
    failures all spike at 14:05 — 8 minutes after the app-v412 deploy at 13:57."""
    points: list[dict[str, Any]] = []
    for i in range(24):  # 13:30 → 15:25
        minute = i * 5
        spiking = minute >= 35  # 14:05 onward (35 min after 13:30)
        fail = 41.0 if spiking else 3.2
        points.append(
            {
                "timestamp": _ts(minute),
                "failure_rate_pct": fail if not (minute in (35, 40)) else (38.0 if minute == 35 else 41.0),
                "failed": int((fail / 100) * 480),
                "total": 480,
                "p95_ms": 2400.0 if spiking else 320.0,
                "p50_ms": 600.0 if spiking else 110.0,
                "exceptions": 190 if spiking else 6,
                "dep_failure_pct": 44.0 if spiking else 1.1,
                "failures": 210 if spiking else 5,
            }
        )
    return {
        "series_keys": ["failure_rate_pct", "p95_ms", "exceptions", "dep_failure_pct"],
        "points": points,
        "change_events": [
            {"timestamp": _ts(27), "change_type": "Update", "target": "shop-web-prod (app-v412)", "target_id": f"{DEMO_COMPONENT_ID}-web"},
        ],
        "bin_minutes": 5,
        "signal_count": 4,
        "notes": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "demo": True,
    }


def demo_triage() -> dict[str, Any]:
    summary = {
        "operation": "POST /order",
        "failure_rate_pct": 41.0,
        "failed": 197,
        "total": 480,
        "top_dependency": "sql-prod-eu",
        "top_dependency_type": "SQL",
        "dependency_correlation_pct": 92.0,
        "top_exception": "Microsoft.Data.SqlClient.SqlException",
        "probable_trigger": "Update on shop-web-prod (revision app-v412) at " + _ts(27),
        "trigger_target": f"{DEMO_COMPONENT_ID}-web",
        "change_count": 1,
    }
    evidence = [
        {
            "label": "Top failing operations",
            "kql": "requests | summarize total=count(), failed=countif(success==false) by operation_Name | extend failure_rate_pct=round(100.0*failed/total,2) | top 10 by failure_rate_pct desc",
            "rows": [
                {"operation_Name": "POST /order", "total": 480, "failed": 197, "failure_rate_pct": 41.0},
                {"operation_Name": "GET /cart", "total": 1200, "failed": 14, "failure_rate_pct": 1.2},
            ],
            "ok": True,
        },
        {
            "label": "Correlated failing dependencies",
            "kql": "let failed_reqs = requests | where operation_Name=='POST /order' | where success==false | project operation_Id; dependencies | where success==false | join kind=inner (failed_reqs) on operation_Id | summarize correlated=count() by target,type,resultCode | top 10 by correlated desc",
            "rows": [
                {"dependency_target": "sql-prod-eu", "dependency_type": "SQL", "dep_resultCode": "Timeout", "correlated": 181, "avg_dep_ms": 30000.0},
                {"dependency_target": "redis-prod", "dependency_type": "Redis", "dep_resultCode": "200", "correlated": 16, "avg_dep_ms": 4.0},
            ],
            "ok": True,
        },
        {
            "label": "Top exceptions",
            "kql": "exceptions | where operation_Name=='POST /order' | summarize occurrences=count() by problemId,type,outerMessage | top 10 by occurrences desc",
            "rows": [
                {"problemId": "SqlException at OrderRepository.SaveAsync", "type": "Microsoft.Data.SqlClient.SqlException", "outerMessage": "Connection Timeout Expired", "occurrences": 181},
            ],
            "ok": True,
        },
    ]
    hypothesis = (
        "POST /order failure rate jumped from 3.2% to 41% (197/480 requests) starting ~14:05. "
        "92% of the failures correlate to dependency sql-prod-eu (SQL) returning Timeout, with "
        "181 SqlException 'Connection Timeout Expired'. The spike began 8 minutes after revision "
        "app-v412 was deployed to shop-web-prod (13:57) — the probable trigger. Recommend rolling "
        "back app-v412 and checking the SQL connection-pool / command-timeout change it introduced."
    )
    return {
        "component": {"id": DEMO_COMPONENT_ID, "name": "shop-appinsights"},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "hypothesis": hypothesis,
        "evidence": evidence,
        "has_spike": True,
        "demo": True,
    }


def demo_smart_detection() -> dict[str, Any]:
    return {
        "items": [
            {"display_name": "Abnormal rise in failed request rate", "rule_name": "degradationindependencyduration", "severity": "error", "components": ["shop-appinsights"], "occurrences": 1},
            {"display_name": "Degradation in dependency duration", "rule_name": "degradationindependencyduration", "severity": "warning", "components": ["shop-appinsights", "shop-api-appinsights"], "occurrences": 2},
            {"display_name": "Slow page load time", "rule_name": "slowpageloadtime", "severity": "info", "components": ["shop-web-appinsights"], "occurrences": 1},
        ],
        "component_count": 3,
        "detection_count": 4,
        "note": "",
        "demo": True,
    }


def demo_transaction() -> dict[str, Any]:
    spans = [
        {"timestamp": _ts(36), "kind": "request", "name": "POST /order", "target": "", "result_code": "500", "duration_ms": 30240.0, "failed": True, "id": "req-1", "parent_id": ""},
        {"timestamp": _ts(36), "kind": "dependency", "name": "GET /cart/items", "target": "redis-prod", "result_code": "200", "duration_ms": 6.0, "failed": False, "id": "dep-1", "parent_id": "req-1"},
        {"timestamp": _ts(36), "kind": "dependency", "name": "INSERT Orders", "target": "sql-prod-eu", "result_code": "Timeout", "duration_ms": 30000.0, "failed": True, "id": "dep-2", "parent_id": "req-1"},
        {"timestamp": _ts(36), "kind": "exception", "name": "Microsoft.Data.SqlClient.SqlException", "target": "", "result_code": "3", "duration_ms": None, "failed": True, "id": "exc-1", "parent_id": "dep-2"},
    ]
    return {
        "ok": True,
        "operation_id": DEMO_OPERATION_ID,
        "kql": "union (requests | where operation_Id=='" + DEMO_OPERATION_ID + "'), (dependencies | ...), (exceptions | ...) | order by timestamp asc",
        "spans": spans,
        "total_ms": 30246.0,
        "failing_step": "INSERT Orders",
        "narration": (
            "The POST /order transaction took ~30.2s end-to-end. A Redis cart lookup returned in 6ms, "
            "but the INSERT Orders dependency to sql-prod-eu hit a 30s Timeout and threw "
            "SqlException 'Connection Timeout Expired' — the failure point. Nearly all the time was "
            "spent waiting on the SQL dependency."
        ),
        "demo": True,
    }


def demo_code_optimizations() -> dict[str, Any]:
    return {
        "items": [
            {"type": "CPU", "issue": "High CPU in OrderSerializer.Serialize (Newtonsoft)", "impact": "12% of CPU samples", "function": "OrderSerializer.Serialize"},
            {"type": "Memory", "issue": "Large object allocations in CartMapper.Map", "impact": "8% of allocations", "function": "CartMapper.Map"},
        ],
        "note": "",
        "demo": True,
    }


def demo_nlkql_example() -> dict[str, Any]:
    return {
        "question": "why were checkout requests slow yesterday afternoon?",
        "kql": (
            "requests | where operation_Name == 'POST /order' "
            "| summarize p95_ms = percentile(duration, 95), count() by bin(timestamp, 5m) "
            "| order by timestamp asc | take 200"
        ),
        "explanation": "p95 latency of POST /order in 5-minute buckets over the window.",
        "answer": (
            "Checkout (POST /order) p95 latency rose from ~320ms to ~2.4s starting around 14:05 "
            "yesterday, coinciding with SQL dependency timeouts to sql-prod-eu."
        ),
        "rows": [
            {"timestamp": _ts(30), "p95_ms": 318.0, "count_": 460},
            {"timestamp": _ts(40), "p95_ms": 2410.0, "count_": 480},
        ],
        "demo": True,
    }


def build_overview() -> dict[str, Any]:
    return {
        "scope_kind": "workload",
        "scope_id": DEMO_WORKLOAD_ID,
        "scope_name": DEMO_WORKLOAD_NAME,
        "components": [DEMO_COMPONENT],
        "sli_context": (
            "Critical thresholds: POST /order p95 < 800ms, failure rate < 2%. Critical dependency: "
            "sql-prod-eu (orders DB). Diagnostic hint: check SQL DTU + connection pool on order failures."
        ),
        "connection_configured": False,
        "source": "demo_dummy_data",
        "demo": True,
        "error": "",
    }


def is_demo_scope(scope_kind: str, scope_id: str) -> bool:
    from app.amba.demo import is_demo_scope as _is

    return _is(scope_kind, scope_id)


def ensure_demo() -> None:
    from app.amba.demo import ensure_demo_workload

    ensure_demo_workload()
