"""Identity Posture Score behavior.

The score's credibility rests on three properties, each of which has a test here:
determinism, "blind is not zero", and scale invariance. A score that quietly punishes a
tenant for a permission we were never granted is worse than no score at all.
"""
from __future__ import annotations

import pytest

from app.entra import demo, model
from app.entra import score as score_mod
from app.entra import signals as sig
from app.entra.signals import SignalContext


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    from app.entra import cache, snapshot as snapshot_mod

    cache.set_root_for_tests(tmp_path_factory.mktemp("entra-score"))
    demo.seed()
    yield snapshot_mod.analyze(demo.DEMO_TENANT, force=True)


def _score(snapshot, *, domain_meta=None, licences=None, ctx=None):
    data = snapshot["data"]
    meta = domain_meta or snapshot["domains"]
    result = sig.evaluate_all(data, meta, ctx or SignalContext(), licences or snapshot["licences"])
    return score_mod.compute(data, result, ctx or SignalContext())


# ------------------------------------------------------------------------ determinism
def test_score_is_deterministic(seeded):
    a = _score(seeded)
    b = _score(seeded)
    assert a["score"] == b["score"]
    assert a["coverage"] == b["coverage"]
    assert [p["score"] for p in a["pillars"]] == [p["score"] for p in b["pillars"]]


def test_score_and_coverage_are_always_published_together(seeded):
    s = _score(seeded)
    assert 0 <= s["score"] <= 100
    assert 0.0 <= s["coverage"] <= 1.0
    assert s["measured_weight"] + sum(
        p["weight"] for p in s["pillars"] if p["score"] is None
    ) == s["total_weight"]


# --------------------------------------------------------------- blind is not zero
def test_unmeasured_pillar_is_excluded_not_zeroed(seeded):
    """A tenant without P2 must not look catastrophic because PIM data was unavailable.

    Constructed rather than assumed: the demo tenant is fully licensed and fully readable,
    so the blind state is induced here instead of depending on the demo happening to have
    a gap — which it did until the risk and governance domains landed."""
    blinded_meta = dict(seeded["domains"])
    for name in ("pim", "risk"):
        blinded_meta[name] = {"status": model.STATUS_UNLICENSED,
                              "error": "Requires Entra ID P2.", "missing_permissions": []}
    s = _score(seeded, domain_meta=blinded_meta)
    unmeasured = [p for p in s["pillars"] if p["score"] is None]
    assert unmeasured, "unlicensing PIM and risk must leave at least one pillar unmeasured"
    for p in unmeasured:
        assert p["score"] is None          # never 0
        assert p["state"] in ("unlicensed", "blind", "error", "not_collected", "not_implemented")
        # Every unmeasured pillar must be able to say WHY, in one human sentence.
        assert p["reason"], f"pillar {p['key']} is unmeasured with no reason"
    assert s["coverage"] < 1.0


def test_blinding_a_pillar_does_not_crater_the_score(seeded):
    full = _score(seeded)
    blinded_meta = dict(seeded["domains"])
    blinded_meta["apps"] = {"status": model.STATUS_BLIND, "missing_permissions": ["Application.Read.All"]}
    blinded = _score(seeded, domain_meta=blinded_meta)
    app_before = next(p for p in full["pillars"] if p["key"] == "app")
    app_after = next(p for p in blinded["pillars"] if p["key"] == "app")
    # Losing the apps domain removes most of that pillar's model. What remains (the two
    # tenant-level consent checks) is still scored, but the pillar reports honestly how
    # little of itself it could measure — and tenant coverage drops accordingly.
    assert app_after["measured_signals"] < app_before["measured_signals"]
    assert app_after["measured_fraction"] < app_before["measured_fraction"]
    assert blinded["coverage"] < full["coverage"]
    # And the untouched pillars keep their own scores unchanged.
    for key in ("auth", "ppl"):
        assert (next(p for p in blinded["pillars"] if p["key"] == key)["score"]
                == next(p for p in full["pillars"] if p["key"] == key)["score"])


