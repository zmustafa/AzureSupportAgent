"""Chat-owned downloadable files ingested from external tool output."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2] / ".data" / "chat_artifacts"
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_MAX_JSON_BYTES = 25 * 1024 * 1024
_MAX_XLSX_BYTES = 100 * 1024 * 1024


def set_path_for_tests(path: Path) -> None:
    global _ROOT
    _ROOT = path


def _safe_id(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value or ""):
        raise ValueError(f"Invalid {label}.")
    return value


def _temp_file(path: str, suffix: str) -> Path:
    source = Path(path).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if source.suffix.lower() != suffix or not source.is_file():
        raise ValueError(f"Expected a generated {suffix} report file.")
    if source.parent != temp_root:
        raise ValueError("Generated report path is outside the temporary directory.")
    return source


def _store(chat_id: str, source: Path, kind: str) -> dict[str, str]:
    _safe_id(chat_id, "chat id")
    artifact_id = uuid.uuid4().hex
    suffix = source.suffix.lower()
    maximum = _MAX_XLSX_BYTES if suffix == ".xlsx" else _MAX_JSON_BYTES
    if source.stat().st_size > maximum:
        raise ValueError(f"Generated {kind.upper()} report exceeds the download limit.")
    directory = _ROOT / chat_id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{artifact_id}{suffix}"
    shutil.copyfile(source, destination)
    filename = f"azure-quick-review-{artifact_id[:8]}{suffix}"
    metadata = {
        "artifact_id": artifact_id,
        "kind": kind,
        "filename": filename,
        "content_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if suffix == ".xlsx" else "application/json"
        ),
        "created_at": str(time.time()),
    }
    (directory / f"{artifact_id}.meta.json").write_text(
        json.dumps(metadata, separators=(",", ":")), encoding="utf-8",
    )
    return {
        **metadata,
        "url": f"/api/chats/{chat_id}/artifacts/{artifact_id}",
        "label": f"Download {kind.upper()} report",
    }


def resolve(chat_id: str, artifact_id: str) -> tuple[Path, dict[str, str]]:
    _safe_id(chat_id, "chat id")
    _safe_id(artifact_id, "artifact id")
    directory = _ROOT / chat_id
    metadata_path = directory / f"{artifact_id}.meta.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("Artifact was not found.") from exc
    if metadata.get("artifact_id") != artifact_id:
        raise FileNotFoundError("Artifact was not found.")
    suffix = ".xlsx" if metadata.get("kind") == "xlsx" else ".json"
    path = directory / f"{artifact_id}{suffix}"
    if not path.is_file():
        raise FileNotFoundError("Artifact was not found.")
    return path, metadata


def delete_chat(chat_id: str) -> None:
    if _SAFE_ID.fullmatch(chat_id or ""):
        shutil.rmtree(_ROOT / chat_id, ignore_errors=True)


def _find_report_paths(value: Any, found: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if isinstance(item, str) and lowered.endswith("reportpath"):
                if item.lower().endswith(".json"):
                    found["json"] = item
                elif item.lower().endswith(".xlsx"):
                    found["xlsx"] = item
            _find_report_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _find_report_paths(item, found)


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[nested data omitted; download the JSON report]"
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth=depth + 1) for key, item in list(value.items())[:120]}
    if isinstance(value, list):
        kept = [_bounded(item, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            kept.append({"omitted_items": len(value) - 100})
        return kept
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


def ingest_azqr_result(result: dict[str, Any], chat_id: str = "") -> dict[str, Any]:
    """Copy generated reports into chat-owned storage and replace internal paths with data."""
    if result.get("isError"):
        return result
    parsed_blocks: list[Any] = []
    paths: dict[str, str] = {}
    for block in result.get("content") or []:
        try:
            parsed = json.loads(block) if isinstance(block, str) else block
        except (json.JSONDecodeError, TypeError):
            parsed = block
        parsed_blocks.append(parsed)
        _find_report_paths(parsed, paths)
    if not paths:
        return result

    sources: dict[str, Path] = {}
    artifacts: list[dict[str, str]] = []
    report_preview: Any = None
    try:
        for kind, raw_path in paths.items():
            source = _temp_file(raw_path, f".{kind}")
            sources[kind] = source
            if kind == "json":
                if source.stat().st_size <= _MAX_JSON_BYTES:
                    report_preview = _bounded(json.loads(source.read_text(encoding="utf-8")))
            if chat_id:
                artifacts.append(_store(chat_id, source, kind))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "isError": True,
            "content": [f"Azure Quick Review generated a report, but secure ingestion failed: {exc}"],
        }
    finally:
        for source in sources.values():
            try:
                source.unlink()
            except OSError:
                pass

    payload = {
        "status": "completed",
        "message": "Azure Quick Review report generated successfully.",
        "artifacts": artifacts,
        "artifact_note": (
            "Download links are scoped to this chat and require the signed-in user."
            if artifacts else "No chat download context was available."
        ),
        "response_instruction": (
            "Include every artifact URL below as a Markdown download link in the final answer."
            if artifacts else ""
        ),
        "report_preview": report_preview,
    }
    return {
        "isError": False,
        "content": [json.dumps(payload, ensure_ascii=False, default=str)],
        "display_summary": "Azure Quick Review report generated",
        "artifacts": artifacts,
    }
