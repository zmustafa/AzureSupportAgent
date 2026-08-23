"""Renders must not run on the event loop.

A synchronous openpyxl/CSV build inside an ``async def`` stalls *every* request in the
process, not just its own. This was observed live: ``app.core.loopwatch`` logged
"event loop blocked for 3.12s" during ``GET /alert-analysis/export?format=xlsx``.

The structural test is the real guard — a behavioural test cannot catch a new endpoint
that someone adds tomorrow.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"

# Functions that do real CPU work (workbook assembly, PDF raster, large CSV joins).
_BLOCKING = re.compile(
    r"^(to_workbook|build_workbook|to_xlsx|render_pdf|to_pdf|build_pdf|write_pdf"
    r"|to_csv|build_report|render_report|to_deck|build_deck|to_excel)$"
)

# to_json is cheap (stdlib json on an already-built dict) and stays inline deliberately.


def _offenders() -> list[tuple[str, int, str, str]]:
    """Every blocking render called directly inside an async def, deduped by (file, line)."""
    found: dict[tuple[str, int], tuple[str, int, str, str]] = {}

    class V(ast.NodeVisitor):
        def __init__(self, rel: str, src: str) -> None:
            self.rel, self.lines = rel, src.splitlines()
            self.stack: list[bool] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(False)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.stack.append(True)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            # Only the innermost enclosing function counts; a sync helper nested in an
            # async collector is fine.
            if self.stack and self.stack[-1]:
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name and _BLOCKING.match(name):
                    line = self.lines[node.lineno - 1].strip()
                    # `await x.build_report(...)` is already a coroutine, not blocking.
                    awaited_coro = line.lstrip().startswith("return await ") or " = await " in line
                    offloaded = "to_thread" in line or "run_in_executor" in line
                    if not offloaded and not awaited_coro:
                        found[(self.rel, node.lineno)] = (self.rel, node.lineno, name, line)
            self.generic_visit(node)

    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            continue
        V(str(path.relative_to(APP)).replace("\\", "/"), src).visit(tree)

    return sorted(found.values())


def test_no_synchronous_render_on_the_event_loop() -> None:
    offenders = _offenders()
    assert not offenders, "Blocking render inside async def — wrap in asyncio.to_thread():\n" + "\n".join(
        f"  {rel}:{ln}  {name}()  ->  {line[:100]}" for rel, ln, name, line in offenders
    )


def test_the_detector_actually_detects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity: prove the scan fails on a known-bad shape."""
    bad = tmp_path / "app"
    (bad / "sub").mkdir(parents=True)
    (bad / "sub" / "route.py").write_text(
        "async def export():\n    return to_workbook(x)\n", encoding="utf-8"
    )
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "APP", bad)
    assert mod._offenders(), "detector missed an obvious blocking call"


def test_await_of_an_async_builder_is_not_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`return await report_ops.build_report(...)` is a coroutine, not blocking work."""
    good = tmp_path / "app"
    (good / "sub").mkdir(parents=True)
    (good / "sub" / "route.py").write_text(
        "async def r():\n    return await ops.build_report(c, e, days=30)\n", encoding="utf-8"
    )
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "APP", good)
    assert not mod._offenders(), "awaited coroutine was wrongly flagged as blocking"


def test_to_thread_form_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = tmp_path / "app"
    (ok / "sub").mkdir(parents=True)
    (ok / "sub" / "route.py").write_text(
        "async def e():\n    return await asyncio.to_thread(export.to_workbook, snap)\n",
        encoding="utf-8",
    )
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "APP", ok)
    assert not mod._offenders(), "correctly offloaded call was flagged"
