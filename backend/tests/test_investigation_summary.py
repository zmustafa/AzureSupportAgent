"""Tests for deep-investigation digest + confidence scoring."""
from app.agent.investigation_summary import (
    confidence_score,
    hypothesis_counts,
    investigation_digest,
)


def _inv(statuses, root_cause="", summary="", agents=0):
    return {
        "hypotheses": [{"id": str(i), "status": s} for i, s in enumerate(statuses)],
        "conclusion": {"root_cause": root_cause, "summary": summary},
        "agents": [{"id": str(i)} for i in range(agents)],
    }


def test_empty_investigation_zero_confidence():
    assert confidence_score(None) == 0
    assert confidence_score({}) == 0


def test_no_hypotheses_but_root_cause_is_moderate():
    assert confidence_score({"hypotheses": [], "conclusion": {"root_cause": "disk full"}}) == 55
    assert confidence_score({"hypotheses": [], "conclusion": {}}) == 0


def test_validated_hypothesis_is_high_confidence():
    score = confidence_score(_inv(["validated", "invalidated"], root_cause="throttling"))
    assert score >= 75


def test_all_invalidated_is_low_but_floored_by_conclusion():
    # No root cause + all invalidated → very low.
    assert confidence_score(_inv(["invalidated", "invalidated"])) == 0
    # A stated root cause floors confidence at 50%.
    assert confidence_score(_inv(["invalidated"], root_cause="x")) == 50


def test_hypothesis_counts():
    counts = hypothesis_counts(_inv(["validated", "validated", "invalidated", "inconclusive"]))
    assert counts["validated"] == 2
    assert counts["invalidated"] == 1
    assert counts["inconclusive"] == 1


def test_digest_shape():
    d = investigation_digest(_inv(["validated"], root_cause="rc", summary="s", agents=3))
    assert d["root_cause"] == "rc"
    assert d["summary"] == "s"
    assert d["hypothesis_total"] == 1
    assert d["agent_count"] == 3
    assert d["has_conclusion"] is True
    assert 0 <= d["confidence"] <= 100
