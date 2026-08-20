"""P2 platform surfaces: proactive scanners, their delta and their delivery.

A scanner detects nothing of its own — it selects signals, applies a severity floor and reports
what changed since it last looked. So the tests that matter are not about detection. They are
about the three ways this surface can lie:

  - reporting zero because it could not look;
  - consuming its own delta when somebody merely opens the screen;
  - notifying so much that the notifications get filtered, after which it detects nothing
    however good the signals are.
"""
from __future__ import annotations

import pytest

from app.iam import cache, demo, findings, scanners, signals as sig


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _evaluate(tenant="t1"):
    results = findings.evaluate(tenant)
    return [f.public() for r in results for f in r.findings], results


# =========================================================================== registry hygiene
def test_every_scanner_selects_at_least_one_real_signal():
    """A scanner selecting nothing renders as a permanently clean card. It is indistinguishable
    from a scanner covering a healthy tenant, and there is no error anywhere to explain it."""
    for spec in scanners.registry():
        assert spec.signals(), f"{spec.id} selects no signal in the registry"


def test_every_always_immediate_id_exists_in_the_registry():
    """The first draft of this list invented 8 of its 9 ids from memory. Each one silently
    selected nothing, so the bypass-the-digest path would have delivered NOTHING while looking
    correctly configured."""
    known = {s.id for s in sig.all_signals()}
    unknown = [i for i in scanners.ALWAYS_IMMEDIATE if i not in known]
    assert not unknown, f"ALWAYS_IMMEDIATE names signals that do not exist: {unknown}"


def test_scanner_ids_are_unique_and_namespaced():
    ids = [s.id for s in scanners.registry()]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("iam.") for i in ids)


def test_cadences_are_known():
    assert all(s.cadence in scanners.CADENCES for s in scanners.registry())


def test_severity_floors_are_iam_vocabulary_not_entra():
    """IAM grades critical/error/warning/info; Entra grades info..critical. Reusing Entra's
    ordering here would invert every floor and make the critical scanner report everything."""
    for s in scanners.registry():
        assert s.severity_floor in sig.SEVERITIES


def test_the_severity_floor_is_at_or_above_not_at_or_below():
    """Exercised through `select`, not by re-deriving the comparison.

    IAM ranks critical=0 … info=3, the inverse of Entra's scale. A test that recomputes the
    filter here would agree with a `select` that had the comparison backwards, and the critical
    scanner would report everything EXCEPT critical while every test stayed green."""
    sid = sig.all_signals()[0].id
    spec = scanners.ScannerSpec(id="iam.t", name="t", description="", cadence="daily",
                                severity_floor="error", signal_ids=(sid,))
    rows = [{"signal_id": sid, "severity": s, "id": s}
            for s in ("critical", "error", "warning", "info")]
    kept = {f["severity"] for f in scanners.select(spec, rows)}
    assert kept == {"critical", "error"}, "an 'error' floor must include critical, not exclude it"


def test_an_info_floor_reports_everything_and_a_critical_floor_only_critical():
    sid = sig.all_signals()[0].id
    rows = [{"signal_id": sid, "severity": s, "id": s}
            for s in ("critical", "error", "warning", "info")]
    wide = scanners.ScannerSpec(id="iam.w", name="w", description="", cadence="daily",
                                severity_floor="info", signal_ids=(sid,))
    narrow = scanners.ScannerSpec(id="iam.n", name="n", description="", cadence="daily",
                                  severity_floor="critical", signal_ids=(sid,))
    assert len(scanners.select(wide, rows)) == 4
    assert {f["severity"] for f in scanners.select(narrow, rows)} == {"critical"}


# =========================================================================== blindness
def test_a_scanner_whose_every_signal_is_unmeasured_is_blocked_not_clean(isolated_cache):
    """The whole point of the card. Zero findings and "could not look" are the same picture and
    opposite facts, and the reassuring one is wrong."""
    spec = scanners.ScannerSpec(id="iam.t", name="t", description="", cadence="daily",
                                pillars=("priv",))
    results = [
        sig.SignalResult(spec=s, findings=[], measured=False, reason="PIM was not collected.")
        for s in spec.signals()
    ]
    out = scanners.run(spec, "t1", [], results, persist=False)
    assert out["blocked"] == ["PIM was not collected."]
    assert out["counts"] is None, "a blocked scanner must withhold its counts, not publish zero"