def test_fully_blind_pillar_scores_none_not_zero(seeded):
    """The headline contract: a pillar we could not measure at all is excluded from the
    denominator, never scored 0."""
    meta = dict(seeded["domains"])
    meta["ca"] = {"status": model.STATUS_BLIND, "missing_permissions": ["Policy.Read.All"]}
    meta["people"] = {"status": model.STATUS_BLIND, "missing_permissions": ["User.Read.All"]}
    meta["roles"] = {"status": model.STATUS_BLIND, "missing_permissions": ["RoleManagement.Read.Directory"]}
    s = _score(seeded, domain_meta=meta)
    ca_pillar = next(p for p in s["pillars"] if p["key"] == "ca")
    assert ca_pillar["score"] is None
    assert ca_pillar["state"] == "blind"
    assert ca_pillar["measured_fraction"] == 0.0


def test_grade_is_withheld_below_the_coverage_floor(seeded):
    meta = {d: {"status": model.STATUS_BLIND, "missing_permissions": ["x"]} for d in seeded["domains"]}
    meta["tenant"] = {"status": model.STATUS_OK}
    s = _score(seeded, domain_meta=meta)
    assert s["coverage"] < score_mod.MIN_COVERAGE_FOR_GRADE
    assert s["grade"] == ""
    assert "misleading" in s["grade_withheld_reason"]


# ------------------------------------------------------------------- impact shapes
def test_binary_impact_is_all_or_nothing():
    spec = sig.by_id("app.user_consent_unrestricted")
    assert sig.penalty_units(spec, 0, {}) == 0
    assert sig.penalty_units(spec, 1, {}) == sig.penalty_units(spec, 50, {})


def test_saturating_impact_caps_small_n_criticals():
    spec = sig.by_id("priv.standing_global_admin")
    one = sig.penalty_units(spec, 1, {})
    three = sig.penalty_units(spec, spec.saturation, {})
    thirty = sig.penalty_units(spec, 30, {})
    assert 0 < one < three
    assert three == thirty          # thirty is not ten times worse than three


def test_ratio_impact_is_scale_invariant():
    """The same proportion of MFA-less users must score the same in a 200 and a 200,000
    user tenant — otherwise growth alone moves the score."""
    spec = sig.by_id("ppl.stale_user")

    def population(n):
        return {"people": {"users": [{"id": str(i), "enabled": True, "user_type": "Member"}
                                     for i in range(n)]}}

    small = sig.penalty_units(spec, 20, population(200))
    large = sig.penalty_units(spec, 20_000, population(200_000))
    assert small == pytest.approx(large)


def test_ratio_impact_is_clamped_at_one():
    spec = sig.by_id("ppl.stale_user")
    data = {"people": {"users": [{"id": "1", "enabled": True, "user_type": "Member"}]}}
    assert sig.penalty_units(spec, 50, data) == sig.max_units(spec) * 0.4  # medium severity


# ------------------------------------------------------------------ recoverable points
def test_top_wins_are_ordered_by_recoverable_points(seeded):
    s = _score(seeded)
    points = [w["points"] for w in s["top_wins"]]
    assert points == sorted(points, reverse=True)
    assert all(w["points"] >= 0 for w in s["top_wins"])
    assert all(w["remediation"] for w in s["top_wins"])


def test_resolving_a_win_moves_the_score_by_about_the_advertised_amount(seeded):
    """The 'biggest wins' list is only useful if the number on it is true."""
    base = _score(seeded)
    win = base["top_wins"][0]
    suppress = {
        f["fingerprint"] for f in seeded["_analysis"]["findings"] if f["signal_id"] == win["signal_id"]
    }
    ctx = SignalContext(suppressions=suppress)
    after = _score(seeded, ctx=ctx)
    gained = after["score"] - base["score"]
    assert gained == pytest.approx(win["points"], abs=1.0)


# --------------------------------------------------------------------------- history
def test_history_entry_records_the_registry_version(seeded):
    s = _score(seeded)
    entry = score_mod.history_entry(s, "snap-1", "2026-07-30T00:00:00+00:00")
    assert entry["registry_version"] == sig.registry_version()
    assert set(entry["pillars"]) == {p["key"] for p in sig.PILLARS}


def test_diff_findings_reports_new_resolved_and_persisting():
    a = [{"fingerprint": "f1"}, {"fingerprint": "f2"}]
    b = [{"fingerprint": "f2"}, {"fingerprint": "f3"}]
    diff = score_mod.diff_findings(b, a)
    assert diff["counts"] == {"new": 1, "resolved": 1, "persisting": 1}
    assert diff["new"][0]["fingerprint"] == "f3"
    assert diff["resolved"][0]["fingerprint"] == "f1"
