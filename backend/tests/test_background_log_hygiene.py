from __future__ import annotations

import ast
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[1]
_TARGETS = (
    _BACKEND / "app" / "agent" / "turn_runner.py",
    _BACKEND / "app" / "automations" / "runner.py",
    _BACKEND / "app" / "core" / "genjob.py",
    _BACKEND / "app" / "core" / "durable_jobs.py",
)
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
_REQUEST_DERIVED_NAMES = {
    "chat_id",
    "error",
    "exc",
    "feature",
    "key",
    "self",
    "task",
    "task_id",
    "task_name",
    "target_type",
    "tenant_id",
    "trigger",
}


def test_background_coordination_logs_exclude_request_derived_values() -> None:
    """Process logs stay static; details belong in tenant-scoped durable/audit records."""
    findings: list[str] = []
    for path in _TARGETS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in _LOG_METHODS:
                continue
            referenced = {
                node.id
                for argument in (*call.args, *(keyword.value for keyword in call.keywords))
                for node in ast.walk(argument)
                if isinstance(node, ast.Name)
            }
            unsafe = sorted(referenced & _REQUEST_DERIVED_NAMES)
            if unsafe:
                findings.append(
                    f"{path.relative_to(_BACKEND)}:{call.lineno}: {', '.join(unsafe)}"
                )
    assert findings == []
