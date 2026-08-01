"""The sign-in lookback window: served, bounded, and actually written.

The window is the only lever a reader has against the 200,000-row sign-in cap, so the
screen that suffers from the cap now offers it directly. Two things have to hold for that
control to be honest:

1. the API reports the window the NEXT collection will use *and* the window the numbers on
   screen already cover, because those differ between saving and re-collecting, and
2. saving actually persists.

The second is not hypothetical. ``AppSettingsUpdate`` is a whitelist, so before the field
was declared there the update answered 200, changed nothing, and the UI would have shown a
window the collector never used.
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import entra as entra_api
from app.core import app_settings
from app.entra import cache, demo
from app.entra import snapshot as snapshot_mod


class _Principal:
    tenant_id = demo.DEMO_TENANT
    subject = "dev"


@pytest.fixture(autouse=True)
def _demo_tenant(tmp_path, monkeypatch):
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation

    import app.core.azure_connections as ac

    monkeypatch.setattr(
        ac, "resolve_connection",
        lambda cid: {"id": "conn-demo", "tenant_id": demo.DEMO_TENANT} if cid == "conn-demo" else None,
    )
    demo.seed()
    yield
    cache.clear_memo()


def _overview():
    return asyncio.run(entra_api.signals_overview(connection_id="conn-demo", principal=_Principal()))


def test_overview_reports_the_window_and_its_bounds():
    lookback = _overview()["lookback"]
    assert lookback["min"] == 1, "the control has to be able to offer a single day"
    assert lookback["max"] == 90
    assert lookback["setting_key"] == "entra_signin_lookback_days"
    assert isinstance(lookback["days"], int)


def test_configured_window_and_collected_window_are_reported_separately(monkeypatch):
    """Saving a window does not re-read Graph, and the payload must not pretend it did."""
    before = _overview()["lookback"]
    collected = before["data_days"]

    original = snapshot_mod.settings()
    monkeypatch.setattr(snapshot_mod, "settings", lambda: {**original, "signin_lookback_days": 7})
    after = _overview()["lookback"]

    assert after["days"] == 7, "the next collection's window should follow the setting"
    assert after["data_days"] == collected, "the collected window must not move without a collection"


@pytest.mark.parametrize("value", [1, 7, 30, 90])
def test_the_settings_store_accepts_every_offerable_window(value, tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "_SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    saved = app_settings.save_settings({"entra_signin_lookback_days": value})
    assert saved["entra_signin_lookback_days"] == value


@pytest.mark.parametrize("value,expected", [
    (-5, 1),      # below the floor: clamped up
    (365, 90),    # above the ceiling: clamped down
    (0, 30),      # falsy, so the store treats it as "unset" and applies the default
])
def test_the_settings_store_clamps_rather_than_storing_nonsense(value, expected, tmp_path, monkeypatch):
    """Zero is the odd one out, which is why the API model rejects it before it gets here:
    the store's `or 30` reads it as absent rather than as a one-day floor, and a control
    that offered 0 would silently produce a month."""
    monkeypatch.setattr(app_settings, "_SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    saved = app_settings.save_settings({"entra_signin_lookback_days": value})
    assert saved["entra_signin_lookback_days"] == expected


def test_the_update_model_declares_the_field():
    """A whitelist that omits the field accepts the write and drops it silently."""
    from app.api.admin import AppSettingsUpdate

    assert "entra_signin_lookback_days" in AppSettingsUpdate.model_fields
    body = AppSettingsUpdate(entra_signin_lookback_days=7)
    assert body.model_dump(exclude_none=True) == {"entra_signin_lookback_days": 7}


@pytest.mark.parametrize("value", [0, 91, -1])
def test_the_update_model_rejects_a_window_outside_the_offered_range(value):
    from pydantic import ValidationError

    from app.api.admin import AppSettingsUpdate

    with pytest.raises(ValidationError):
        AppSettingsUpdate(entra_signin_lookback_days=value)
