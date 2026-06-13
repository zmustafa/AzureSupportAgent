"""Static catalogs describing what Monitor can render and bind to.

These power the widget editor UI (data-source picker + visualization picker) and ground
the AI author so it only emits valid kinds/types. Kept in one place so frontend and AI
stay in sync via the ``/admin/monitor/datasources`` endpoint.
"""
from __future__ import annotations

from typing import Any

# Each datasource: kind + label + which config fields it needs + whether it hits Azure.
DATASOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "kind": "app_telemetry",
        "label": "App telemetry",
        "group": "Internal",
        "description": "This agent's own health: messages, tool calls, providers, posture, automations.",
        "azure": False,
        "fields": [
            {"key": "telemetry_key", "label": "Series", "type": "select", "required": True,
             "options": ["activity_24h", "activity_14d", "tool_status", "providers", "top_tools",
                         "top_chats", "tokens_by_model", "posture_pillars", "automations_status", "totals"]},
        ],
    },
    {
        "kind": "resource_graph",
        "label": "Azure Resource Graph (KQL)",
        "group": "Azure",
        "description": "KQL over all resources for inventory/posture. Great for counts and group-by.",
        "azure": True,
        "fields": [
            {"key": "connection_id", "label": "Connection", "type": "connection"},
            {"key": "query", "label": "KQL query", "type": "kql", "required": True,
             "placeholder": "Resources | summarize count() by type | order by count_ desc"},
        ],
    },
    {
        "kind": "log_analytics",
        "label": "Log Analytics (KQL)",
        "group": "Azure",
        "description": "Full KQL against a Log Analytics workspace (logs + time-series).",
        "azure": True,
        "fields": [
            {"key": "connection_id", "label": "Connection", "type": "connection"},
            {"key": "workspace_id", "label": "Workspace id (optional, else connection's)", "type": "text"},
            {"key": "query", "label": "KQL query", "type": "kql", "required": True,
             "placeholder": "AzureActivity | summarize count() by bin(TimeGenerated, 1h)"},
            {"key": "timespan", "label": "Timespan", "type": "select",
             "options": ["PT1H", "PT6H", "P1D", "P7D", "P30D"], "default": "P1D"},
        ],
    },
    {
        "kind": "azure_metrics",
        "label": "Azure Monitor metrics",
        "group": "Azure",
        "description": "Platform metrics (CPU, requests, latency…) for one or more resources.",
        "azure": True,
        "fields": [
            {"key": "connection_id", "label": "Connection", "type": "connection"},
            {"key": "resource_ids", "label": "Resource id(s)", "type": "text_list", "required": True},
            {"key": "metrics", "label": "Metric name(s)", "type": "text_list", "required": True,
             "placeholder": "Percentage CPU"},
            {"key": "aggregation", "label": "Aggregation", "type": "select",
             "options": ["Average", "Total", "Maximum", "Minimum", "Count"], "default": "Average"},
            {"key": "interval", "label": "Grain", "type": "select",
             "options": ["PT1M", "PT5M", "PT15M", "PT1H"], "default": "PT5M"},
        ],
    },
    {
        "kind": "web_ping",
        "label": "Website ping (HTTPS)",
        "group": "Synthetic",
        "description": "Probe an HTTPS URL: status, latency, TLS expiry, optional body assert. SSRF-guarded.",
        "azure": False,
        "fields": [
            {"key": "url", "label": "URL", "type": "text", "required": True, "placeholder": "https://example.com/health"},
            {"key": "method", "label": "Method", "type": "select", "options": ["GET", "HEAD"], "default": "GET"},
            {"key": "assert_status", "label": "Expected status (optional)", "type": "number"},
            {"key": "assert_body", "label": "Body must contain (optional)", "type": "text"},
            {"key": "sample_every_s", "label": "Background sample every (sec)", "type": "number", "default": 300},
        ],
    },
    {
        "kind": "tcp_ping",
        "label": "TCP ping",
        "group": "Synthetic",
        "description": "Probe host:port reachability + connect latency. SSRF-guarded.",
        "azure": False,
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True},
            {"key": "sample_every_s", "label": "Background sample every (sec)", "type": "number", "default": 300},
        ],
    },
    {
        "kind": "workbook_ref",
        "label": "Workbook (latest run)",
        "group": "Internal",
        "description": "Bind to a saved Workbook's most recent run (severity / number / extract).",
        "azure": False,
        "fields": [
            {"key": "workbook_id", "label": "Workbook", "type": "workbook", "required": True},
        ],
    },
    {
        "kind": "static",
        "label": "Static / inline data",
        "group": "Internal",
        "description": "Hand-entered rows (for notes, mockups, or markdown tables).",
        "azure": False,
        "fields": [
            {"key": "rows", "label": "Rows (JSON)", "type": "json"},
        ],
    },
    {
        "kind": "none",
        "label": "No data (clock / markdown)",
        "group": "Internal",
        "description": "Static widgets that need no data source.",
        "azure": False,
        "fields": [],
    },
]

# Widget types + which chart sub-types and which datasource kinds make sense.
WIDGET_CATALOG: list[dict[str, Any]] = [
    {"type": "stat", "label": "Stat / KPI", "icon": "123", "desc": "A big number with delta + sparkline."},
    {"type": "chart", "label": "Chart", "icon": "📈", "desc": "Line, area, bar, pie, donut, scatter — multi-series.",
     "chartTypes": ["line", "area", "bar", "stackedBar", "pie", "donut", "scatter"]},
    {"type": "table", "label": "Table", "icon": "▦", "desc": "Sortable, formatted columns."},
    {"type": "list", "label": "List", "icon": "≣", "desc": "Compact ranked / feed list."},
    {"type": "gauge", "label": "Gauge", "icon": "◑", "desc": "Single value vs. thresholds."},
    {"type": "availability", "label": "Availability", "icon": "🟢", "desc": "Uptime + latency for a ping target."},
    {"type": "map", "label": "Map", "icon": "🗺️", "desc": "Geo distribution by Azure region."},
    {"type": "markdown", "label": "Markdown", "icon": "✎", "desc": "Notes / runbook links (no data)."},
    {"type": "clock", "label": "Clock", "icon": "🕐", "desc": "World clock(s) for NOC walls (no data)."},
]

# Default size hints per widget type for the grid (cols of 12, rowHeight ~70px).
DEFAULT_SIZE: dict[str, dict[str, int]] = {
    "stat": {"w": 3, "h": 2},
    "chart": {"w": 6, "h": 4},
    "table": {"w": 6, "h": 5},
    "list": {"w": 4, "h": 5},
    "gauge": {"w": 3, "h": 3},
    "availability": {"w": 4, "h": 3},
    "map": {"w": 6, "h": 5},
    "markdown": {"w": 4, "h": 3},
    "clock": {"w": 3, "h": 2},
}
