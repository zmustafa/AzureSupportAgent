"""Reference-set editor round-trip tests for AMBA / Telemetry / Backup-DR / Radar.

Each reference module persists to .data/<feature>_reference.json (+ _revisions). Tests
monkeypatch those paths to a tmp dir so the real reference sets are never mutated.
"""
from __future__ import annotations

import pytest

from app.amba import reference as amba_ref
from app.telemetry import reference as tel_ref
from app.backupdr import reference as bdr_ref
from app.radar import reference as radar_ref


def _isolate(monkeypatch, tmp_path, mod):
    monkeypatch.setattr(mod, "_PATH", tmp_path / f"{mod.__name__}_ref.json")
    monkeypatch.setattr(mod, "_REV_PATH", tmp_path / f"{mod.__name__}_rev.json")


@pytest.mark.parametrize("mod", [amba_ref, tel_ref, bdr_ref])
def test_typed_reference_roundtrip(monkeypatch, tmp_path, mod):
    _isolate(monkeypatch, tmp_path, mod)

    ref = mod.load_reference()
    assert ref["version"] == 0  # fresh seed = builtin, unsaved
    assert ref["types"], "seed must include types"
    if mod is amba_ref:
        disks = {item["key"]: item for item in ref["types"]["microsoft.compute/disks"]["alerts"]}
        cosmos = {item["key"]: item for item in ref["types"]["microsoft.documentdb/databaseaccounts"]["alerts"]}
        assert disks["disk_iops_saturation"]["deployable"] is False
        assert cosmos["cosmos_429"]["aggregation"] == "Count"
    n_types = len(ref["types"])

    # Save an edit (drop one type) → version bumps, revision recorded.
    types = dict(ref["types"])
    dropped_key = next(iter(types))
    types.pop(dropped_key)
    saved = mod.save_reference(types, actor="tester", reason="drop one type")
    assert saved["version"] == 1
    assert len(saved["types"]) == n_types - 1
    assert saved["updated_by"] == "tester"

    revs = mod.list_revisions()
    assert len(revs) >= 1
    assert revs[0]["version"] == 1

    # Restore a recorded revision by id → non-destructive, bumps the version forward.
    target = revs[-1]
    restored = mod.restore_revision(target["id"], actor="tester")
    assert restored is not None
    assert restored["version"] >= 2

    # Reset to builtin returns the full seed set again.
    reset = mod.reset_to_builtin(actor="tester")
    assert len(reset["types"]) == n_types


@pytest.mark.parametrize("mod", [amba_ref, tel_ref, bdr_ref])
def test_typed_reference_restore_unknown_revision(monkeypatch, tmp_path, mod):
    _isolate(monkeypatch, tmp_path, mod)
    mod.load_reference()
    assert mod.restore_revision("does-not-exist", actor="tester") is None


@pytest.mark.parametrize("mod", [amba_ref, tel_ref, bdr_ref])
def test_typed_reference_save_empty_types(monkeypatch, tmp_path, mod):
    _isolate(monkeypatch, tmp_path, mod)
    mod.load_reference()
    saved = mod.save_reference({}, actor="tester", reason="empty")
    assert saved["types"] == {}
    assert saved["version"] == 1
    # Recovery: reset brings the builtin set back.
    reset = mod.reset_to_builtin(actor="tester")
    assert len(reset["types"]) > 0


def test_radar_reference_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, radar_ref)
    ref = radar_ref.load_reference()
    assert ref["version"] == 0
    rules = ref["classification_rules"]
    models = ref["model_lifecycle"]
    assert isinstance(rules, list) and isinstance(models, list)

    saved = radar_ref.save_reference(
        classification_rules=rules[:-1] if rules else [],
        model_lifecycle=models,
        actor="tester",
        reason="drop one rule",
    )
    assert saved["version"] == 1

    revs = radar_ref.list_revisions()
    assert len(revs) >= 1

    reset = radar_ref.reset_to_builtin(actor="tester")
    assert len(reset["classification_rules"]) == len(rules)
    assert len(reset["model_lifecycle"]) == len(models)


def test_radar_restore_unknown_revision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, radar_ref)
    radar_ref.load_reference()
    assert radar_ref.restore_revision("nope", actor="tester") is None
