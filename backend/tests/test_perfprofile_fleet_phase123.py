"""Regression coverage for Performance Profiler fleet hardening phases 1-3."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.db as dbmod
import app.exec.command_runner as command_runner
from app.exec.command_runner import CaptureResult
from app.models import Base, PerfProfileFleetBatch, PerfProfileFleetItem
from app.perfprofile import collector, fleet, runs, service


def _settings(**overrides):
    values = {
        "perfprofile_metric_concurrency": 2,
        "perfprofile_metric_max_attempts": 1,
        "perfprofile_workload_timeout_s": 1200,
        "perfprofile_fleet_concurrency": 1,
        "perfprofile_fleet_start_delay_ms": 0,
        "perfprofile_interval": "PT15M",
        "perfprofile_scan_cap": 200,
    }
    values.update(overrides)
    return values


def test_no_observations_never_score_as_perfect_health():
    reference = {
        "types": {
            "microsoft.compute/virtualmachines": {
                "display": "VM",
                "alerts": [
                    {
                        "key": "cpu",
                        "metric": "Percentage CPU",
                        "name": "CPU",
                        "signal": "metric",
                        "threshold": 90,
                        "operator": "GreaterThan",
                        "severity": "warning",
                        "unit": "Percent",
                    }
                ],
            }
        }
    }
    snapshot = collector.compute_profile(
        [{"id": "/vm", "name": "vm", "type": "microsoft.compute/virtualmachines"}],
        {"/vm": {}},
        reference=reference,
    )
    assert snapshot["resources"][0]["score"] is None
    assert snapshot["scorecard"]["workload_score"] is None
    assert snapshot["resources"][0]["state"] == "no_data"


def test_service_principal_session_reuses_shared_cli_extensions(monkeypatch, tmp_path):
    home = tmp_path / "home"
    extensions = home / ".azure" / "cliextensions"
    extensions.mkdir(parents=True)
    monkeypatch.delenv("AZURE_EXTENSION_DIR", raising=False)
    monkeypatch.setattr(command_runner.Path, "home", lambda: home)
    env = command_runner._run_env(
        {"tenant_id": "tenant", "auth_method": "service_principal"},
        str(tmp_path / "session"),
    )
    assert env["AZURE_EXTENSION_DIR"] == str(extensions)


def test_grouped_metric_collection_reuses_one_sp_session_and_one_request(monkeypatch):
    reference = {
        "types": {
            "microsoft.test/widgets": {
                "display": "Widget",
                "alerts": [
                    {"key": "a", "metric": "Metric A", "name": "A", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent"},
                    {"key": "b", "metric": "Metric B", "name": "B", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent"},
                ],
            }
        }
    }
    resources = [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]
    sessions: list[tuple[str, str | None]] = []
    calls: list[list[str]] = []

    async def open_session(_connection):
        sessions.append(("open", "session-1"))
        return "session-1", None

    def close_session(session):
        sessions.append(("close", session))

    async def metric_capture(_rid, metrics, _connection, **kwargs):
        calls.append(list(metrics))
        assert kwargs["session_config_dir"] == "session-1"
        return CaptureResult(
            ok=True,
            stdout=json.dumps(
                {
                    "value": [
                        {"name": {"value": "Metric A"}, "timeseries": [{"data": [{"timeStamp": "t", "average": 10}]}]},
                        {"name": {"value": "Metric B"}, "timeseries": [{"data": [{"timeStamp": "t", "average": 20}]}]},
                    ]
                }
            ),
        )

    async def query(_predicates, _connection, *, session_dir=None):
        assert session_dir == "session-1"
        return resources

    async def hydrate(_targets, _connection, *, session_dir=None):
        assert session_dir == "session-1"

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", close_session)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", hydrate)
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(
            {"auth_method": "service_principal_secret"},
            scope_kind="subscription",
            scope_id="sub",
            workload=None,
        )
    )
    assert calls == [["Metric A", "Metric B"]]
    assert sessions == [("open", "session-1"), ("close", "session-1")]
    assert snapshot["status"] == "succeeded"
    assert snapshot["collection"]["metric_requests_total"] == 1
    assert snapshot["collection"]["metric_checks_succeeded"] == 2
    assert snapshot["scorecard"]["workload_score"] == 100


def test_storage_metric_groups_use_service_subresources_and_supported_intervals(monkeypatch):
    reference = {
        "types": {
            "microsoft.storage/storageaccounts": {
                "display": "Storage",
                "alerts": [
                    {"key": "root", "metric": "Availability", "metric_namespace": "Microsoft.Storage/storageAccounts", "window_size": "PT5M", "name": "Availability", "signal": "metric", "threshold": 99, "operator": "LessThan", "severity": "warning", "unit": "Percent"},
                    {"key": "blob", "metric": "BlobCapacity", "metric_namespace": "Microsoft.Storage/storageAccounts/blobServices", "window_size": "PT1H", "name": "Blob capacity", "signal": "metric", "threshold": 1000, "operator": "GreaterThan", "severity": "warning", "unit": "Bytes"},
                    {"key": "queue", "metric": "QueueCount", "metric_namespace": "Microsoft.Storage/storageAccounts/queueServices", "window_size": "PT1H", "name": "Queue count", "signal": "metric", "threshold": 10, "operator": "GreaterThan", "severity": "warning", "unit": "Count"},
                    {"key": "file", "metric": "FileShareCount", "metric_namespace": "Microsoft.Storage/storageAccounts/fileServices", "window_size": "PT1H", "name": "File count", "signal": "metric", "threshold": 10, "operator": "GreaterThan", "severity": "warning", "unit": "Count"},
                ],
            }
        }
    }
    root = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa"
    calls: list[tuple[str, tuple[str, ...], str]] = []

    async def query(*_args, **_kwargs):
        return [{"id": root, "name": "sa", "type": "microsoft.storage/storageaccounts"}]

    async def metric_capture(resource_id, metrics, _connection, **kwargs):
        calls.append((resource_id, tuple(metrics), kwargs["interval"]))
        return CaptureResult(ok=True, stdout=json.dumps({"value": [{"name": {"value": metrics[0]}, "timeseries": [{"data": [{"timeStamp": "t", "average": 1}]}]}]}))

    async def open_session(_connection):
        return None, None

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None, interval="PT15M")
    )
    assert snapshot["status"] == "succeeded"
    assert set(calls) == {
        (root, ("Availability",), "PT15M"),
        (f"{root}/blobServices/default", ("BlobCapacity",), "PT1H"),
        (f"{root}/queueServices/default", ("QueueCount",), "PT1H"),
        (f"{root}/fileServices/default", ("FileShareCount",), "PT1H"),
    }


def test_all_metric_requests_failing_is_terminal_failure_not_score_100(monkeypatch):
    reference = {
        "types": {
            "microsoft.test/widgets": {
                "display": "Widget",
                "alerts": [
                    {"key": "a", "metric": "Metric A", "name": "A", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent"}
                ],
            }
        }
    }

    async def query(*_args, **_kwargs):
        return [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]

    async def metric_capture(*_args, **_kwargs):
        return CaptureResult(ok=False, error="ARM 429 Too Many Requests")

    async def open_session(_connection):
        return None, None

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert snapshot["status"] == "failed"
    assert snapshot["scorecard"]["workload_score"] is None
    assert snapshot["collection"]["metric_checks_failed"] == 1
    assert snapshot["collection"]["metric_requests_throttled"] == 1
    assert "429" in snapshot["error"]


def test_transient_metric_failure_retries_then_succeeds_with_attempt_counts(monkeypatch):
    reference = {
        "types": {
            "microsoft.test/widgets": {
                "display": "Widget",
                "alerts": [
                    {"key": "a", "metric": "Metric A", "name": "A", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent"}
                ],
            }
        }
    }
    attempts = 0

    async def query(*_args, **_kwargs):
        return [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]

    async def metric_capture(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return CaptureResult(ok=False, error="ARM 503 temporarily unavailable")
        return CaptureResult(ok=True, stdout=json.dumps({"value": [{"name": {"value": "Metric A"}, "timeseries": [{"data": [{"timeStamp": "t", "average": 10}]}]}]}))

    async def no_delay(_seconds):
        return None

    async def open_session(_connection):
        return None, None

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings(perfprofile_metric_max_attempts=3))
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)
    monkeypatch.setattr(collector.asyncio, "sleep", no_delay)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert snapshot["status"] == "succeeded"
    assert attempts == 3
    assert snapshot["collection"]["metric_requests_total"] == 1
    assert snapshot["collection"]["metric_request_attempts"] == 3
    assert snapshot["collection"]["metric_requests_retried"] == 2


def test_mixed_metric_success_and_failure_is_partial_with_completeness(monkeypatch):
    reference = {
        "types": {
            "microsoft.test/widgets": {
                "display": "Widget",
                "alerts": [
                    {"key": "ok", "metric": "Metric OK", "name": "OK", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent"},
                    {"key": "bad", "metric": "Metric Bad", "name": "Bad", "signal": "metric", "threshold": 90, "operator": "GreaterThan", "severity": "warning", "unit": "Percent", "dimension_filter": "Kind eq 'bad'"},
                ],
            }
        }
    }

    async def query(*_args, **_kwargs):
        return [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]

    async def metric_capture(_rid, metrics, _connection, **_kwargs):
        if metrics == ["Metric Bad"]:
            return CaptureResult(ok=False, error="ARM 403 forbidden")
        return CaptureResult(ok=True, stdout=json.dumps({"value": [{"name": {"value": "Metric OK"}, "timeseries": [{"data": [{"timeStamp": "t", "average": 10}]}]}]}))

    async def open_session(_connection):
        return None, None

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", open_session)
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert snapshot["status"] == "partial"
    assert snapshot["collection"]["metric_checks_succeeded"] == 1
    assert snapshot["collection"]["metric_checks_failed"] == 1
    assert snapshot["collection"]["completeness_pct"] == 50
    assert snapshot["scorecard"]["workload_score"] == 100
    assert snapshot["warning"]


def test_metric_groups_chunk_at_twenty_and_use_declared_aggregation(monkeypatch):
    alerts = [
        {
            "key": f"m{i}", "metric": f"Metric {i}", "name": f"Metric {i}",
            "signal": "metric", "threshold": 10, "operator": "GreaterThan",
            "severity": "warning", "unit": "Count", "time_aggregation": "Total",
            "metric_namespace": "Microsoft.Test/widgets",
        }
        for i in range(26)
    ]
    reference = {"types": {"microsoft.test/widgets": {"display": "Widget", "alerts": alerts}}}
    calls: list[tuple[list[str], str, str | None]] = []

    async def query(*_args, **_kwargs):
        return [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]

    async def metric_capture(_rid, metrics, _connection, **kwargs):
        calls.append((list(metrics), kwargs["aggregation"], kwargs.get("metric_namespace")))
        return CaptureResult(
            ok=True,
            stdout=json.dumps(
                {
                    "value": [
                        {
                            "name": {"value": metric},
                            "timeseries": [{"data": [{"timeStamp": "t", "total": 1}]}],
                        }
                        for metric in metrics
                    ]
                }
            ),
        )

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", lambda _c: asyncio.sleep(0, result=(None, None)))
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert snapshot["status"] == "succeeded"
    assert [len(metrics) for metrics, _aggregation, _namespace in calls] == [20, 6]
    assert {aggregation for _metrics, aggregation, _namespace in calls} == {"Total"}
    assert {namespace for _metrics, _aggregation, namespace in calls} == {"Microsoft.Test/widgets"}
    assert snapshot["collection"]["metric_checks_succeeded"] == 26
    assert snapshot["collection"]["metric_checks_failed"] == 0


def test_bad_group_is_bisected_and_unsupported_metric_is_no_data(monkeypatch):
    reference = {
        "types": {
            "microsoft.test/widgets": {
                "display": "Widget",
                "alerts": [
                    {"key": key, "metric": metric, "name": metric, "signal": "metric", "threshold": 10, "operator": "GreaterThan", "severity": "warning", "unit": "Count", "time_aggregation": "Total"}
                    for key, metric in (("good-a", "Good A"), ("bad", "Bad"), ("good-b", "Good B"))
                ],
            }
        }
    }

    async def query(*_args, **_kwargs):
        return [{"id": "/r1", "name": "r1", "type": "microsoft.test/widgets"}]

    async def metric_capture(_rid, metrics, _connection, **_kwargs):
        if "Bad" in metrics:
            return CaptureResult(
                ok=False,
                error="ERROR: (BadRequest) Failed to find metric configuration; Valid metrics: Good A,Good B",
            )
        return CaptureResult(
            ok=True,
            stdout=json.dumps(
                {
                    "value": [
                        {"name": {"value": metric}, "timeseries": [{"data": [{"timeStamp": "t", "total": 1}]}]}
                        for metric in metrics
                    ]
                }
            ),
        )

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", lambda _c: asyncio.sleep(0, result=(None, None)))
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert snapshot["status"] == "succeeded"
    assert snapshot["collection"]["metric_checks_succeeded"] == 2
    assert snapshot["collection"]["metric_checks_no_data"] == 1
    assert snapshot["collection"]["metric_checks_unsupported"] == 1
    assert snapshot["collection"]["metric_checks_failed"] == 0
    assert "unavailable on these resource SKUs" in snapshot["warning"]


def test_parent_catalog_metric_targets_actual_child_resource(monkeypatch):
    reference = {
        "types": {
            "microsoft.sql/servers": {
                "display": "SQL Server",
                "alerts": [
                    {
                        "key": "db-failures", "metric": "connection_failed", "name": "Failures",
                        "signal": "metric", "metric_namespace": "Microsoft.Sql/servers/databases",
                        "time_aggregation": "Total", "threshold": 1, "operator": "GreaterThan",
                        "severity": "warning", "unit": "Count",
                    }
                ],
            }
        }
    }
    calls: list[tuple[str, str | None]] = []
    database_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Sql/servers/sql/databases/db"

    async def query(*_args, **_kwargs):
        return [
            {"id": database_id.rsplit("/databases/", 1)[0], "name": "sql", "type": "microsoft.sql/servers"},
            {"id": database_id, "name": "db", "type": "microsoft.sql/servers/databases"},
        ]

    async def metric_capture(resource_id, metrics, _connection, **kwargs):
        calls.append((resource_id, kwargs.get("metric_namespace")))
        return CaptureResult(
            ok=True,
            stdout=json.dumps(
                {"value": [{"name": {"value": metrics[0]}, "timeseries": [{"data": [{"timeStamp": "t", "total": 0}]}]}]}
            ),
        )

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr("app.exec.command_runner.open_sp_session", lambda _c: asyncio.sleep(0, result=(None, None)))
    monkeypatch.setattr("app.exec.command_runner.close_sp_session", lambda _session: None)
    monkeypatch.setattr("app.exec.command_runner.run_metrics_capture", metric_capture)
    monkeypatch.setattr(collector, "_query_resources", query)
    monkeypatch.setattr(collector, "_hydrate_disk_limits", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(collector, "load_reference", lambda: reference)

    snapshot = asyncio.run(
        collector.profile_workload(None, scope_kind="subscription", scope_id="sub", workload=None)
    )
    assert calls == [(database_id, "Microsoft.Sql/servers/databases")]
    assert [row["resource_type"] for row in snapshot["resources"]] == ["microsoft.sql/servers/databases"]


def test_amba_dimensions_translate_to_filter_and_wildcards_do_not():
    assert collector._dimension_filter(
        {"dimensions": [{"name": "Status", "operator": "Include", "values": ["Failed", "429"]}]}
    ) == "(Status eq 'Failed' or Status eq '429')"
    assert collector._dimension_filter(
        {"dimensions": [{"name": "Status", "operator": "Exclude", "values": ["Completed"]}]}
    ) == "Status ne 'Completed'"
    assert collector._dimension_filter(
        {"dimensions": [{"name": "ShardId", "operator": "Include", "values": ["*"]}]}
    ) == ""


def test_metric_gate_is_process_wide_and_never_exceeds_two(monkeypatch):
    from app.perfprofile.limits import current_gate_snapshot, metric_slot, reset_gate_observed

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings(perfprofile_metric_concurrency=2))

    async def exercise():
        active = 0
        max_active = 0
        lock = asyncio.Lock()
        reset_gate_observed()

        async def one():
            nonlocal active, max_active
            async with metric_slot():
                async with lock:
                    active += 1
                    max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                async with lock:
                    active -= 1

        await asyncio.gather(*(one() for _ in range(20)))
        return max_active, current_gate_snapshot()

    max_active, snapshot = asyncio.run(exercise())
    assert max_active == 2
    assert snapshot.max_observed == 2
    assert snapshot.active == 0


def test_failed_attempt_is_saved_but_never_promoted_to_cache_or_trend(monkeypatch):
    failed = collector._empty("workload", "w1", error="429 throttled")
    cache_writes: list[dict] = []
    saved: list[dict] = []

    async def profile(*_args, **_kwargs):
        return failed

    def save(*_args, **kwargs):
        saved.append(kwargs)
        return {**failed, "id": "failed-run", "run_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr(collector, "profile_workload", profile)
    monkeypatch.setattr(service.cache, "write_snapshot", lambda *_a: cache_writes.append(_a[-1]))
    monkeypatch.setattr(service.runs, "save_run", save)

    result = asyncio.run(
        service.execute_profile(
            tenant_id="t1",
            actor="tester",
            scope_kind="workload",
            scope_id="w1",
            connection={},
            workload={"id": "w1", "name": "Workload"},
            window="P1D",
            interval="PT15M",
            scan_cap=200,
        )
    )
    assert result["status"] == "failed"
    assert cache_writes == []
    assert saved[0]["record_trend"] is False


def test_latest_success_is_preserved_when_newer_attempt_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "_PATH", tmp_path / "runs.json")
    success = {"status": "succeeded", "scorecard": {"workload_score": 75}}
    failure = {"status": "failed", "error": "timeout", "scorecard": {"workload_score": None}}
    successful = runs.save_run("t1", "workload", "w1", success, record_trend=False)
    failed = runs.save_run("t1", "workload", "w1", failure, record_trend=False)
    assert runs.latest_run("t1", "workload", "w1")["id"] == failed["id"]
    assert runs.latest_successful_run("t1", "workload", "w1")["id"] == successful["id"]
    trusted = runs.latest_runs_for_scopes("t1", [("workload", "w1")])
    attempts = runs.latest_attempts_for_scopes("t1", [("workload", "w1")])
    assert trusted["workload:w1"]["status"] == "succeeded"
    assert attempts["workload:w1"]["status"] == "failed"


def test_latest_partial_is_usable_when_no_complete_success_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "_PATH", tmp_path / "runs.json")
    partial = runs.save_run(
        "t1", "workload", "w1",
        {"status": "partial", "scorecard": {"workload_score": 62}, "collection": {"completeness_pct": 45}},
        record_trend=False,
    )
    failed = runs.save_run(
        "t1", "workload", "w1",
        {"status": "failed", "error": "timeout", "scorecard": {"workload_score": None}},
        record_trend=False,
    )
    assert runs.latest_run("t1", "workload", "w1")["id"] == failed["id"]
    assert runs.latest_successful_run("t1", "workload", "w1") is None
    assert runs.latest_usable_run("t1", "workload", "w1")["id"] == partial["id"]
    trusted = runs.latest_runs_for_scopes("t1", [("workload", "w1")])
    assert trusted["workload:w1"]["status"] == "partial"
    assert trusted["workload:w1"]["workload_score"] == 62


def _fleet_env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'perf-fleet.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr("app.core.app_settings.load_settings", lambda: _settings())
    monkeypatch.setattr(runs, "find_run_by_trigger", lambda *_args: None)
    monkeypatch.setattr("app.workloads.registry.get_workload", lambda wid, **_kw: {"id": wid, "name": wid, "connection_id": "c1"})
    monkeypatch.setattr("app.core.azure_connections.connection_for_scope", lambda *_a, **_k: {"id": "c1"})
    return engine, Session


def test_durable_fleet_runs_thirty_items_serially_and_every_item_terminates(monkeypatch, tmp_path):
    engine, Session = _fleet_env(monkeypatch, tmp_path)
    active = 0
    max_active = 0

    async def fake_execute(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.002)
        active -= 1
        return {
            "id": f"run-{kwargs['scope_id']}",
            "status": "succeeded",
            "collection": {"resources_completed": 2, "resources_selected": 2},
            "error": "",
        }

    monkeypatch.setattr(service, "execute_profile", fake_execute)

    async def run():
        async with Session() as db:
            batch, created = await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="batch-thirty-items",
                workloads=[{"id": f"w{i:02}", "name": f"W{i:02}", "connection_id": "c1"} for i in range(30)],
                window="P1D",
                start_time="",
                end_time="",
            )
            assert created is True
            duplicate, duplicate_created = await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="batch-thirty-items",
                workloads=[{"id": "different", "name": "Different"}],
                window="P1D",
                start_time="",
                end_time="",
            )
            assert duplicate.id == batch.id and duplicate_created is False
        await fleet.worker.start()
        fleet.worker.wake()
        try:
            async def completed():
                while True:
                    current = await fleet.get_batch(batch.id, "t1")
                    if current and current["status"] in {"succeeded", "partial", "failed", "cancelled"}:
                        return current
                    await asyncio.sleep(0.01)

            return await asyncio.wait_for(completed(), timeout=5)
        finally:
            await fleet.worker.stop()
            await engine.dispose()

    result = asyncio.run(run())
    assert result["status"] == "succeeded"
    assert result["completed"] == result["total"] == 30
    assert result["succeeded"] == 30
    assert {item["status"] for item in result["items"]} == {"succeeded"}
    assert max_active == 1


def test_restart_requeues_running_item_and_cancel_marks_pending_terminal(monkeypatch, tmp_path):
    engine, Session = _fleet_env(monkeypatch, tmp_path)

    async def run():
        async with Session() as db:
            batch, _ = await fleet.create_batch(
                db,
                tenant_id="t1",
                actor="tester",
                idempotency_key="batch-restart-recovery",
                workloads=[{"id": "w1", "name": "W1"}, {"id": "w2", "name": "W2"}],
                window="P1D",
                start_time="",
                end_time="",
            )
            first = (
                await db.execute(
                    select(PerfProfileFleetItem).where(PerfProfileFleetItem.batch_id == batch.id).limit(1)
                )
            ).scalar_one()
            first.status = "running"
            first.started_at = datetime.now(timezone.utc)
            batch.status = "running"
            await db.commit()
        recovered = await fleet.recover_interrupted()
        async with Session() as db:
            statuses = list(
                (
                    await db.execute(
                        select(PerfProfileFleetItem.status).where(PerfProfileFleetItem.batch_id == batch.id)
                    )
                ).scalars().all()
            )
        cancelled = await fleet.cancel_batch(batch.id, "t1")
        final = await fleet.get_batch(batch.id, "t1")
        retryable = await fleet.retryable_workloads(batch.id, "t1")
        await engine.dispose()
        return recovered, statuses, cancelled, final, retryable

    recovered, statuses, cancelled, final, retryable = asyncio.run(run())
    assert recovered == 1
    assert statuses == ["queued", "queued"]
    assert cancelled is True
    assert final["status"] == "cancelled"
    assert final["cancelled"] == 2
    assert len(retryable or []) == 2
