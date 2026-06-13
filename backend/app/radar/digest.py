"""Digest selection for the scheduled Radar push.

Given a freshly-computed snapshot and the last-known tracking-ID set, selects the items
that warrant a notification: brand-new events/models, and items that have crossed a
configured deadline lead-time threshold (e.g. 90/60/30 days). This keeps the scheduled
Teams/Slack/email digest to *new + deadline-approaching* items only, as the briefing
recommends — never the full list every run."""
from __future__ import annotations

from typing import Any


def _crossed_threshold(days: int | None, lead_days: list[int]) -> int | None:
    """Return the threshold the item is now within (smallest applicable), else None."""
    if days is None or days < 0:
        return None
    applicable = [t for t in sorted(lead_days) if days <= t]
    return applicable[0] if applicable else None


def select_digest_items(
    snapshot: dict[str, Any],
    *,
    known_ids: set[str],
    lead_days: list[int],
) -> dict[str, Any]:
    """Return {events, models, new_count, approaching_count, summary}.

    ``known_ids`` are tracking IDs already seen on the previous run; anything not in it is
    'new'. Deadline-approaching = within the smallest configured lead-time threshold."""
    lead_days = sorted({int(d) for d in (lead_days or [90, 60, 30]) if int(d) > 0}) or [90, 60, 30]
    new_events: list[dict[str, Any]] = []
    approaching: list[dict[str, Any]] = []

    for e in snapshot.get("events", []) or []:
        # Done/waived items never page.
        if e.get("status") in ("done", "waived"):
            continue
        tid = e.get("tracking_id") or e.get("id", "")
        is_new = tid not in known_ids
        thr = _crossed_threshold(e.get("days_until"), lead_days)
        if is_new:
            new_events.append({**_slim(e), "reason": "new"})
        elif thr is not None:
            approaching.append({**_slim(e), "reason": f"within {thr} days"})

    new_models: list[dict[str, Any]] = []
    for m in snapshot.get("model_items", []) or []:
        tid = m.get("id", "")
        if tid not in known_ids and m.get("severity") in ("red", "amber"):
            new_models.append(m)

    selected = new_events + approaching
    summary = (
        f"{len(new_events)} new and {len(approaching)} deadline-approaching lifecycle item(s)"
        + (f", {len(new_models)} AI-model deployment(s) at risk" if new_models else "")
        + "."
    )
    return {
        "events": selected,
        "models": new_models,
        "new_count": len(new_events),
        "approaching_count": len(approaching),
        "summary": summary,
    }


def _slim(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": e.get("id", ""),
        "tracking_id": e.get("tracking_id", ""),
        "title": e.get("title", ""),
        "service": e.get("service", ""),
        "change_type": e.get("change_type", ""),
        "retirement_date": e.get("retirement_date", ""),
        "days_until": e.get("days_until"),
        "impacted_count": e.get("impacted_count", 0),
        "owner": e.get("owner", ""),
        "unowned": e.get("unowned", False),
        "severity": e.get("severity", "grey"),
    }


def current_tracking_ids(snapshot: dict[str, Any]) -> list[str]:
    ids = [e.get("tracking_id") or e.get("id", "") for e in snapshot.get("events", []) or []]
    ids += [m.get("id", "") for m in snapshot.get("model_items", []) or []]
    return [i for i in ids if i]
