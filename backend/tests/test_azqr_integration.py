"""Runtime readiness and secure report ingestion for Azure Quick Review."""
from __future__ import annotations

import json

import pytest

from app.agent import file_artifacts
from app.mcp.client import (
    DiscoveredTool,
    classify_tool,
    normalize_structured_result,
    refresh_runtime_availability,
    tool_runtime_availability,
)


@pytest.fixture(autouse=True)
def _restore_artifact_root():
    original = file_artifacts._ROOT
    yield
    file_artifacts.set_path_for_tests(original)


def _tool() -> DiscoveredTool:
    return DiscoveredTool(
        name="extension_azqr",
        description="Azure Quick Review",
        parameters={"type": "object", "properties": {}},
        kind="write",
    )


def test_missing_azqr_is_marked_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.client.shutil.which", lambda _name: None)
    available, reason = tool_runtime_availability("extension_azqr")
    assert available is False
    assert "not found in PATH" in reason
    [tool] = refresh_runtime_availability([_tool()])
    assert tool.available is False
    assert tool.unavailable_reason == reason


def test_installed_azqr_is_available_and_explicitly_read_only(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.client.shutil.which", lambda _name: "/usr/local/bin/azqr")
    assert tool_runtime_availability("extension_azqr") == (True, "")
    assert classify_tool("extension_azqr") == "read"


def test_unrelated_mcp_tool_has_no_runtime_requirement(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.client.shutil.which", lambda _name: None)
    assert tool_runtime_availability("arm") == (True, "")


def test_missing_azd_is_marked_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.mcp.client.shutil.which", lambda _name: None)
    available, reason = tool_runtime_availability("azd")
    assert available is False
    assert "Azure Developer CLI" in reason


def test_structured_http_failure_becomes_a_tool_error() -> None:
    result = normalize_structured_result({
        "isError": False,
        "content": [json.dumps({"status": 400, "message": "The command parameter is required."})],
    })
    assert result["isError"] is True


def test_structured_success_stays_successful() -> None:
    result = normalize_structured_result({
        "isError": False,
        "content": [json.dumps({"status": 200, "message": "Success", "results": {}})],
    })
    assert result["isError"] is False


def test_ingestion_copies_downloads_parses_json_and_deletes_temp_files(tmp_path, monkeypatch) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(file_artifacts.tempfile, "gettempdir", lambda: str(reports))
    file_artifacts.set_path_for_tests(storage)

    json_path = reports / "report.json"
    xlsx_path = reports / "report.xlsx"
    json_path.write_text(json.dumps({"recommendations": [{"name": "one"}]}), encoding="utf-8")
    xlsx_path.write_bytes(b"PK\x03\x04workbook")
    raw = {
        "isError": False,
        "content": [json.dumps({
            "status": 200,
            "results": {
                "xlsxReportPath": str(xlsx_path),
                "jsonReportPath": str(json_path),
                "stdout": "done",
            },
        })],
    }

    result = file_artifacts.ingest_azqr_result(raw, "chat-1")
    assert result["isError"] is False
    assert result["display_summary"] == "Azure Quick Review report generated"
    assert {a["kind"] for a in result["artifacts"]} == {"json", "xlsx"}
    payload = json.loads(result["content"][0])
    assert payload["report_preview"]["recommendations"][0]["name"] == "one"
    assert result["content"][0].index('"artifacts"') < result["content"][0].index('"report_preview"')
    assert "Markdown download link" in payload["response_instruction"]
    assert str(reports) not in result["content"][0]
    assert not json_path.exists()
    assert not xlsx_path.exists()
    for artifact in result["artifacts"]:
        path, metadata = file_artifacts.resolve("chat-1", artifact["artifact_id"])
        assert path.is_file()
        assert metadata["filename"] == artifact["filename"]


def test_ingestion_refuses_report_outside_temp_root(tmp_path, monkeypatch) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(file_artifacts.tempfile, "gettempdir", lambda: str(reports))
    result = file_artifacts.ingest_azqr_result({
        "isError": False,
        "content": [json.dumps({"jsonReportPath": str(outside)})],
    }, "chat-1")
    assert result["isError"] is True
    assert "outside the temporary directory" in result["content"][0]
    assert outside.exists()


def test_delete_chat_removes_only_that_chat_directory(tmp_path) -> None:
    file_artifacts.set_path_for_tests(tmp_path)
    first = tmp_path / "chat-one"
    second = tmp_path / "chat-two"
    first.mkdir()
    second.mkdir()
    (first / "file").write_text("one", encoding="utf-8")
    (second / "file").write_text("two", encoding="utf-8")
    file_artifacts.delete_chat("chat-one")
    assert not first.exists()
    assert second.exists()


def test_resolve_rejects_path_traversal(tmp_path) -> None:
    file_artifacts.set_path_for_tests(tmp_path)
    try:
        file_artifacts.resolve("../outside", "artifact")
    except ValueError as exc:
        assert "Invalid chat id" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")
