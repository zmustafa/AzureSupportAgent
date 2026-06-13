"""Azure-backed Monitor datasources: Resource Graph, Log Analytics, Azure Monitor metrics.

Each resolver takes a widget ``dataSource`` config + the resolved Azure connection and
returns a normalized :class:`TableResult`. All are READ-ONLY.
"""
from __future__ import annotations

from typing import Any

from app.exec.command_runner import (
    KQL_MAX_ROWS,
    run_kql_capture,
    run_la_capture,
    run_metrics_capture,
)

from .base import Column, TableResult, parse_json_output, table_from_records


def _render(text: str, params: dict[str, Any]) -> str:
    """Interpolate {{key}} placeholders (dashboard params) into a query string."""
    if not text or not params:
        return text or ""
    out = text
    for k, v in params.items():
        out = out.replace("{{" + str(k) + "}}", "" if v is None else str(v))
    return out


async def resolve_resource_graph(
    cfg: dict[str, Any], conn: dict[str, Any] | None, params: dict[str, Any]
) -> TableResult:
    """Azure Resource Graph (KQL) query via `az graph query`."""
    query = _render(str(cfg.get("query") or "").strip(), params)
    if not query:
        return TableResult.from_error("No Resource Graph query provided.")
    cap = await run_kql_capture(query, conn, output="json")
    if not cap.ok:
        return TableResult.from_error(cap.error or "Resource Graph query failed.")
    data, perr = parse_json_output(cap.stdout)
    if perr:
        return TableResult.from_error(perr)
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
    if not isinstance(rows, list):
        return TableResult.from_error("Unexpected Resource Graph result shape.")
    res = table_from_records(rows, max_rows=KQL_MAX_ROWS)
    res.meta["source"] = "resource_graph"
    return res


async def resolve_log_analytics(
    cfg: dict[str, Any], conn: dict[str, Any] | None, params: dict[str, Any]
) -> TableResult:
    """Log Analytics KQL via `az monitor log-analytics query` against a workspace."""
    query = _render(str(cfg.get("query") or "").strip(), params)
    if not query:
        return TableResult.from_error("No Log Analytics query provided.")
    workspace = str(cfg.get("workspace_id") or "").strip() or (
        conn.get("log_analytics_workspace_id", "") if conn else ""
    )
    if not workspace:
        return TableResult.from_error(
            "No Log Analytics workspace id. Set one on the Azure connection or the widget."
        )
    timespan = str(cfg.get("timespan") or "P1D")
    cap = await run_la_capture(query, workspace, conn, timespan=timespan)
    if not cap.ok:
        return TableResult.from_error(cap.error or "Log Analytics query failed.")
    data, perr = parse_json_output(cap.stdout)
    if perr:
        return TableResult.from_error(perr)
    # The CLI returns a flat list of row dicts (it flattens tables/columns for us).
    if not isinstance(data, list):
        if isinstance(data, dict) and isinstance(data.get("tables"), list):
            return _table_from_la_tables(data["tables"])
        return TableResult.from_error("Unexpected Log Analytics result shape.")
    res = table_from_records(data, max_rows=1000)
    res.meta["source"] = "log_analytics"
    return res


def _table_from_la_tables(tables: list[dict[str, Any]]) -> TableResult:
    """Fallback parser for the raw {tables:[{columns,rows}]} LA response shape."""
    if not tables:
        return TableResult(meta={"source": "log_analytics"})
    t = tables[0]
    cols = t.get("columns") or []
    columns = [Column(name=c.get("name", f"c{i}"), type=_la_type(c.get("type"))) for i, c in enumerate(cols)]
    rows = [list(r) for r in (t.get("rows") or [])][:1000]
    return TableResult(columns=columns, rows=rows, meta={"source": "log_analytics", "row_count": len(rows)})


def _la_type(t: Any) -> str:
    s = str(t or "").lower()
    if s in ("int", "long", "real", "double", "decimal"):
        return "number"
    if s in ("datetime",):
        return "datetime"
    if s in ("bool", "boolean"):
        return "bool"
    return "string"


