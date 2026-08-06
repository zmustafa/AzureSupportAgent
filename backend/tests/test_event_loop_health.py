"""Guards for the freeze: nothing expensive may run on the event loop.

This product runs a single uvicorn worker, so there is exactly ONE event loop and every request
in the application shares it. A synchronous call left on that loop does not slow one screen down
— it stops all of them, including `/health` and the login that would let somebody back in. The
reported symptom is always the same ("the app froze") and the visible error is always misleading
(SQLite `database is locked`, because an awaited commit cannot resume while nothing is being
scheduled).

It has now happened three times, in three different call sites, with the same root cause:

  * a right-sizing analysis run inline in an async handler (fixed at the endpoint),
  * the SAME analysis still inline in `orchestrator.refresh_usage` (missed by that fix),
  * the shadow-access join and the run snapshot inline in the refresh job.

Fixing the instances is not enough, because the next one is written the same way. These tests
fail on the SHAPE.

Measured on a real 5,514-row tenant while writing them: the same work costs 0.98 s of event-loop
lag inline and 0.04 s in a thread — a 23x difference in how long the rest of the product is dead.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import statistics
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Functions that are expensive enough that calling one on the event loop is a defect. Each
# recomposes the estate, indexes every role definition, or gzips a full row set.
_MUST_BE_THREADED = {
    "build_master_rows",
    "build_facts",
    "compute_overview",
    "compute_pivots",
    "build_scope_tree",
    "read_directory",
    "all_scope_rows",
    "write_scope",
    "write_directory",
    "write_bypass",
    "write_usage",
    "write_drift",
    "compute_reachability",
    "analyse_for_tenant",
    "graph_for_tenant",
    "evaluate_all",
    "to_workbook",
    "build_leavers",
    "to_disabled_workbook",
}

_ROOT = Path(__file__).resolve().parents[1] / "app"
_FILES = [
    _ROOT / "api" / "iam.py",
    _ROOT / "iam" / "orchestrator.py",
    _ROOT / "iam" / "job.py",
    _ROOT / "iam" / "store.py",
]


class _InlineCallFinder(ast.NodeVisitor):
    """Finds calls to expensive functions made directly inside an `async def` body.

    A call is acceptable when it is the FUNCTION passed to `asyncio.to_thread(...)`, or when it
    sits inside a nested plain `def` (which is what gets handed to `to_thread`). A call in the
    ARGUMENT list of `to_thread` is NOT acceptable and is deliberately reported: the call is
    threaded, its arguments are evaluated on the loop first. That exact mistake was live in the
    workbook export, where the two most expensive computations in the product sat in the
    argument list of a `to_thread` that appeared to protect them."""

    def __init__(self) -> None:
        self.async_depth = 0
        self.sync_depth = 0
        self.offenders: list[tuple[str, int]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.async_depth += 1
        self.generic_visit(node)
        self.async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        # A nested plain `def` is the standard way to hand a block of work to a thread.
        self.sync_depth += 1
        self.generic_visit(node)
        self.sync_depth -= 1

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self.sync_depth += 1
        self.generic_visit(node)
        self.sync_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node.func)
        if name in ("to_thread", "run") and _is_offload(node.func):
            # The offloaded callable is fine; its arguments are evaluated on the loop.
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            if node.args:
                first = node.args[0]
                # `to_thread(fn, ...)` passes a reference; `to_thread(fn(x))` would be a bug.
                if isinstance(first, (ast.Lambda, ast.Call)):
                    self.visit(first)
            return
        if name in _MUST_BE_THREADED and self.async_depth and not self.sync_depth:
            self.offenders.append((name, node.lineno))
        self.generic_visit(node)


def _is_offload(func: ast.AST) -> bool:
    """`asyncio.to_thread(...)` or `cpu.run(...)` — the two sanctioned ways off the loop."""
    if not isinstance(func, ast.Attribute):
        return False
    owner = func.value
    owner_name = owner.id if isinstance(owner, ast.Name) else ""
    return (func.attr == "to_thread" and owner_name == "asyncio") or (
        func.attr == "run" and owner_name == "cpu"
    )


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_no_expensive_call_runs_on_the_event_loop(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    finder = _InlineCallFinder()
    finder.visit(tree)
    assert not finder.offenders, (
        f"{path.name} calls these on the event loop: "
        + ", ".join(f"{n}() at line {ln}" for n, ln in finder.offenders)
        + ". Wrap them in asyncio.to_thread — inline, they stall EVERY request in the process, "
        "not just this one."
    )


def test_the_detector_can_actually_fail():
    """A guard nobody has watched fail is not a guard.

    The rule in this repo is to break the thing a new detector protects and confirm it fires.
    This is that check, kept, so a future refactor of the visitor cannot quietly neuter it."""
    src = "async def handler():\n    rows = compose.build_master_rows(t)\n"
    finder = _InlineCallFinder()
    finder.visit(ast.parse(src))
    assert finder.offenders == [("build_master_rows", 2)]

    # And the argument-list trap specifically, which is the subtle one.
    src2 = "async def handler():\n    await asyncio.to_thread(export.to_workbook, escalation=graph_for_tenant(t))\n"
    finder2 = _InlineCallFinder()
    finder2.visit(ast.parse(src2))
    assert finder2.offenders == [("graph_for_tenant", 2)], (
        "a call in the ARGUMENT list of to_thread runs on the loop and must be reported"
    )

    # A properly threaded call must NOT be reported, or the guard is noise and gets deleted.
    src3 = "async def handler():\n    rows = await asyncio.to_thread(compose.build_master_rows, t)\n"
    finder3 = _InlineCallFinder()
    finder3.visit(ast.parse(src3))
    assert finder3.offenders == []

    # ...and neither must the capped runner.
    src4 = "async def handler():\n    g = await cpu.run(escalation.graph_for_tenant, t)\n"
    finder4 = _InlineCallFinder()
    finder4.visit(ast.parse(src4))
    assert finder4.offenders == []

    # A bare `run(...)` on something else must NOT be mistaken for an offload.
    src5 = "async def handler():\n    x = scanners.run(compute_overview(t))\n"
    finder5 = _InlineCallFinder()
    finder5.visit(ast.parse(src5))
    assert finder5.offenders == [("compute_overview", 2)]


@pytest.mark.parametrize(
    "module,attr",
    [
        ("app.api.iam", "escalation_graph"),
        ("app.api.iam", "rightsizing"),
        ("app.api.iam", "export_workbook"),
        ("app.iam.orchestrator", "refresh_usage"),
    ],
)
def test_the_heaviest_jobs_run_under_the_concurrency_cap(module: str, attr: str):
    """Threading stops the freeze; it does not stop the pile-up.

    Each of these is tens of seconds of pure-Python CPU. Threaded but uncapped, two users on
    Findings plus a scheduled refresh plus an export is four of them competing for one GIL, and
    the result is indistinguishable from the freeze whatever each one does individually."""
    import importlib

    mod = importlib.import_module(module)
    src = inspect.getsource(getattr(mod, attr))
    assert "cpu.run(" in src, f"{module}.{attr} must go through the capped CPU runner"


async def test_the_session_heartbeat_is_not_awaited_on_the_request_path():
    """Every authenticated request calls `resolve_session`. If it commits before returning, the
    whole product queues behind whatever holds the SQLite write lock."""
    from app.auth import service

    src = inspect.getsource(service.resolve_session)
    assert "_schedule_slide" in src, "the heartbeat must be queued, not awaited inline"


async def test_refresh_usage_does_not_analyse_on_the_loop():
    """The specific regression: the right-sizing analysis was moved off the loop at the
    /iam/rightsizing endpoint and left inline here, so the freeze came back during refreshes.
    One fixed call site is not a fixed defect."""
    from app.iam import orchestrator

    src = inspect.getsource(orchestrator.refresh_usage)
    assert "cpu.run(rightsize.analyse_for_tenant" in src


async def test_the_cpu_cap_actually_serialises():
    """More workers do not make Python CPU work finish sooner — they spread one GIL across more
    contenders and make every interactive request slower. The cap must hold."""
    from app.iam import cpu

    concurrent = 0
    peak = 0

    def work() -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        time.sleep(0.05)
        concurrent -= 1

    await asyncio.gather(*[cpu.run(work) for _ in range(cpu.MAX_CONCURRENT * 4)])
    assert peak <= cpu.MAX_CONCURRENT, f"{peak} jobs ran at once against a cap of {cpu.MAX_CONCURRENT}"
    assert cpu.stats()["inflight"] == 0, "the gauge leaked"


async def test_a_failing_cpu_job_does_not_leak_a_slot():
    """A raised exception must release the semaphore, or the cap degrades to a deadlock after a
    handful of failures — which would present as the very freeze this exists to prevent."""
    from app.iam import cpu

    def boom() -> None:
        raise RuntimeError("nope")

    for _ in range(cpu.MAX_CONCURRENT + 2):
        with pytest.raises(RuntimeError):
            await cpu.run(boom)
    assert cpu.stats()["inflight"] == 0
    await asyncio.wait_for(cpu.run(lambda: None), timeout=2)


async def test_a_blocking_call_is_detectable_at_runtime():
    """The loop-lag probe must actually observe a stall, or the monitor in production is
    decoration. Blocks the loop deliberately and checks the lag is seen."""
    from app.core import loopwatch

    loopwatch.reset()
    samples: list[float] = []

    async def probe() -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            samples.append(time.monotonic() - before - 0.01)

    task = asyncio.get_running_loop().create_task(probe())
    await asyncio.sleep(0.05)
    # Blocking the loop on purpose is the POINT of this test: it is the defect being detected,
    # so the probe must see it. ASYNC251 is exactly right about the code and exactly wrong here.
    time.sleep(0.3)  # noqa: ASYNC251
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert max(samples) >= 0.2, f"a 0.3s block went unseen (max lag {max(samples):.3f}s)"
    assert statistics.fmean(samples) < max(samples)