def test_one_measured_signal_is_enough_to_make_the_count_meaningful(isolated_cache):
    """Blocking on ANY unmeasured signal would blank a card that has real findings on it."""
    spec = scanners.ScannerSpec(id="iam.t", name="t", description="", cadence="daily",
                                pillars=("priv",))
    picked = spec.signals()
    results = [sig.SignalResult(spec=s, findings=[], measured=i == 0, reason="" if i == 0 else "no data")
               for i, s in enumerate(picked)]
    out = scanners.run(spec, "t1", [], results, persist=False)
    assert out["blocked"] == []
    assert out["counts"] == {"total": 0, "new": 0, "resolved": 0, "persisting": 0}
    # …but the checks that could not run are still named, so the zero is qualified.
    assert len(out["unmeasured"]) == len(picked) - 1


def test_a_blocked_scanner_does_not_move_its_own_baseline(isolated_cache):
    spec = scanners.ScannerSpec(id="iam.t", name="t", description="", cadence="daily",
                                pillars=("priv",))
    results = [sig.SignalResult(spec=s, findings=[], measured=False, reason="no data")
               for s in spec.signals()]
    scanners.run(spec, "t1", [], results)
    assert scanners.read_runs("t1") == {}, "a scan that could not look must not record a run"


# =========================================================================== the delta
def _spec_over(signal_id: str) -> scanners.ScannerSpec:
    return scanners.ScannerSpec(id="iam.t", name="t", description="", cadence="daily",
                                severity_floor="info", signal_ids=(signal_id,))


def _measured(spec: scanners.ScannerSpec) -> list[sig.SignalResult]:
    return [sig.SignalResult(spec=s, findings=[], measured=True) for s in spec.signals()]


def test_new_resolved_and_persisting_partition_the_findings(isolated_cache):
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    results = _measured(spec)
    first = [{"id": "a", "signal_id": sid, "severity": "error"},
             {"id": "b", "signal_id": sid, "severity": "error"}]
    r1 = scanners.run(spec, "t1", first, results)
    assert r1["first_run"] is True
    assert r1["counts"] == {"total": 2, "new": 2, "resolved": 0, "persisting": 0}

    second = [{"id": "b", "signal_id": sid, "severity": "error"},
              {"id": "c", "signal_id": sid, "severity": "error"}]
    r2 = scanners.run(spec, "t1", second, results)
    assert r2["first_run"] is False
    assert r2["counts"] == {"total": 2, "new": 1, "resolved": 1, "persisting": 1}
    assert [f["id"] for f in r2["new"]] == ["c"]
    assert r2["resolved_fingerprints"] == ["a"]


def test_total_always_equals_new_plus_persisting(isolated_cache):
    """The arithmetic a reader does without being asked. It only holds because fingerprints are
    unique — a colliding pair silently vanishes from the delta while still counting toward the
    total, which is how this surface first reported `total 128, new 35, persisting 0`."""
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    results = _measured(spec)
    rows = [{"id": f"fp{i}", "signal_id": sid, "severity": "warning"} for i in range(5)]
    out = scanners.run(spec, "t1", rows, results)
    c = out["counts"]
    assert c["total"] == c["new"] + c["persisting"]


def test_reading_a_card_must_not_consume_the_delta(isolated_cache):
    """`persist=False` is the whole reason GET /scanners is safe. If viewing recorded a run, the
    first person to open the screen each morning would turn everyone else's "3 new" into
    "0 new" — and nobody would ever see a notification-worthy change on the screen itself."""
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    results = _measured(spec)
    rows = [{"id": "a", "signal_id": sid, "severity": "error"}]

    scanners.run(spec, "t1", rows, results, persist=False)
    scanners.run(spec, "t1", rows, results, persist=False)
    assert scanners.read_runs("t1") == {}

    again = scanners.run(spec, "t1", rows, results, persist=False)
    assert again["counts"]["new"] == 1, "a read must keep reporting the same delta"


# =========================================================================== the ledger
def test_the_ledger_records_first_seen_and_computes_resolution(isolated_cache):
    a = [{"id": "a", "signal_id": "s", "severity": "error"}]
    scanners.update_ledger("t1", a, now="2026-01-01T00:00:00+00:00")
    ledger = scanners.update_ledger("t1", [], now="2026-01-05T00:00:00+00:00")
    assert ledger["a"]["first_seen"] == "2026-01-01T00:00:00+00:00"
    assert ledger["a"]["resolved_at"] == "2026-01-05T00:00:00+00:00"


