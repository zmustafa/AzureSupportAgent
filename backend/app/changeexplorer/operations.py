"""Operation grouping + chronological narrative for the Change Explorer (features A1/A2).

- ``group_operations`` (A1): collapse many changes that share a ``correlationId`` (or, lacking
  one, the same actor within a short time burst) into a single *operation* — e.g. "1 deployment by
  Zeeshan → 12 resources". Turns a flat 1,500-row list into a handful of meaningful actions.
- ``build_narrative`` (A2): an ordered, plain-English story of the window built from those
  operations, so a reviewer reads a sequence of events rather than a table.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
_BURST_SECONDS = 120  # group correlation-less changes by the same actor within this window


def _parse(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _actor_label(e: dict[str, Any]) -> str:
    return e.get("actorDisplay") or e.get("actor", "") or "unknown"


def _op_verb(events: list[dict[str, Any]]) -> str:
    """A human verb for the operation from the dominant category/operation of its changes."""
    cats = [e.get("category", "") for e in events]
    ops = " ".join(str(e.get("operation", "")).lower() for e in events)
    if any(c == "Deployment" for c in cats) or "deployments" in ops:
        return "Deployment"
    if all("delete" in str(e.get("operation", "")).lower() for e in events):
        return "Deletion"
    if any(c in ("RBAC", "PIM") for c in cats):
        return "Access change"
    # Most common category.
    if cats:
        top = max(set(cats), key=cats.count)
        return f"{top} change" if top and top != "Unknown" else "Change"
    return "Change"


def group_operations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group events into operations. Returns a list of operation dicts sorted by start time desc.

    Operation shape: {operationId, correlationId, actor, actorKind, verb, startTime, endTime,
    changeCount, resourceCount, categories[], highestRiskScore, highestRiskLabel, securityFlagCount,
    resourceNames[], changeIds[]}."""
    # Bucket by correlation id (real ones) first.
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    loose: list[dict[str, Any]] = []
    for e in events:
        cid = (e.get("correlationId", "") or "").strip()
        if cid and cid != _ZERO_GUID:
            buckets[cid].append(e)
        else:
            loose.append(e)

    # Loose (no correlation id): group by actor + time burst.
    loose.sort(key=lambda e: (_actor_label(e), e.get("eventTime", "")))
    cur_key: tuple[str, Any] | None = None
    cur_anchor: datetime | None = None
    burst_idx = 0
    for e in loose:
        actor = _actor_label(e)
        ts = _parse(e.get("eventTime", ""))
        if (cur_key is None or cur_key[0] != actor or cur_anchor is None or ts is None
                or (ts - cur_anchor).total_seconds() > _BURST_SECONDS):
            burst_idx += 1
            cur_key = (actor, burst_idx)
            cur_anchor = ts
        buckets[f"burst:{actor}:{burst_idx}"].append(e)

    ops: list[dict[str, Any]] = []
    for cid, evs in buckets.items():
        evs_sorted = sorted(evs, key=lambda e: e.get("eventTime", ""))
        times = [e.get("eventTime", "") for e in evs_sorted if e.get("eventTime")]
        cats = sorted({e.get("category", "") for e in evs_sorted if e.get("category")})
        res = {e.get("resourceId", "") for e in evs_sorted if e.get("resourceId")}
        res_names = list(dict.fromkeys(e.get("resourceName", "") for e in evs_sorted if e.get("resourceName")))
        top = max(evs_sorted, key=lambda e: int(e.get("riskScore", 0)))
        sec = sum(len(e.get("securityFlags") or []) for e in evs_sorted)
        actor = _actor_label(evs_sorted[0])
        ops.append({
            "operationId": cid,
            "correlationId": "" if cid.startswith("burst:") else cid,
            "actor": actor,
            "actorKind": evs_sorted[0].get("actorKind") or evs_sorted[0].get("actorType", "Unknown"),
            "verb": _op_verb(evs_sorted),
            "startTime": times[0] if times else "",
            "endTime": times[-1] if times else "",
            "changeCount": len(evs_sorted),
            "resourceCount": len(res),
            "categories": cats,
            "highestRiskScore": int(top.get("riskScore", 0)),
            "highestRiskLabel": top.get("riskLabel", "Informational"),
            "securityFlagCount": sec,
            "resourceNames": res_names[:12],
            "changeIds": [e.get("changeId", "") for e in evs_sorted],
        })
    ops.sort(key=lambda o: o["startTime"], reverse=True)
    return ops


def operation_phrase(op: dict[str, Any]) -> str:
    """One-line plain-English phrase for an operation (used by the narrative + Operations tab)."""
    n = op["changeCount"]
    rc = op["resourceCount"]
    verb = op["verb"].lower()
    res = ""
    if rc == 1 and op["resourceNames"]:
        res = f" to {op['resourceNames'][0]}"
    elif rc > 1:
        res = f" across {rc} resources"
    sec = f" · {op['securityFlagCount']} security flag(s)" if op["securityFlagCount"] else ""
    return f"{op['actor']} performed a {verb} ({n} change{'s' if n != 1 else ''}{res}){sec}"


def build_narrative(events: list[dict[str, Any]], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A chronological list of narrative beats (oldest → newest) for the Narrative tab.

    Each beat: {time, actor, riskLabel, text, changeIds, securityFlagCount}. Built from operations
    so the story reads as a sequence of actions, not individual property writes."""
    beats: list[dict[str, Any]] = []
    for op in sorted(operations, key=lambda o: o["startTime"]):
        if not op["startTime"]:
            continue
        beats.append({
            "time": op["startTime"],
            "actor": op["actor"],
            "riskLabel": op["highestRiskLabel"],
            "riskScore": op["highestRiskScore"],
            "securityFlagCount": op["securityFlagCount"],
            "text": operation_phrase(op),
            "changeIds": op["changeIds"],
            "categories": op["categories"],
        })
    return beats
