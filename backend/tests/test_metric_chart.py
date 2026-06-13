"""Tests for the azure_metrics chart tool, the chart artifact store, and the GET endpoint.

The tool is READ-ONLY: it fetches an Azure Monitor time-series and hands the chat a
``chart_id`` (inside a ```azchart fenced block). These tests stub the Azure datasource so
no real ``az`` call is made, and verify the round-trip: tool → store → endpoint.
"""
import asyncio
import json
import re
from datetime import timedelta

import pytest

import app.agent.builtins as b
import app.monitor.chart_store as store
from app.api.charts import read_chart


class _FakeTable:
    """Stand-in for monitor TableResult — only ``to_dict()`` is used by the tool."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


def _sample_table():
    return {
        "columns": [
            {"name": "timestamp", "type": "datetime"},
            {"name": "Percentage CPU", "type": "number"},
        ],
        "rows": [
            ["2026-06-10T00:00:00Z", 12.5],
            ["2026-06-10T01:00:00Z", 80.0],
            ["2026-06-10T02:00:00Z", 41.0],
        ],
        "meta": {"source": "azure_metrics"},
        "error": "",
    }


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the chart store at a temp file so tests never touch backend/.data."""
    monkeypatch.setattr(store, "_PATH", tmp_path / "chart_artifacts.json")


@pytest.fixture(autouse=True)
def _no_live_metric_defs(monkeypatch):
    """Default: no live metric-definitions call (so tests never hit `az`). Individual
    tests can override by patching app.monitor.metric_defs.get_metric_definitions."""
    async def _empty(_rid, _conn):
        return []

    monkeypatch.setattr("app.monitor.metric_defs.get_metric_definitions", _empty)


# --- pure helpers ---------------------------------------------------------------------
def test_parse_lookback_iso_and_friendly():
    assert b._parse_lookback("PT1H") == timedelta(hours=1)
    assert b._parse_lookback("P7D") == timedelta(days=7)
    assert b._parse_lookback("P1DT6H") == timedelta(days=1, hours=6)
    assert b._parse_lookback("24h") == timedelta(hours=24)
    assert b._parse_lookback("90m") == timedelta(minutes=90)
    # Unknown / empty falls back to 1 day; never returns a non-positive delta.
    assert b._parse_lookback("") == timedelta(days=1)
    assert b._parse_lookback("garbage") == timedelta(days=1)
    # Capped at the 93-day metrics retention ceiling.
    assert b._parse_lookback("P3650D") == timedelta(days=93)


def test_auto_interval_scales_with_window():
    # Only universally-supported grains (PT5M/PT1H/P1D) — no PT1M/PT15M/PT6H which some
    # metric sets (e.g. App Service Http5xx) reject.
    assert b._auto_interval(timedelta(hours=1)) == "PT5M"
    assert b._auto_interval(timedelta(hours=6)) == "PT5M"
    assert b._auto_interval(timedelta(days=1)) == "PT1H"
    assert b._auto_interval(timedelta(days=7)) == "PT1H"
    assert b._auto_interval(timedelta(days=30)) == "P1D"


def test_iso_from_azure_grain():
    assert b._iso_from_azure_grain("00:05:00") == "PT5M"
    assert b._iso_from_azure_grain("01:00:00") == "PT1H"
    assert b._iso_from_azure_grain("1.00:00:00") == "P1D"
    assert b._iso_from_azure_grain("00:15:00") == "PT15M"
    assert b._iso_from_azure_grain("garbage") == ""
    # Sub-minute grains: the seconds component must be honored (was dropped before).
    assert b._iso_from_azure_grain("00:05:30") == "PT330S"  # 5m30s
    assert b._iso_from_azure_grain("00:00:30") == "PT30S"
    assert b._iso_from_azure_grain("00:00:00") == ""  # zero grain
    assert b._iso_from_azure_grain("00:05:30:99") == ""  # too many fields


def test_supported_grains_from_error():
    msg = (
        "ERROR: (BadRequest) Commonly allowed time grains: 00:05:00,01:00:00,1.00:00:00 "
        "between metrics: Http5xx,HttpResponseTime"
    )
    assert b._supported_grains_from_error(msg) == ["PT5M", "PT1H", "P1D"]
    assert b._supported_grains_from_error("some unrelated error") == []


