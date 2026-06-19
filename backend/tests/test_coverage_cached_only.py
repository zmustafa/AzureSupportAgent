"""The coverage GET endpoints are cached-only: navigating to a scope with no saved scan must
NOT trigger a live Azure compute (which can hang and strand the view on "Loading…"). Instead
the GET returns a lightweight ``report_exists=False`` sentinel the UI renders as an empty
"run first scan" state. Computing happens only on an explicit Refresh (``force=True``).
"""
from __future__ import annotations

import pytest

from app.api import amba, backupdr, telemetry
from app.core.security import Principal

_FEATURES = [amba, telemetry, backupdr]
_P = Principal(subject="t", email="t@t", tenant_id="probe-tenant-cov", role="admin")


@pytest.mark.parametrize("mod", _FEATURES, ids=lambda m: m.router.prefix.strip("/"))
async def test_get_is_cached_only_no_report(mod):
    """A non-demo scope with no cache returns report_exists=False and empty data, fast."""
    _kind, _id, snap = await mod.latest_snapshot(_P, "no-such-workload-xyz", None)
    assert snap.get("report_exists") is False
    assert (snap.get("all_resources") or []) == []
    assert (snap.get("gaps") or []) == []


@pytest.mark.parametrize("mod", _FEATURES, ids=lambda m: m.router.prefix.strip("/"))
async def test_demo_scope_reports_exists(mod):
    """Demo scopes seed synthetic data, so a report always exists for them."""
    snap = await mod._get_snapshot(_P, "workload", mod.demo.DEMO_WORKLOAD_ID, force=False, compute=False)
    assert snap.get("report_exists") is True
    assert len(snap.get("all_resources") or []) > 0
