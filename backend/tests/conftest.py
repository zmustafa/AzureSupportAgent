"""Pytest bootstrap: make the `app` package importable when tests run from any CWD."""
import os
import sys

import pytest

from app.alerts_manager import cache as alerts_manager_cache

# backend/ (the dir that contains the `app` package) — one level up from tests/.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# ---------------------------------------------------------------- fast vs. full test runs
# The default `pytest` run (see pyproject ``addopts = -m 'not slow'``) executes a fast core
# suite — the unit logic that guards day-to-day development. The heavier / lower-frequency
# suites below (PDF rendering, broad estate/coverage/RBAC/assessment matrices, integration
# bridges) are auto-tagged ``slow`` here and excluded by default, so the inner-loop test run
# builds and finishes quickly. Run EVERYTHING with ``pytest -m ""`` (CI), or just the heavy
# suites with ``pytest -m slow``. This keeps full coverage available while making the common
# case fast — no tests were deleted.
_SLOW_TEST_FILES: set[str] = {
    # Slow renderers (each PDF render is ~0.5–2.5s).
    "test_assessment_pdf.py",
    "test_coverage_suite.py", "test_coverage_runs.py", "test_coverage_trends.py",
    "test_coverage_cached_only.py",
    # Large estate-intelligence / governance matrices (broad, overlapping coverage).
    "test_tagintel.py", "test_tagintel_generate.py",
    "test_changeexplorer_nlquery.py", "test_cis_v5.py", "test_metric_chart.py",
    "test_inventory_scope.py", "test_assessment_hardening.py", "test_assessment_catalog.py",
    "test_graph_analytics.py", "test_graph_scope.py", "test_scope_batching.py",
    # Integration bridges + heavier tool/provider suites.
    "test_arm_rest_bridge.py", "test_arm_mg_hierarchy.py", "test_tool_protocol.py",
    "test_builtin_tools.py", "test_builtin_agents.py", "test_autopilot.py",
    "test_backup_restore.py", "test_security_e2e.py",
    "test_workload_profile.py", "test_perfprofile.py", "test_perfprofile_runs.py",
    "test_perfprofile_tool.py", "test_missions.py", "test_teleintel.py",
    "test_reservations.py", "test_radar.py", "test_pricing.py",
}


# ------------------------------------------------------- tests that need a live Azure env
# These assert behaviour that can only be exercised against a real Azure connection: they
# shell out to `az` or resolve a configured connection, and fail with "Please run 'az login'"
# or "No Azure connection is configured for this scope" when neither exists.
#
# They pass on a developer machine (which has an az session and .data/azure_connections.json,
# both gitignored) and cannot pass in CI. Skipping on the ACTUAL condition rather than tagging
# the whole file `slow` keeps them in the fast local loop, where they still catch regressions,
# while giving CI an honest skip instead of a failure.
_AZURE_REQUIRED_TESTS: set[str] = {
    "test_rule_apply_blocks_stale_state",
    "test_update_change_blocks_stale_azure_state",
    "test_bulk_simulator_resolves_visible_cross_subscription_group",
    "test_submit_and_plan_decision_create_only_approval_ledger_rows",
    "test_plan_decision_rejects_child_from_different_connection",
    "test_selected_gap_plan_is_server_built_approval_gated_and_status_is_ledger_aware",
}


def _azure_env_available() -> bool:
    """True when at least one Azure connection is configured for these tests to use."""
    try:
        from app.core.azure_connections import list_connections

        return bool(list_connections())
    except Exception:  # noqa: BLE001 - absence is the answer we want
        return False


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """Create the database schema once before any test runs.

    Some tests write through a background job rather than an HTTP request, so they never
    start the app lifespan and never trigger ``ensure_schema()``. On a developer machine
    that goes unnoticed because .data/app.db already exists from earlier runs; on a fresh
    clone (or CI) those tests fail with "no such table: audit_log".

    Uses a SYNCHRONOUS engine on purpose. Creating the schema through the app's async
    engine binds it to whichever loop happens to be active at session setup, which is the
    "bound to a different event loop" failure this suite has hit before. ``create_all`` is
    idempotent, so this is a no-op when the tables already exist.
    """
    from sqlalchemy import create_engine

    from app.core.config import Settings
    from app.core.db import Base

    import app.models  # noqa: F401 - registers every table on Base.metadata

    url = Settings().database_url
    # Only SQLite is set up here; a Postgres-backed run is expected to be migrated already.
    if url.startswith("sqlite"):
        sync_url = url.replace("+aiosqlite", "")
        path = sync_url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        engine = create_engine(sync_url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
    yield


@pytest.fixture(autouse=True)
def _isolate_alerts_manager_cache():
    alerts_manager_cache.reset_for_tests()
    yield
    alerts_manager_cache.reset_for_tests()


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Auto-tag the heavy suites as ``slow`` so the default ``-m 'not slow'`` run is fast,
    and skip the Azure-dependent tests when no connection is configured."""
    slow = pytest.mark.slow
    azure_ok = _azure_env_available()
    needs_azure = pytest.mark.skip(
        reason="needs a configured Azure connection (az session + .data/azure_connections.json)"
    )
    for item in items:
        if os.path.basename(str(item.fspath)) in _SLOW_TEST_FILES:
            item.add_marker(slow)
        if not azure_ok and item.name.split("[")[0] in _AZURE_REQUIRED_TESTS:
            item.add_marker(needs_azure)