def test_pick_grain_keeps_chart_readable():
    # 1-day window: PT5M (288 pts) is under 500 → finest acceptable.
    assert b._pick_grain(timedelta(days=1), ["PT5M", "PT1H", "P1D"]) == "PT5M"
    # 30-day window: PT5M (8640 pts) too many, PT1H (720) too many → P1D.
    assert b._pick_grain(timedelta(days=30), ["PT5M", "PT1H", "P1D"]) == "P1D"


def test_arm_type_of():
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    assert b._arm_type_of(rid) == "microsoft.web/sites"
    assert b._arm_type_of("not-a-resource-id") == ""
    assert b._arm_type_of("") == ""


def test_amba_metrics_for(monkeypatch):
    fake = {
        "alerts": [
            {"signal": "metric", "metric": "Percentage CPU"},
            {"signal": "log", "metric": ""},
            {"signal": "metric", "metric": "Available Memory Bytes"},
            {"signal": "metric", "metric": "Percentage CPU"},  # dup, ignored
        ]
    }
    monkeypatch.setattr("app.amba.reference.reference_for_type", lambda t: fake)
    assert b._amba_metrics_for("microsoft.compute/virtualmachines") == [
        "Percentage CPU",
        "Available Memory Bytes",
    ]
    assert b._amba_metrics_for("") == []


def test_summarize_series_reports_peak():
    s = b._summarize_series(_sample_table(), "%")
    assert "Percentage CPU" in s
    assert "max 80.00 %" in s
    assert "2026-06-10T01:00:00Z" in s  # peak timestamp


def test_summarize_series_skips_non_finite_values():
    # NaN/Inf datapoints (rare, from bad metric data) must be filtered, not formatted
    # into the summary — and an all-non-finite series yields the friendly fallback.
    mixed = {
        "columns": [{"name": "timestamp", "type": "datetime"}, {"name": "m", "type": "number"}],
        "rows": [["t0", float("nan")], ["t1", 5.0], ["t2", float("inf")]],
    }
    s = b._summarize_series(mixed, "")
    assert "nan" not in s.lower() and "inf" not in s.lower()
    assert "avg 5.00" in s  # only the finite point counts
    all_bad = {
        "columns": [{"name": "timestamp"}, {"name": "m"}],
        "rows": [["t0", float("nan")]],
    }
    assert b._summarize_series(all_bad, "") == "No numeric datapoints."


# --- the tool ------------------------------------------------------------------------
def _patch_azure(monkeypatch, table):
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda cid=None: {})

    async def _fake_resolve(cfg, conn, params):
        return _FakeTable(table)

    monkeypatch.setattr("app.monitor.datasources.azure.resolve_azure_metrics", _fake_resolve)


def _extract_chart_id(text):
    block = re.search(r"```azchart\s*(\{.*?\})\s*```", text, re.DOTALL)
    assert block, f"no azchart block in tool output:\n{text}"
    return json.loads(block.group(1))


def test_azure_metrics_tool_round_trip(monkeypatch):
    _patch_azure(monkeypatch, _sample_table())
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    out = asyncio.run(
        b._azure_metrics(
            {},
            {"resource_id": rid, "metrics": ["Percentage CPU"], "timespan": "P1D", "chart_type": "line"},
        )
    )
    assert out["isError"] is False
    text = out["content"][0]
    spec = _extract_chart_id(text)
    assert spec["type"] == "line"  # explicit chart_type is honored
    # The id resolves in the store and carries the same series.
    art = store.get_chart(spec["chart_id"])
    assert art is not None
    assert art["result"]["rows"] == _sample_table()["rows"]
    assert art["spec"]["metrics"] == ["Percentage CPU"]