def test_a_finding_that_comes_back_is_no_longer_resolved(isolated_cache):
    a = [{"id": "a", "signal_id": "s", "severity": "error"}]
    scanners.update_ledger("t1", a, now="2026-01-01T00:00:00+00:00")
    scanners.update_ledger("t1", [], now="2026-01-02T00:00:00+00:00")
    ledger = scanners.update_ledger("t1", a, now="2026-01-03T00:00:00+00:00")
    assert "resolved_at" not in ledger["a"]


def test_age_is_measured_from_first_seen_not_last_seen(isolated_cache):
    scanners.update_ledger("t1", [{"id": "a", "signal_id": "s", "severity": "error"}],
                           now="2026-01-01T00:00:00+00:00")
    entry = scanners.read_ledger("t1")["a"]
    assert scanners.age_days(entry, now="2026-01-31T00:00:00+00:00") == 30


# =========================================================================== scheduling
def test_a_scanner_that_never_ran_is_always_due(isolated_cache):
    assert scanners.due(scanners.registry()[0], "t1") is True


def test_a_daily_scanner_is_not_due_again_the_same_hour(isolated_cache):
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    scanners.run(spec, "t1", [], _measured(spec), now="2026-01-01T00:00:00+00:00")
    assert scanners.due(spec, "t1", now="2026-01-01T06:00:00+00:00") is False
    assert scanners.due(spec, "t1", now="2026-01-02T06:00:00+00:00") is True


# =========================================================================== state isolation
def test_deleting_a_tenant_drops_its_scanner_baseline(isolated_cache):
    """A re-added tenant whose baseline survived would report every finding as resolved and
    then new, against a run whose data is gone."""
    demo.seed_demo("t1")
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    scanners.run(spec, "t1", [{"id": "a", "signal_id": sid, "severity": "error"}], _measured(spec))
    assert scanners.read_runs("t1")

    cache.delete_tenant("t1")
    assert scanners.read_runs("t1") == {}


def test_state_is_per_tenant(isolated_cache):
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    scanners.run(spec, "t1", [{"id": "a", "signal_id": sid, "severity": "error"}], _measured(spec))
    assert scanners.read_runs("t2") == {}


# =========================================================================== delivery
@pytest.mark.anyio
async def test_a_first_run_never_notifies(isolated_cache, monkeypatch):
    """Enabling a scanner on a real tenant makes every existing finding "new". Publishing 400
    notifications at that moment is how a user turns notifications off permanently."""
    from app.iam import scanner_jobs

    sent: list[dict] = []

    async def _fake_publish(**kw):
        sent.append(kw)
        return "n1"

    monkeypatch.setattr(scanner_jobs.notify, "publish", _fake_publish)
    sid = sig.all_signals()[0].id
    spec = _spec_over(sid)
    await scanner_jobs.run_scanner(
        "t1", spec,
        findings=[{"id": "a", "signal_id": sid, "severity": "error", "title": "t"}],
        results=_measured(spec),
    )
    assert sent == []


@pytest.mark.anyio
async def test_an_immediate_finding_bypasses_the_digest(isolated_cache, monkeypatch):
    from app.iam import scanner_jobs

    sent: list[dict] = []

    async def _fake_publish(**kw):
        sent.append(kw)
        return "n1"

    monkeypatch.setattr(scanner_jobs.notify, "publish", _fake_publish)
    sid = scanners.ALWAYS_IMMEDIATE[0]
    spec = _spec_over(sid)
    results = _measured(spec)
    base = {"id": "a", "signal_id": sid, "severity": "critical", "title": "t"}
    await scanner_jobs.run_scanner("t1", spec, findings=[base], results=results)  # baseline
    sent.clear()
    await scanner_jobs.run_scanner(
        "t1", spec,
        findings=[base, {"id": "b", "signal_id": sid, "severity": "critical", "title": "t2"}],
        results=results,
    )
    types = [s["type"] for s in sent]
    assert scanner_jobs.TYPE_IMMEDIATE in types
    assert scanner_jobs.TYPE_DIGEST not in types, "an immediate finding must not also be digested"


