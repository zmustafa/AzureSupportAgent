"""Cold-start budget.

`import app.main` was 5,344 ms / 2,485 modules. Two changes brought it to ~3,521 ms:
deferring the `openai` and `mcp` SDKs out of module scope, and registering sub-routers
directly on the app instead of nesting them through one parent router (``include_router``
re-creates every route it copies, so nesting cost a second full pass over ~1,077 routes).

Both regress silently and invisibly - a module-level ``import openai`` added next quarter
costs 374 ms and nothing fails. These assertions are the alarm.

The wall-clock ceiling is deliberately generous because CI hardware varies. The
``sys.modules`` and route-count assertions are exact, deterministic, and are what actually
prevents the regression.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

#: Generous: ~2x the measured 3.5 s, so slow CI never produces a false alarm. The point of
#: this number is to catch a change that doubles startup, not to police 10% drift.
#:
#: This is the app's OWN import cost: the probe loads the heavy third-party floor first, so
#: the figure excludes however long this host takes to map those C extensions. Without that
#: split the gate measures the machine — on a host whose security software rescans every
#: `.pyd`, `import sqlalchemy` alone measured 40 s and the budget could never be met however
#: lean the application was. A newly added heavy dependency is still caught: it is not in the
#: floor, so it lands in the measured window.
MAX_IMPORT_MS = 8000
#: Measured 1,422 after the deferrals; headroom for legitimate new dependencies.
MAX_MODULES = 1700
#: Route count is a contract. It changes only when someone adds or removes an endpoint, and
#: then this number is updated deliberately in the same commit.
EXPECTED_ROUTES = 1155

_PROBE = """
import json, sys, time

# The third-party floor, imported before the clock starts. These are not the repo's to make
# faster, and on some hosts they dominate everything else.
b = time.perf_counter()
import fastapi, httpx, sqlalchemy  # noqa: F401
floor_ms = (time.perf_counter() - b) * 1000

t = time.perf_counter()
import app.main as m
elapsed_ms = (time.perf_counter() - t) * 1000
print("RESULT" + json.dumps({
    "ms": elapsed_ms,
    "floor_ms": floor_ms,
    "modules": len(sys.modules),
    "openai": "openai" in sys.modules,
    "mcp": "mcp" in sys.modules,
    "routes": len([r for r in m.app.routes if hasattr(r, "methods")]),
}))
"""


@pytest.fixture(scope="module")
def cold_import() -> dict:
    """Import the app in a FRESH interpreter - in-process it is already imported by conftest."""
    # S603: the argv is `sys.executable` plus a module-constant probe string. No user, network
    # or filesystem input reaches it, and a subprocess is required because conftest has already
    # imported app.main in-process, which would make a warm import look like a cold one.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE],
        cwd=str(BACKEND), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"cold import failed:\n{proc.stderr[-4000:]}"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, f"probe produced no result:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    return json.loads(line[len("RESULT"):])


def test_openai_sdk_is_not_imported_at_startup(cold_import):
    """`openai` costs ~374 ms and is needed only when an OpenAI-family provider is built.

    `app/agent/factory.py` imports the provider modules inside `build_provider_for` branches.
    Moving any of them back to module scope puts the SDK on every cold start.
    """
    assert not cold_import["openai"], (
        "the openai SDK is being imported at startup again - check that "
        "app/agent/factory.py still imports its provider modules INSIDE build_provider_for"
    )


def test_mcp_sdk_is_not_imported_at_startup(cold_import):
    """`mcp` is needed only when an MCP server is actually spawned."""
    assert not cold_import["mcp"], (
        "the mcp SDK is being imported at startup again - check that app/mcp/client.py "
        "still imports it inside _session/_consent_elicitation_callback/__init__"
    )


def test_route_count_is_unchanged(cold_import):
    """Guards the router flattening: a mistake there silently drops or duplicates routes."""
    assert cold_import["routes"] == EXPECTED_ROUTES, (
        f"route count moved {EXPECTED_ROUTES} -> {cold_import['routes']}. If you added or "
        "removed an endpoint on purpose, update EXPECTED_ROUTES in the same commit."
    )


def test_module_count_stays_bounded(cold_import):
    assert cold_import["modules"] <= MAX_MODULES, (
        f"{cold_import['modules']} modules imported at startup (budget {MAX_MODULES}). "
        "Something heavy was added to module scope."
    )


def test_cold_import_stays_within_budget(cold_import):
    assert cold_import["ms"] <= MAX_IMPORT_MS, (
        f"import app.main took {cold_import['ms']:.0f} ms (budget {MAX_IMPORT_MS} ms), on top "
        f"of a {cold_import.get('floor_ms', 0):.0f} ms third-party floor. "
        "Profile with: python -X importtime -c 'import app.main'"
    )


def test_routers_are_registered_flat_not_nested():
    """Structural guard for the flattening.

    Nesting every sub-router inside one parent router costs a second full registration pass
    (measured 2,012 ms vs 999 ms). The saving is invisible in behaviour, so only a structural
    check keeps it: nothing may go back to `api.include_router(...)` for a sub-router.
    """
    src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    nested = [
        ln.strip() for ln in src.splitlines()
        if ln.strip().startswith("api.include_router(")
    ]
    assert not nested, (
        "sub-routers must be registered on `app` with prefix='/api', not nested via "
        f"`api.include_router(...)`: {nested[:5]}"
    )