def test_auto_chart_type_picks_by_data_shape():
    def tbl(npts, nseries):
        cols = [{"name": "timestamp", "type": "datetime"}] + [
            {"name": f"m{i}", "type": "number"} for i in range(nseries)
        ]
        rows = [[f"2026-06-12T0{i % 10}:00:00Z"] + [float(i)] * nseries for i in range(npts)]
        return {"columns": cols, "rows": rows}

    # Sparse (<=3 points) → bar regardless of metric.
    assert b._auto_chart_type(["Percentage CPU"], "Average", tbl(1, 1)) == "bar"
    # Count/total metrics → bar even when dense.
    assert b._auto_chart_type(["Http5xx"], "Total", tbl(100, 1)) == "bar"
    assert b._auto_chart_type(["Requests"], "Average", tbl(50, 1)) == "bar"
    # Single continuous series → area.
    assert b._auto_chart_type(["MemoryWorkingSet"], "Average", tbl(12, 1)) == "area"
    # Multiple continuous series → line.
    assert b._auto_chart_type(["Percentage CPU", "MemoryWorkingSet"], "Average", tbl(288, 2)) == "line"


def test_azure_metrics_auto_selects_chart_type(monkeypatch):
    """Without an explicit chart_type, the tool varies the type by data shape."""
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"

    # Dense single continuous series → area.
    dense = {
        "columns": [{"name": "timestamp", "type": "datetime"}, {"name": "MemoryWorkingSet", "type": "number"}],
        "rows": [[f"2026-06-12T{h:02d}:00:00Z", 100.0 + h] for h in range(12)],
        "meta": {},
        "error": "",
    }
    _patch_azure(monkeypatch, dense)
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["MemoryWorkingSet"]}))
    assert _extract_chart_id(out["content"][0])["type"] == "area"

    # Count metric → bar.
    _patch_azure(monkeypatch, dense)  # rows shape irrelevant; metric name drives it
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["Http5xx"], "aggregation": "Total"}))
    assert _extract_chart_id(out["content"][0])["type"] == "bar"


def test_azure_metrics_honors_explicit_pie_and_donut(monkeypatch):
    """Explicit chart_type pie/donut is passed through to the chart spec."""
    _patch_azure(monkeypatch, _sample_table())
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    for kind in ("pie", "donut", "line", "area", "bar"):
        out = asyncio.run(
            b._azure_metrics({}, {"resource_id": rid, "metrics": ["Http5xx"], "chart_type": kind})
        )
        assert _extract_chart_id(out["content"][0])["type"] == kind
    # An unknown chart_type falls back to auto-selection (never errors on it).
    out = asyncio.run(
        b._azure_metrics({}, {"resource_id": rid, "metrics": ["Http5xx"], "chart_type": "bogus"})
    )
    assert _extract_chart_id(out["content"][0])["type"] in ("line", "area", "bar")


def test_azure_metrics_defaults_metrics_from_amba(monkeypatch):
    _patch_azure(monkeypatch, _sample_table())
    monkeypatch.setattr(
        "app.amba.reference.reference_for_type",
        lambda t: {"alerts": [{"signal": "metric", "metric": "Percentage CPU"}]},
    )
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/web1"
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid}))
    assert out["isError"] is False
    art = store.get_chart(_extract_chart_id(out["content"][0])["chart_id"])
    assert art["spec"]["metrics"] == ["Percentage CPU"]


def test_azure_metrics_retries_on_unsupported_grain(monkeypatch):
    """Azure rejects the first grain with an allowed-grains hint → tool retries + succeeds.

    Reproduces the App Service ``Http5xx``/``HttpResponseTime`` case where PT15M is rejected
    but PT5M/PT1H/P1D are allowed.
    """
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda cid=None: {})
    grain_err = {
        "columns": [],
        "rows": [],
        "meta": {},
        "error": "ERROR: (BadRequest) Commonly allowed time grains: 00:05:00,01:00:00,1.00:00:00",
    }
    seen: list[str] = []

    async def _fake_resolve(cfg, conn, params):
        seen.append(cfg["interval"])
        # Fail the first attempt (PT15M); succeed once a supported grain is used.
        if cfg["interval"] not in ("PT5M", "PT1H", "P1D"):
            return _FakeTable(grain_err)
        return _FakeTable(_sample_table())

    monkeypatch.setattr("app.monitor.datasources.azure.resolve_azure_metrics", _fake_resolve)
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    # Force the bad first grain explicitly so we exercise the retry path deterministically.
    out = asyncio.run(
        b._azure_metrics({}, {"resource_id": rid, "metrics": ["Http5xx"], "interval": "PT15M"})
    )
    assert out["isError"] is False, out["content"][0]
    assert seen[0] == "PT15M" and seen[-1] in ("PT5M", "PT1H", "P1D")
    art = store.get_chart(_extract_chart_id(out["content"][0])["chart_id"])
    assert art["spec"]["interval"] in ("PT5M", "PT1H", "P1D")