@pytest.mark.anyio
async def test_a_blocked_scanner_notifies_that_it_could_not_run(isolated_cache, monkeypatch):
    """Silence from a broken check is indistinguishable from silence from a clean tenant."""
    from app.iam import scanner_jobs

    sent: list[dict] = []

    async def _fake_publish(**kw):
        sent.append(kw)
        return "n1"

    monkeypatch.setattr(scanner_jobs.notify, "publish", _fake_publish)
    spec = _spec_over(sig.all_signals()[0].id)
    results = [sig.SignalResult(spec=s, findings=[], measured=False, reason="PIM not collected.")
               for s in spec.signals()]
    await scanner_jobs.run_scanner("t1", spec, findings=[], results=results)
    assert [s["type"] for s in sent] == [scanner_jobs.TYPE_BLOCKED]
    assert "PIM not collected." in sent[0]["body"]


@pytest.mark.anyio
async def test_a_permanently_blocked_scanner_repeats_one_fingerprint(isolated_cache, monkeypatch):
    """So the notification center can collapse it instead of raising a daily alarm forever."""
    from app.iam import scanner_jobs

    sent: list[dict] = []

    async def _fake_publish(**kw):
        sent.append(kw)
        return "n1"

    monkeypatch.setattr(scanner_jobs.notify, "publish", _fake_publish)
    spec = _spec_over(sig.all_signals()[0].id)
    results = [sig.SignalResult(spec=s, findings=[], measured=False, reason="PIM not collected.")
               for s in spec.signals()]
    await scanner_jobs.run_scanner("t1", spec, findings=[], results=results)
    await scanner_jobs.run_scanner("t1", spec, findings=[], results=results)
    assert sent[0]["fingerprint"] == sent[1]["fingerprint"]


@pytest.mark.anyio
async def test_one_failing_scanner_does_not_stop_the_sweep(isolated_cache, monkeypatch):
    from app.iam import scanner_jobs

    demo.seed_demo("t1")
    calls: list[str] = []
    real_run = scanners.run

    def _explode(spec, tenant_id, f, r, **kw):
        calls.append(spec.id)
        if spec.id == "iam.hygiene":
            raise RuntimeError("boom")
        return real_run(spec, tenant_id, f, r, **kw)

    monkeypatch.setattr(scanners, "run", _explode)
    out = await scanner_jobs.run_due("t1", force=True, notify_enabled=False)
    assert "iam.hygiene" in calls
    assert len(out) == len([s for s in scanners.registry() if s.enabled]) - 1


# =========================================================================== card payload size
def test_the_card_list_does_not_ship_finding_bodies():
    """The scanner list renders counts only. On a FIRST run every finding is "new", so shipping
    the bodies inline made the nine-card response 3.2 MB on a realistic tenant for data no
    component reads — the detail has its own endpoint."""
    card = {
        "scanner_id": "s1", "name": "S", "at": "now", "blocked": [],
        "counts": {"total": 2, "new": 2, "resolved": 0, "persisting": 0},
        "by_severity": {"critical": 2},
        "new": [{"id": "f1", "title": "x" * 400}, {"id": "f2", "title": "y" * 400}],
        "resolved_fingerprints": ["old1"],
        "immediate": [{"id": "f1"}],
        "unmeasured": [], "first_run": True, "last_run_at": "",
    }
    out = scanners.summarize(card)
    assert "new" not in out
    assert "immediate" not in out
    assert "resolved_fingerprints" not in out
    assert out["immediate_count"] == 1
    # The numbers a reader sees must be untouched by the trimming.
    assert out["counts"] == {"total": 2, "new": 2, "resolved": 0, "persisting": 0}
    assert out["by_severity"] == {"critical": 2}
    assert out["blocked"] == []


def test_summarise_keeps_a_blocked_card_blocked():
    """`counts: None` is how a scanner says "I could not look". If trimming ever turned that
    into a zero, a blind scanner would render as a clean one."""
    out = scanners.summarize({
        "scanner_id": "s1", "blocked": ["Managed identities were not collected."],
        "counts": None, "by_severity": {}, "new": [], "immediate": [],
        "resolved_fingerprints": [],
    })
    assert out["counts"] is None
    assert out["blocked"] == ["Managed identities were not collected."]
