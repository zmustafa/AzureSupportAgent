"""Intent-vs-Reality drift for the ``/graph`` flagship differentiator.

Compares a workload's DOCUMENTED architecture (the nodes/edges authored or AI-inferred on
the Architecture canvas) against its LIVE inventory (the resources currently attributed to
the workload). Pure: takes plain dicts, returns a classification + a drift score.

Visual language (applied on the frontend):
- node present in both           → ``ok`` (solid)
- documented but NOT live         → ``documented_missing`` (amber, dashed border)
- live but NOT documented         → ``live_uncontrolled`` (red)
- architecture edge w/o live pair → ``inferred`` (dashed edge)
"""
from __future__ import annotations

from typing import Any


def _norm(arm_id: str) -> str:
    return (arm_id or "").strip().lower()


def compute_drift(
    *, architecture: dict[str, Any] | None, member_resources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Classify each resource as ok / documented_missing / live_uncontrolled.

    Returns ``{has_architecture, ok, documented_missing, live_uncontrolled, counts,
    drift_score, summary}``. ``drift_score`` is 0-100 where 100 = perfectly aligned."""
    documented: dict[str, dict[str, Any]] = {}
    if architecture:
        for n in architecture.get("nodes", []) or []:
            arm = _norm(n.get("arm_id", ""))
            if arm:
                documented[arm] = {"arm_id": n.get("arm_id", ""), "name": n.get("name", ""), "type": n.get("type", "")}

    live: dict[str, dict[str, Any]] = {}
    for r in member_resources:
        arm = _norm(r.get("id", ""))
        if arm:
            live[arm] = {"arm_id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", "")}

    if not architecture:
        # No documented design → everything live is "uncontrolled" by definition, but we don't
        # punish a workload that simply has no architecture yet; flag it instead.
        return {
            "has_architecture": False,
            "ok": [],
            "documented_missing": [],
            "live_uncontrolled": list(live.values()),
            "counts": {"ok": 0, "documented_missing": 0, "live_uncontrolled": len(live)},
            "drift_score": None,
            "summary": "No documented architecture to compare against — generate one to enable drift detection.",
        }

    doc_keys = set(documented)
    live_keys = set(live)
    ok_keys = doc_keys & live_keys
    missing_keys = doc_keys - live_keys
    extra_keys = live_keys - doc_keys

    ok = [documented[k] for k in ok_keys]
    documented_missing = [documented[k] for k in missing_keys]
    live_uncontrolled = [live[k] for k in extra_keys]

    total = len(doc_keys | live_keys)
    drift_score = round(len(ok_keys) / total * 100) if total else 100
    summary = _summary(len(ok_keys), len(missing_keys), len(extra_keys), drift_score)
    return {
        "has_architecture": True,
        "ok": ok,
        "documented_missing": documented_missing,
        "live_uncontrolled": live_uncontrolled,
        "counts": {"ok": len(ok), "documented_missing": len(documented_missing), "live_uncontrolled": len(live_uncontrolled)},
        "drift_score": drift_score,
        "summary": summary,
    }


def _summary(ok: int, missing: int, extra: int, score: int) -> str:
    if missing == 0 and extra == 0:
        return f"Live estate matches the documented architecture ({ok} resources aligned)."
    parts = [f"{score}% aligned"]
    if extra:
        parts.append(f"{extra} live resource(s) outside the documented design")
    if missing:
        parts.append(f"{missing} documented resource(s) not found live")
    return " — ".join(parts) + "."


def drift_classification(drift: dict[str, Any]) -> dict[str, str]:
    """``{arm_id_lower: classification}`` so the caller can tag graph resource nodes."""
    out: dict[str, str] = {}
    for r in drift.get("ok", []):
        out[_norm(r.get("arm_id", ""))] = "ok"
    for r in drift.get("documented_missing", []):
        out[_norm(r.get("arm_id", ""))] = "documented_missing"
    for r in drift.get("live_uncontrolled", []):
        out[_norm(r.get("arm_id", ""))] = "live_uncontrolled"
    out.pop("", None)
    return out