def test_azure_metrics_requires_resource_id(monkeypatch):
    out = asyncio.run(b._azure_metrics({}, {"metrics": ["Percentage CPU"]}))
    assert out["isError"] is True
    assert "resource_id" in out["content"][0]


def test_azure_metrics_error_suggests_amba_metrics(monkeypatch):
    bad = {"columns": [], "rows": [], "meta": {}, "error": "Metric 'Foo' not found"}
    _patch_azure(monkeypatch, bad)
    monkeypatch.setattr(
        "app.amba.reference.reference_for_type",
        lambda t: {"alerts": [{"signal": "metric", "metric": "Percentage CPU"}]},
    )
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["Foo"]}))
    assert out["isError"] is True
    assert "Percentage CPU" in out["content"][0]  # suggestion included


# --- store + endpoint ----------------------------------------------------------------
def test_chart_store_round_trip_and_missing():
    cid = store.save_chart({"title": "t"}, _sample_table())
    art = store.get_chart(cid)
    assert art and art["spec"]["title"] == "t"
    assert store.get_chart("does-not-exist") is None


def test_chart_store_evicts_oldest(monkeypatch):
    monkeypatch.setattr(store, "_MAX_ARTIFACTS", 3)
    ids = [store.save_chart({"n": i}, _sample_table()) for i in range(5)]
    # Oldest two evicted, newest three retained.
    assert store.get_chart(ids[0]) is None
    assert store.get_chart(ids[1]) is None
    assert store.get_chart(ids[-1]) is not None


def test_read_chart_endpoint():
    cid = store.save_chart({"title": "ep"}, _sample_table())
    payload = asyncio.run(read_chart(cid, _principal=object()))
    assert payload["spec"]["title"] == "ep"
    assert payload["result"]["rows"] == _sample_table()["rows"]
    with pytest.raises(Exception):
        asyncio.run(read_chart("missing", _principal=object()))


# --- resolver: zero-value retention + per-metric aggregation --------------------------
def _fake_capture(payload):
    from app.exec.command_runner import CaptureResult

    return CaptureResult(ok=True, stdout=json.dumps(payload))


def _az_metrics_payload(metric_to_points):
    """Build a raw `az monitor metrics list` JSON response from {metric: [point,...]}."""
    return {
        "value": [
            {
                "name": {"value": name},
                "timeseries": [{"data": points}],
            }
            for name, points in metric_to_points.items()
        ]
    }


def test_resolve_keeps_zero_datapoints(monkeypatch):
    """Regression: a legitimate 0.0 datapoint must NOT be dropped (the old `a or b` chain
    discarded every zero, breaking idle/zero metrics like ServerErrors=0)."""
    from app.monitor.datasources import azure as az

    payload = _az_metrics_payload(
        {"ServerErrors": [
            {"timeStamp": "2026-06-10T00:00:00Z", "total": 0.0},
            {"timeStamp": "2026-06-10T01:00:00Z", "total": 0.0},
        ]}
    )

    async def _fake(*a, **k):
        return _fake_capture(payload)

    monkeypatch.setattr(az, "run_metrics_capture", _fake)
    res = asyncio.run(
        az.resolve_azure_metrics(
            {"resource_id": "/x", "metrics": ["ServerErrors"], "aggregation": "Total"}, None, {}
        )
    )
    d = res.to_dict()
    assert d["error"] == ""
    assert len(d["rows"]) == 2  # both zero points retained
    assert d["rows"][0][1] == 0.0


