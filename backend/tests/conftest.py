"""Pytest bootstrap: make the `app` package importable when tests run from any CWD."""
import os
import sys

import pytest

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
    "test_coverage_pdf.py", "test_performance_pdf.py", "test_assessment_pdf.py",
    "test_coverage_suite.py", "test_coverage_runs.py", "test_coverage_trends.py",
    "test_coverage_cached_only.py",
    # Large estate-intelligence / governance matrices (broad, overlapping coverage).
    "test_tagintel.py", "test_tagintel_generate.py", "test_changeexplorer.py",
    "test_changeexplorer_nlquery.py", "test_cis_v5.py", "test_metric_chart.py",
    "test_inventory_scope.py", "test_assessment_hardening.py", "test_assessment_catalog.py",
    "test_graph_analytics.py", "test_graph_scope.py", "test_scope_batching.py",
    # Integration bridges + heavier tool/provider suites.
    "test_arm_rest_bridge.py", "test_arm_mg_hierarchy.py", "test_tool_protocol.py",
    "test_builtin_tools.py", "test_builtin_agents.py", "test_autopilot.py",
    "test_backup_restore.py", "test_security_e2e.py", "test_admin_demo.py",
    "test_workload_profile.py", "test_perfprofile.py", "test_perfprofile_runs.py",
    "test_perfprofile_tool.py", "test_missions.py", "test_teleintel.py",
    "test_reservations.py", "test_radar.py", "test_pricing.py",
}


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Auto-tag the heavy suites as ``slow`` so the default ``-m 'not slow'`` run is fast."""
    slow = pytest.mark.slow
    for item in items:
        if os.path.basename(str(item.fspath)) in _SLOW_TEST_FILES:
            item.add_marker(slow)
