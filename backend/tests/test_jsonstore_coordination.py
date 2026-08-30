from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from app.core import jsonstore


_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"


def test_postgres_ssl_query_is_a_driver_argument(monkeypatch) -> None:
    """Azure's ``?ssl=require`` must not be sent as a server runtime parameter."""
    from app.core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            # Construct the userinfo from separate fixture fragments so the repository's
            # connection-string detector still blocks any complete inline credential.
            resolved_database_url="postgresql+asyncpg://app:"
            + "p%40ss"
            + "@db.example.test:5432/azsup?ssl=require"
        ),
    )

    options = jsonstore._postgres_connect_kwargs()

    assert options == {
        "host": "db.example.test",
        "port": 5432,
        "database": "azsup",
        "user": "app",
        "password": "p@ss",
        "timeout": jsonstore._LOCK_TIMEOUT_SECONDS,
        "ssl": "require",
    }


def _contains_json_dumps(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "json"
        and child.func.attr == "dumps"
        for child in ast.walk(node)
    )


def test_backend_has_no_unsafe_direct_json_writers() -> None:
    """All JSON files must go through the coordinated, atomic jsonstore funnel."""
    findings: list[str] = []
    jsonstore_path = (_APP / "core" / "jsonstore.py").resolve()
    for path in _APP.rglob("*.py"):
        if path.resolve() == jsonstore_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "write_text"
                and any(_contains_json_dumps(argument) for argument in node.args)
            ):
                findings.append(f"{path.relative_to(_BACKEND)}:{node.lineno}: write_text(json.dumps(...))")
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "json"
                and function.attr == "dump"
            ):
                findings.append(f"{path.relative_to(_BACKEND)}:{node.lineno}: json.dump(...)")
    assert findings == []


def test_local_fallback_serializes_read_modify_write_across_processes(tmp_path: Path) -> None:
    """The SQLite/local fallback protects the whole mutation, not only os.replace."""
    path = tmp_path / "registry.json"
    database = tmp_path / "coordination.db"
    workers = 4
    mutations_per_worker = 20
    code = """
import sys
from pathlib import Path
from app.core import jsonstore
path = Path(sys.argv[1])
for _ in range(int(sys.argv[2])):
    def increment(value):
        value[\"count\"] = int(value.get(\"count\") or 0) + 1
    jsonstore.mutate_json(path, {\"count\": 0}, increment, indent=None)
"""
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    environment["PYTHONPATH"] = str(_BACKEND)
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and inline test program
            [sys.executable, "-c", code, str(path), str(mutations_per_worker)],
            cwd=_BACKEND,
            env=environment,
        )
        for _ in range(workers)
    ]
    exit_codes = [process.wait(timeout=30) for process in processes]
    assert exit_codes == [0] * workers
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "count": workers * mutations_per_worker
    }


def test_read_cache_observes_atomic_external_replace(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    jsonstore.write_json(path, {"value": "old"})
    assert jsonstore.read_json(path, {}) == {"value": "old"}

    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"value":"new-and-different-size"}', encoding="utf-8")
    os.replace(replacement, path)

    assert jsonstore.read_json(path, {}) == {"value": "new-and-different-size"}