def test_resolve_per_metric_aggregation(monkeypatch):
    """Each metric reads its own preferred aggregation column (gauge→average, count→total)."""
    from app.monitor.datasources import azure as az

    payload = _az_metrics_payload(
        {
            "cpu_percent": [{"timeStamp": "2026-06-10T00:00:00Z", "average": 42.0, "total": 999.0}],
            "connection_failed": [{"timeStamp": "2026-06-10T00:00:00Z", "average": 0.1, "total": 7.0}],
        }
    )

    async def _fake(*a, **k):
        return _fake_capture(payload)

    monkeypatch.setattr(az, "run_metrics_capture", _fake)
    res = asyncio.run(
        az.resolve_azure_metrics(
            {
                "resource_id": "/x",
                "metrics": ["cpu_percent", "connection_failed"],
                "aggregations": ["Average", "Total"],
                "aggregation_by_metric": {"cpu_percent": "Average", "connection_failed": "Total"},
            },
            None,
            {},
        )
    )
    d = res.to_dict()
    cols = [c["name"] for c in d["columns"]]
    row = d["rows"][0]
    assert row[cols.index("cpu_percent")] == 42.0  # used 'average'
    assert row[cols.index("connection_failed")] == 7.0  # used 'total', not 0.1


# --- tool: live metric definitions (validation, per-metric agg, unit) -----------------
_FAKE_DEFS = [
    {"name": "Percentage CPU", "display": "CPU %", "primary": "Average", "supported": ["Average"], "unit": "Percent"},
    {"name": "Requests", "display": "Requests", "primary": "Total", "supported": ["Total"], "unit": "Count"},
    {"name": "MemoryWorkingSet", "display": "Mem", "primary": "Average", "supported": ["Average"], "unit": "Bytes"},
]


def _patch_defs(monkeypatch, defs):
    async def _f(_rid, _conn):
        return defs

    monkeypatch.setattr("app.monitor.metric_defs.get_metric_definitions", _f)


def test_tool_validates_against_live_catalog(monkeypatch):
    _patch_azure(monkeypatch, _sample_table())
    _patch_defs(monkeypatch, _FAKE_DEFS)
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    # Unknown metric → helpful error listing the real catalog.
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["Bogus"]}))
    assert out["isError"] is True
    assert "Available metrics" in out["content"][0]
    assert "Percentage CPU" in out["content"][0]


def test_tool_passes_per_metric_aggregation_and_unit(monkeypatch):
    captured = {}
    _patch_defs(monkeypatch, _FAKE_DEFS)
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda cid=None: {})

    async def _capture_resolve(cfg, conn, params):
        captured.update(cfg)
        return _FakeTable(_sample_table())

    monkeypatch.setattr("app.monitor.datasources.azure.resolve_azure_metrics", _capture_resolve)
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"

    # CPU (Average gauge, Percent) + Requests (Total count) → mixed unit ⇒ no unit label.
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["Percentage CPU", "Requests"]}))
    assert out["isError"] is False
    assert captured["aggregation_by_metric"] == {"percentage cpu": "Average", "requests": "Total"}
    assert set(captured["aggregations"]) == {"Average", "Total"}
    spec = _extract_chart_id(out["content"][0])
    assert spec["unit"] == ""  # mixed % + count

    # Single Percent metric ⇒ unit label '%'.
    out = asyncio.run(b._azure_metrics({}, {"resource_id": rid, "metrics": ["Percentage CPU"]}))
    assert _extract_chart_id(out["content"][0])["unit"] == "%"


def test_tool_explicit_aggregation_overrides_per_metric(monkeypatch):
    captured = {}
    _patch_defs(monkeypatch, _FAKE_DEFS)
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda cid=None: {})

    async def _capture_resolve(cfg, conn, params):
        captured.update(cfg)
        return _FakeTable(_sample_table())

    monkeypatch.setattr("app.monitor.datasources.azure.resolve_azure_metrics", _capture_resolve)
    rid = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/sites/album-api"
    out = asyncio.run(
        b._azure_metrics({}, {"resource_id": rid, "metrics": ["Requests"], "aggregation": "Maximum"})
    )
    assert out["isError"] is False
    assert captured["aggregations"] == ["Maximum"]
    assert captured["aggregation_by_metric"] == {}  # explicit override → no per-metric map


def test_default_metrics_from_defs_prefers_gauges():
    picked = b._default_metrics_from_defs(_FAKE_DEFS, limit=2)
    # Gauges (Average) before the Total count.
    assert picked[0] in ("Percentage CPU", "MemoryWorkingSet")
    assert "Requests" not in picked[:1]
