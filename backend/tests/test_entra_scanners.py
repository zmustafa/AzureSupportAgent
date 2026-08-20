"""Proactive scanners: selection, delta semantics and the findings ledger.

The delta is what decides whether this feature is used or filtered. These tests hold the
line on the two rules that matter: a scanner never notifies about findings it already
reported, and resolution is computed from the fingerprint disappearing rather than clicked
by a human who might simply have given up.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.entra import cache, model
from app.entra import scanners as sc
from app.entra import signals as sig
from app.entra.signals import SignalContext

TENANT = "scanner-tenant"
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    yield
    cache.clear_memo()


def _ctx():
    return SignalContext(now=NOW, tenant_id=TENANT)


def _finding(signal_id, object_id, severity="high", pillar="app"):
    return model.finding(
        signal_id=signal_id, severity=severity, pillar=pillar, object_kind="app",
        object_id=object_id, object_name=object_id, title=f"{signal_id} on {object_id}",
        evidence={},
    )


def _analysis(findings):
    return {"findings": list(findings)}


def _ok_domains(*names):
    return {n: {"name": n, "status": model.STATUS_OK} for n in names}


# ============================================================= registry integrity
def test_every_scanner_selects_at_least_one_real_signal():
    """A scanner that selects nothing is a screen full of green that means nothing."""
    for scanner in sc.registry():
        assert scanner.signals(), f"{scanner.id} selects no signals"


def test_scanner_ids_are_unique_and_namespaced():
    ids = [s.id for s in sc.registry()]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("entra.") for i in ids)


def test_every_always_immediate_signal_exists_in_the_registry():
    """A typo here would silently downgrade a break-glass capture to a digest line."""
    known = {s.id for s in sig.registry()}
    for signal_id in sc.ALWAYS_IMMEDIATE:
        assert signal_id in known, f"{signal_id} is not a registered signal"


def test_the_critical_sweep_covers_every_critical_signal():
    sweep = sc.SCANNER_BY_ID["entra.daily_critical"]
    critical = {s.id for s in sig.registry() if s.severity == "critical" and s.scannable}
    assert {s.id for s in sweep.signals()} == critical


# ================================================================== selection
def test_severity_floor_excludes_quieter_findings():
    scanner = sc.ScannerSpec(id="t", name="T", description="", cadence="daily",
                             severity_floor="high", pillars=("app",))
    spec_id = next(s.id for s in sig.registry() if s.pillar == "app")
    rows = [_finding(spec_id, "a", severity="high"), _finding(spec_id, "b", severity="low")]
    assert [f["object_id"] for f in sc.select(scanner, rows)] == ["a"]


def test_a_scanner_only_selects_its_own_signals():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    rows = [_finding("app.secret_expired", "a"), _finding("ppl.guest_stale", "b", pillar="ppl")]
    assert [f["object_id"] for f in sc.select(scanner, rows)] == ["a"]


def test_a_blind_domain_blocks_the_scanner_rather_than_reporting_zero():
    """'No findings' and 'could not look' are the same picture and opposite facts."""
    scanner = sc.SCANNER_BY_ID["entra.risk_sweep"]
    blind = {"risk": {"name": "risk", "status": model.STATUS_BLIND,
                      "error": "Missing IdentityRiskyUser.Read.All"}}
    reason = sc.unavailable_reason(scanner, blind)
    assert reason
    result = sc.run(scanner, TENANT, _analysis([]), blind)
    assert result["blocked"] == reason
    assert sc.should_notify(result) is False


# ====================================================================== delta
def test_first_run_reports_everything_as_new_and_says_it_was_the_first():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    result = sc.run(scanner, TENANT, _analysis([_finding("app.secret_expired", "a")]),
                    _ok_domains("apps"))
    assert result["first_run"] is True
    assert result["counts"] == {"total": 1, "new": 1, "resolved": 0, "persisting": 0}


def test_second_run_with_the_same_findings_reports_nothing_new():
    """The rule that keeps the digest readable."""
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    findings = [_finding("app.secret_expired", "a"), _finding("app.secret_expiring", "b")]
    sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))
    second = sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))
    assert second["counts"]["new"] == 0
    assert second["counts"]["persisting"] == 2
    assert sc.should_notify(second) is False


def test_a_disappearing_fingerprint_is_reported_as_resolved():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    first = [_finding("app.secret_expired", "a"), _finding("app.secret_expiring", "b")]
    sc.run(scanner, TENANT, _analysis(first), _ok_domains("apps"))
    second = sc.run(scanner, TENANT, _analysis(first[:1]), _ok_domains("apps"))
    assert second["counts"]["resolved"] == 1
    assert second["resolved_fingerprints"] == [first[1]["fingerprint"]]
    assert sc.should_notify(second) is True


def test_a_second_run_still_reports_what_the_scanner_currently_finds():
    """The delta decides what is NOTIFIED; it must not decide what is SHOWN.

    Once a baseline exists a healthy scanner reports zero new findings forever. A screen
    driven by the delta alone made a scanner sitting on hundreds of open findings look
    identical to one that had found nothing, so the totals have to survive the second run.
    """
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    findings = [_finding("app.secret_expired", "a"), _finding("app.secret_expiring", "b")]
    sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))
    second = sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))

    assert second["counts"]["new"] == 0, "nothing changed, so nothing is new"
    assert second["counts"]["total"] == 2, "but the scanner still reports two findings"
    assert sum(second["by_severity"].values()) == 2


def test_what_a_scanner_reports_does_not_depend_on_its_run_history():
    """Selection is a function of the snapshot alone.

    This is what lets the screen show results read-only. If reporting depended on stored
    run state, viewing results would have to record a run, and merely opening the screen
    would consume the delta the next real run depends on.
    """
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    findings = [_finding("app.secret_expired", "a"), _finding("app.secret_expiring", "b")]
    before = sc.select(scanner, findings)
    sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))
    sc.run(scanner, TENANT, _analysis(findings), _ok_domains("apps"))
    assert sc.select(scanner, findings) == before


def test_a_new_finding_after_a_quiet_run_is_notified():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    sc.run(scanner, TENANT, _analysis([]), _ok_domains("apps"))
    second = sc.run(scanner, TENANT, _analysis([_finding("app.secret_expired", "a")]),
                    _ok_domains("apps"))
    assert second["counts"]["new"] == 1
    assert sc.should_notify(second) is True


def test_a_blocked_run_does_not_overwrite_the_previous_fingerprints():
    """Otherwise one blind run would report the entire estate as newly resolved."""
    scanner = sc.SCANNER_BY_ID["entra.risk_sweep"]
    sc.run(scanner, TENANT, _analysis([_finding("risk.legacy_auth_success", "a", pillar="risk")]),
           _ok_domains("risk"))
    blocked = sc.run(scanner, TENANT, _analysis([]),
                     {"risk": {"name": "risk", "status": model.STATUS_BLIND, "error": "no perm"}})
    assert blocked["blocked"]
    restored = sc.run(scanner, TENANT,
                      _analysis([_finding("risk.legacy_auth_success", "a", pillar="risk")]),
                      _ok_domains("risk"))
    assert restored["counts"]["new"] == 0, "the blind run must not have cleared the baseline"


def test_always_immediate_findings_are_flagged_separately():
    scanner = sc.SCANNER_BY_ID["entra.breakglass"]
    result = sc.run(scanner, TENANT,
                    _analysis([_finding("ca.breakglass_over_covered", "bg1", severity="critical",
                                        pillar="ca")]),
                    _ok_domains("ca"))
    assert len(result["immediate"]) == 1
    assert sc.should_notify(result) is True


# ====================================================================== ledger
def test_ledger_records_first_seen_and_keeps_it_stable():
    findings = [_finding("app.secret_expired", "a")]
    first = sc.update_ledger(TENANT, findings, now="2026-01-01T00:00:00+00:00")
    fp = findings[0]["fingerprint"]
    assert first[fp]["first_seen"] == "2026-01-01T00:00:00+00:00"

    later = sc.update_ledger(TENANT, findings, now="2026-06-01T00:00:00+00:00")
    assert later[fp]["first_seen"] == "2026-01-01T00:00:00+00:00", "age must not reset"
    assert later[fp]["last_seen"] == "2026-06-01T00:00:00+00:00"


def test_resolution_is_computed_from_the_fingerprint_disappearing():
    findings = [_finding("app.secret_expired", "a")]
    sc.update_ledger(TENANT, findings, now="2026-01-01T00:00:00+00:00")
    ledger = sc.update_ledger(TENANT, [], now="2026-02-01T00:00:00+00:00")
    assert ledger[findings[0]["fingerprint"]]["resolved_at"] == "2026-02-01T00:00:00+00:00"


def test_a_returning_finding_clears_its_resolution():
    findings = [_finding("app.secret_expired", "a")]
    sc.update_ledger(TENANT, findings, now="2026-01-01T00:00:00+00:00")
    sc.update_ledger(TENANT, [], now="2026-02-01T00:00:00+00:00")
    back = sc.update_ledger(TENANT, findings, now="2026-03-01T00:00:00+00:00")
    assert "resolved_at" not in back[findings[0]["fingerprint"]]


def test_age_is_derived_from_first_seen():
    entry = {"first_seen": "2026-07-01T12:00:00+00:00"}
    assert sc.age_days(entry, _ctx()) == 30


# ==================================================================== scheduling
def test_a_scanner_that_never_ran_is_due():
    assert sc.due(sc.SCANNER_BY_ID["entra.daily_critical"], TENANT, _ctx()) is True


def test_a_daily_scanner_is_not_due_again_the_same_day():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    sc.run(scanner, TENANT, _analysis([]), _ok_domains("apps"), now=NOW.isoformat())
    assert sc.due(scanner, TENANT, _ctx()) is False


def test_a_weekly_scanner_waits_a_week():
    scanner = sc.SCANNER_BY_ID["entra.privileged_review"]
    sc.run(scanner, TENANT, _analysis([]), _ok_domains("roles"),
           now="2026-07-28T12:00:00+00:00")
    assert sc.due(scanner, TENANT, _ctx()) is False
    sc.run(scanner, TENANT, _analysis([]), _ok_domains("roles"),
           now="2026-07-20T12:00:00+00:00")
    assert sc.due(scanner, TENANT, _ctx()) is True


def test_sweep_reports_what_it_skipped_and_why():
    sc.run(sc.SCANNER_BY_ID["entra.credential_expiry"], TENANT, _analysis([]),
           _ok_domains("apps"), now=NOW.isoformat())
    result = sc.sweep(TENANT, _analysis([]), _ok_domains("apps", "roles", "ca", "people"), _ctx())
    skipped = {s["scanner_id"]: s["reason"] for s in result.skipped}
    assert skipped.get("entra.credential_expiry") == "not due"


def test_forcing_a_sweep_runs_everything():
    result = sc.sweep(TENANT, _analysis([]), _ok_domains(*[
        "tenant", "people", "apps", "roles", "pim", "ca", "risk", "governance"]),
        _ctx(), force=True)
    assert len(result.ran) == len(sc.registry())
    assert result.skipped == []


# ================================================================ notification
def test_summary_names_what_changed_and_why_it_matters():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    result = sc.run(scanner, TENANT, _analysis([_finding("app.secret_expired", "a")]),
                    _ok_domains("apps"))
    body = sc.summarize(result)
    assert "Credential expiry watch" in body
    assert "app.secret_expired on a" in body
    spec = sig.by_id("app.secret_expired")
    assert spec.why[:30] in body, "the notification must say why, not just what"


def test_a_blocked_scanner_summarises_the_blockage():
    scanner = sc.SCANNER_BY_ID["entra.risk_sweep"]
    result = sc.run(scanner, TENANT, _analysis([]),
                    {"risk": {"name": "risk", "status": model.STATUS_BLIND,
                              "error": "Missing IdentityRiskyUser.Read.All"}})
    assert "could not run" in sc.summarize(result)


def test_notification_severity_tracks_the_worst_new_finding():
    scanner = sc.SCANNER_BY_ID["entra.credential_expiry"]
    quiet = sc.run(scanner, TENANT, _analysis([]), _ok_domains("apps"))
    assert sc.notification_severity(quiet) == "info"

    loud = sc.run(scanner, TENANT,
                  _analysis([_finding("app.secret_expired", "a", severity="critical")]),
                  _ok_domains("apps"))
    assert sc.notification_severity(loud) == "critical"
