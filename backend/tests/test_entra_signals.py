"""Signal Registry integrity + per-signal behavior.

The registry is the single source of truth behind the score, the findings inbox, the
scanners, the exports and the agent answers, so the invariants here matter more than the
individual assertions: unique ids, known pillars, declared domains, and — above all — that
a check which *cannot* be performed reports "not measured" instead of quietly returning
zero findings and improving the score.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.entra import demo, model
from app.entra import signals as sig
from app.entra.signals import SignalContext, SignalUnavailable


def sig_domains() -> set[str]:
    """Every domain any signal declares — the full set a healthy tenant would have."""
    return {d for spec in sig.registry() for d in spec.domains}


@pytest.fixture(scope="module")
def demo_snapshot(tmp_path_factory):
    from app.entra import cache, snapshot as snapshot_mod

    root = tmp_path_factory.mktemp("entra-signals")
    cache.set_root_for_tests(root)
    demo.seed()
    snap = snapshot_mod.analyze(demo.DEMO_TENANT, force=True)
    yield snap


# ------------------------------------------------------------------ registry invariants
def test_registry_ids_are_unique_and_namespaced():
    ids = [s.id for s in sig.registry()]
    assert len(ids) == len(set(ids))
    for spec in sig.registry():
        assert spec.id.startswith(f"{spec.pillar}."), f"{spec.id} is not namespaced by its pillar"


def test_every_signal_declares_a_known_pillar_and_severity():
    for spec in sig.registry():
        assert spec.pillar in sig.PILLAR_BY_KEY
        assert spec.severity in model.SEVERITIES
        assert spec.object_kind in model.OBJECT_KINDS
        assert 1 <= spec.weight <= 10


def test_pillar_weights_sum_to_one_hundred():
    assert sum(p["weight"] for p in sig.PILLARS) == 100


def test_every_signal_has_a_question_why_and_remediation():
    """These three strings are what turn a finding into something an admin can act on."""
    for spec in sig.registry():
        assert spec.question.strip(), f"{spec.id} has no question"
        assert spec.why.strip(), f"{spec.id} has no rationale"
        assert spec.remediation.strip(), f"{spec.id} has no remediation"
        assert spec.doc_link.startswith("https://"), f"{spec.id} has no documentation link"


def test_ratio_signals_declare_a_population():
    for spec in sig.registry():
        if spec.impact == sig.IMPACT_RATIO:
            assert spec.population is not None, f"{spec.id} is a ratio signal with no population"


def test_every_signal_declares_the_domains_it_reads():
    for spec in sig.registry():
        assert spec.domains, f"{spec.id} declares no domains, so it can never be marked not-measured"


# -------------------------------------------------------------------- evaluation shape
def test_demo_tenant_triggers_a_broad_spread_of_signals(demo_snapshot):
    analysis = demo_snapshot["_analysis"]
    fired = {sid for sid, n in analysis["by_signal"].items() if n}
    pillars_fired = {sig.by_id(s).pillar for s in fired}
    # A demo that only exercises one pillar demonstrates nothing.
    assert {"auth", "ca", "priv", "app", "ppl"} <= pillars_fired
    assert len(fired) >= 40


def test_no_signal_raised_an_unexpected_error(demo_snapshot):
    assert demo_snapshot["_analysis"]["errors"] == {}


def test_findings_carry_evidence_and_a_stable_fingerprint(demo_snapshot):
    for f in demo_snapshot["_analysis"]["findings"]:
        assert f["fingerprint"], f"{f['signal_id']} produced a finding with no fingerprint"
        assert isinstance(f["evidence"], dict)
        assert f["evidence"], f"{f['signal_id']} produced a finding with no evidence"


def test_fingerprints_are_unique_within_a_run(demo_snapshot):
    seen = [f["fingerprint"] for f in demo_snapshot["_analysis"]["findings"]]
    assert len(seen) == len(set(seen))


def test_fingerprint_ignores_counts_and_timestamps():
    """Delta notifications, snoozing and ticket links all key off this being stable."""
    a = model.fingerprint("priv.standing_global_admin", "u-1", "rd-ga")
    b = model.fingerprint("priv.standing_global_admin", "u-1", "rd-ga")
    assert a == b
    assert a != model.fingerprint("priv.standing_global_admin", "u-2", "rd-ga")


# ------------------------------------------------------------- blind is not clean
def test_missing_capability_reports_not_measured_rather_than_zero_findings():
    """The failure mode this product must never have: a tenant we could not measure
    scoring as though it were clean."""
    from app.entra.signal_defs import auth as auth_defs

    spec = next(s for s in auth_defs.SPECS if s.id == "auth.no_mfa_registered")
    data = {
        "people": {"users": [{"id": "u1", "enabled": True, "user_type": "Member"}],
                   "capabilities": {"mfa_registration_report": False}},
        "roles": {},
    }
    with pytest.raises(SignalUnavailable):
        spec.evaluate(data, SignalContext())


def test_evaluate_all_records_not_measured_for_blind_domains():
    # Every other domain is healthy, so `people` is unambiguously the reason a people
    # signal could not run. A signal spanning two domains reports the FIRST missing one,
    # which would make this assertion meaningless if risk or governance were also absent.
    domain_meta = {d: {"status": model.STATUS_OK} for d in sig_domains()}
    domain_meta["people"] = {"status": model.STATUS_BLIND,
                             "missing_permissions": ["User.Read.All"]}
    result = sig.evaluate_all({d: {} for d in domain_meta if d != "people"},
                              domain_meta, SignalContext(),
                              {"detected": True, "p1": True, "p2": True, "governance": True,
                               "workload_id_premium": True})
    people_signals = [s.id for s in sig.registry() if "people" in s.domains]
    for sid in people_signals:
        assert sid in result.not_measured
        assert "User.Read.All" in result.not_measured[sid] or "not permitted" in result.not_measured[sid]
        assert sid not in result.measured


def test_unlicensed_signals_are_not_measured():
    domain_meta = {d: {"status": model.STATUS_OK} for d in ("tenant", "people", "apps", "roles", "ca")}
    result = sig.evaluate_all({d: {} for d in domain_meta}, domain_meta, SignalContext(),
                              {"detected": True, "p1": True, "p2": False})
    p2_signals = [s.id for s in sig.registry() if s.licence == "p2"]
    assert p2_signals
    for sid in p2_signals:
        assert "Entra ID P2" in result.not_measured.get(sid, "")


def test_undetected_licence_still_attempts_the_check():
    """License flags are advisory — a real 403 is authoritative, a guess is not."""
    from app.entra.licences import licence_ok

    assert licence_ok({"detected": False}, "p2") is True
    assert licence_ok({"detected": True, "p2": False}, "p2") is False
    assert licence_ok({"detected": True, "p2": True}, "p2") is True


# ---------------------------------------------------------------------- suppression
def test_suppressed_fingerprints_are_removed_from_the_result(demo_snapshot):
    analysis = demo_snapshot["_analysis"]
    victim = analysis["findings"][0]
    data = dict(demo_snapshot["data"])
    ctx = SignalContext(suppressions={victim["fingerprint"]}, tenant_id=demo.DEMO_TENANT)
    result = sig.evaluate_all(data, demo_snapshot["domains"], ctx, demo_snapshot["licences"])
    assert victim["fingerprint"] not in {f["fingerprint"] for f in result.findings}


# -------------------------------------------------------------------------- thresholds
def test_thresholds_come_from_context_not_constants(demo_snapshot):
    """Changing the stale window must change the result — otherwise a literal is hiding
    somewhere in an evaluate body."""
    data = demo_snapshot["data"]
    meta = demo_snapshot["domains"]
    lax = sig.evaluate_all(data, meta, SignalContext(stale_days=3650), demo_snapshot["licences"])
    strict = sig.evaluate_all(data, meta, SignalContext(stale_days=1), demo_snapshot["licences"])
    assert strict.by_signal.get("ppl.stale_user", 0) > lax.by_signal.get("ppl.stale_user", 0)


def test_expiry_window_controls_expiring_credentials(demo_snapshot):
    data = demo_snapshot["data"]
    meta = demo_snapshot["domains"]
    narrow = sig.evaluate_all(data, meta, SignalContext(expiry_window_days=1), demo_snapshot["licences"])
    wide = sig.evaluate_all(data, meta, SignalContext(expiry_window_days=365), demo_snapshot["licences"])
    assert wide.by_signal.get("app.secret_expiring", 0) >= narrow.by_signal.get("app.secret_expiring", 0)


def test_context_time_is_injected_not_read_from_the_clock():
    ctx = SignalContext(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert ctx.days_since((datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(days=10)).isoformat()) == 10
    assert ctx.days_until((datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=5)).isoformat()) == 5