async def resolve_azure_metrics(
    cfg: dict[str, Any], conn: dict[str, Any] | None, params: dict[str, Any]
) -> TableResult:
    """Azure Monitor metrics via `az monitor metrics list`.

    Produces a time-series table: one ``timestamp`` column + one numeric column per
    metric. When multiple resources are given, columns are suffixed with the resource
    name so a chart can plot a series per resource.
    """
    resource_ids = cfg.get("resource_ids") or ([cfg["resource_id"]] if cfg.get("resource_id") else [])
    resource_ids = [_render(str(r), params) for r in resource_ids if r]
    if not resource_ids:
        return TableResult.from_error("No resource id(s) provided for metrics.")
    metrics = cfg.get("metrics") or ([cfg["metric"]] if cfg.get("metric") else [])
    metrics = [str(m) for m in metrics if m]
    if not metrics:
        return TableResult.from_error("No metric name(s) provided.")
    aggregation = str(cfg.get("aggregation") or "Average")
    # Optional: request several aggregation columns at once (``az`` supports it) so metrics
    # with different primary aggregations all come back populated.
    request_aggs = cfg.get("aggregations") or [aggregation]
    # Optional: per-metric preferred aggregation field (lowercased metric -> agg name), so a
    # count metric shows its Total/Count and a gauge shows its Average even in one chart.
    agg_by_metric = {str(k).lower(): str(v).lower() for k, v in (cfg.get("aggregation_by_metric") or {}).items()}
    interval = str(cfg.get("interval") or "PT5M")
    timespan = cfg.get("timespan") or None

    _AGG_FIELDS = ["average", "total", "maximum", "minimum", "count"]

    def _pick_value(pt: dict[str, Any], preferred: str) -> float | None:
        """Choose a datapoint's value, honoring the preferred aggregation. Uses explicit
        ``is not None`` checks so a legitimate 0.0 is NOT dropped (the old ``a or b`` chain
        silently discarded every zero-valued point — breaking idle/zero metrics)."""
        order = [preferred] + [f for f in _AGG_FIELDS if f != preferred]
        for f in order:
            v = pt.get(f)
            if v is not None:
                return float(v)
        return None

    # timestamp -> {series_name: value}
    buckets: dict[str, dict[str, float]] = {}
    series_names: list[str] = []
    multi = len(resource_ids) > 1
    for rid in resource_ids:
        cap = await run_metrics_capture(
            rid, metrics, conn, aggregation=request_aggs, interval=interval, timespan=timespan
        )
        if not cap.ok:
            return TableResult.from_error(cap.error or "Metrics query failed.")
        data, perr = parse_json_output(cap.stdout)
        if perr or not isinstance(data, dict):
            return TableResult.from_error(perr or "Unexpected metrics result shape.")
        short = rid.rsplit("/", 1)[-1] if multi else ""
        for m in data.get("value", []) or []:
            mname = (m.get("name", {}) or {}).get("value") or m.get("name") or "metric"
            preferred = agg_by_metric.get(str(mname).lower(), aggregation.lower())
            sname = f"{mname} · {short}" if short else str(mname)
            if sname not in series_names:
                series_names.append(sname)
            for ts in (m.get("timeseries") or []):
                for pt in (ts.get("data") or []):
                    t = pt.get("timeStamp") or pt.get("timestamp")
                    if not t:
                        continue
                    val = _pick_value(pt, preferred)
                    if val is None:
                        continue
                    buckets.setdefault(t, {})[sname] = val

    if not buckets:
        return TableResult(
            columns=[Column("timestamp", "datetime")],
            rows=[],
            meta={"source": "azure_metrics", "note": "No datapoints returned."},
        )
    columns = [Column("timestamp", "datetime")] + [Column(s, "number") for s in series_names]
    rows: list[list[Any]] = []
    for ts in sorted(buckets.keys()):
        row: list[Any] = [ts]
        for s in series_names:
            row.append(buckets[ts].get(s))
        rows.append(row)
    return TableResult(columns=columns, rows=rows[:2000], meta={"source": "azure_metrics", "series": series_names})
