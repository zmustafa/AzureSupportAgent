"""AI Insight Packs — pack-health rollup + snooze tests.

Covers the pure logic behind ``GET /insights/health`` (``runs.aggregate_health``): per-pack
run/verdict counts, notify + false-positive rates, the recent-verdict sparkline ordering, and
the "raise the threshold" hint for noisy packs. Also covers ``packfile`` snooze normalization
that backs ``POST /insights/packs/{id}/snooze``.
"""
from app.insights import packfile
from app.insights import runs as runs_store


def _run(pack_id, verdict, *, notified=False, fp=False, ack=False, created_at=None,
         pack_name="Pack", pack_icon="\U0001f6e1\ufe0f"):
    return {
        "pack_id": pack_id,
        "pack_name": pack_name,
        "pack_icon": pack_icon,
        "verdict": verdict,
        "notified": notified,
        "false_positive": fp,
        "acknowledged_at": "2026-01-01T00:00:00Z" if ack else "",
        "created_at": created_at,
    }


# ------------------------------------------------------------------- aggregate_health
def test_aggregate_health_empty_is_empty():
    assert runs_store.aggregate_health([]) == {}


def test_aggregate_health_skips_runs_without_pack_id():
    out = runs_store.aggregate_health([_run("", "urgent"), {"verdict": "notable"}])
    assert out == {}


def test_aggregate_health_counts_and_verdict_mix():
    runs = [
        _run("p1", "urgent", notified=True),
        _run("p1", "notable", notified=True),
        _run("p1", "nothing_notable"),
    ]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["runs_total"] == 3
    assert h["notified"] == 2
    assert h["verdicts"] == {"nothing_notable": 1, "notable": 1, "urgent": 1}
    assert h["material"] == 2  # notable + urgent


def test_aggregate_health_last_run_from_newest():
    runs = [
        _run("p1", "urgent", created_at="2026-02-02T00:00:00Z"),
        _run("p1", "notable", created_at="2026-01-01T00:00:00Z"),
    ]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["last_verdict"] == "urgent"
    assert h["last_run_at"] == "2026-02-02T00:00:00Z"


def test_aggregate_health_noise_score_and_fp_rate_rounded():
    runs = [
        _run("p1", "urgent", notified=True, fp=True),
        _run("p1", "notable", notified=True),
        _run("p1", "nothing_notable"),
    ]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["noise_score"] == round(2 / 3, 3)  # 2 notified / 3 total
    assert h["fp_rate"] == round(1 / 2, 3)      # 1 fp / 2 notified


def test_aggregate_health_fp_rate_zero_when_never_notified():
    h = runs_store.aggregate_health([_run("p1", "nothing_notable")])["p1"]
    assert h["notified"] == 0
    assert h["noise_score"] == 0.0
    assert h["fp_rate"] == 0.0


def test_aggregate_health_acknowledged_counts_only_with_timestamp():
    runs = [_run("p1", "notable", ack=True), _run("p1", "notable", ack=False)]
    assert runs_store.aggregate_health(runs)["p1"]["acknowledged"] == 1


def test_aggregate_health_spark_is_oldest_to_newest():
    # Input is newest-first; sparkline should be drawn oldest -> newest.
    runs = [_run("p1", "urgent"), _run("p1", "notable"), _run("p1", "nothing_notable")]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["spark"] == [0, 1, 2]  # nothing_notable, notable, urgent


def test_aggregate_health_spark_capped_at_20():
    runs = [_run("p1", "notable", notified=True) for _ in range(25)]
    h = runs_store.aggregate_health(runs)["p1"]
    assert len(h["spark"]) == 20


def test_aggregate_health_suggest_raise_threshold_when_noisy():
    # 6 notified, 3 dismissed as false positives -> fp rate 0.5 at >=5 notifies.
    runs = [_run("p1", "notable", notified=True, fp=(i < 3)) for i in range(6)]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["suggest_raise_threshold"] is True
    assert h["fp_rate"] == 0.5


def test_aggregate_health_no_suggest_below_notify_floor():
    # All 4 notifies are false positives, but below the 5-notify floor.
    runs = [_run("p1", "notable", notified=True, fp=True) for _ in range(4)]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["suggest_raise_threshold"] is False


def test_aggregate_health_no_suggest_when_fp_rate_low():
    # 6 notifies but only 2 false positives -> 0.33 < 0.5.
    runs = [_run("p1", "notable", notified=True, fp=(i < 2)) for i in range(6)]
    h = runs_store.aggregate_health(runs)["p1"]
    assert h["suggest_raise_threshold"] is False


def test_aggregate_health_multiple_packs_keyed_in_order():
    runs = [_run("p2", "urgent"), _run("p1", "notable"), _run("p2", "notable")]
    out = runs_store.aggregate_health(runs)
    assert list(out.keys()) == ["p2", "p1"]  # first-seen order preserved
    assert out["p2"]["runs_total"] == 2
    assert out["p1"]["runs_total"] == 1


def test_aggregate_health_defaults_name_and_icon():
    out = runs_store.aggregate_health([{"pack_id": "p1", "verdict": "notable"}])
    assert out["p1"]["pack_name"] == ""
    assert out["p1"]["pack_icon"] == "\U0001f9e0"


# ------------------------------------------------------------------- snooze normalization
def test_normalize_adds_empty_snoozed_until_by_default():
    p = packfile.normalize({"name": "Test", "prompt": "x", "sources": ["changes"]})
    assert p["snoozed_until"] == ""


def test_normalize_coerces_none_snoozed_until_to_empty():
    p = packfile.normalize({"name": "T", "prompt": "x", "sources": ["changes"], "snoozed_until": None})
    assert p["snoozed_until"] == ""


def test_normalize_preserves_snoozed_until_value():
    iso = "2026-07-04T00:00:00+00:00"
    p = packfile.normalize({"name": "T", "prompt": "x", "sources": ["changes"], "snoozed_until": iso})
    assert p["snoozed_until"] == iso
