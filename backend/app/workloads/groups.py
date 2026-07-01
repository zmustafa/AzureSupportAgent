"""Workload Group registry — associate related workloads into an application / service family.

A *group* (a.k.a. "application") is a lightweight, NON-destructive association over workloads
that keep their own identity — e.g. "CRM PROD" and "CRM DEV" both belong to the "CRM" group
while remaining separate workloads with their own environment, criticality and node
membership. This is distinct from:

  * **merge** (registry.merge_workloads) — fuses workloads into ONE, destroying the split;
  * **overlaps** — resources shared across workloads;
  * **grouping_memory** — Autopilot's learning from the user's discovery corrections.

Membership is stored as a ``group_id`` on each workload (see registry.py), so a group's
members are simply the workloads carrying its id — trashing a workload removes it from the
group automatically, and there are no dangling references to prune.

Persisted as backend/.data/workload_groups.json (Azure Files volume), keyed by UUID. No
secrets → no encryption, consistent with the other registries.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "workload_groups.json"

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "color": "",          # optional hex accent for the UI (e.g. "#6366f1")
    "owner": "",          # free-text owning team / DRI
    "tags": [],
    "tenant_id": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}

# Environment tokens stripped from workload names when auto-suggesting env-family groups
# ("CRM PROD" + "CRM DEV" → stem "CRM"). Lowercased, matched per whitespace/separator token.
_ENV_TOKENS: frozenset[str] = frozenset({
    "prod", "production", "prd",
    "dev", "development", "devel",
    "stg", "stage", "staging",
    "test", "tst", "qa", "uat",
    "dr", "sandbox", "sbx", "demo",
    "shared", "preprod", "ppe", "perf",
    "nonprod", "int", "integration",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    from app.core import jsonstore

    data = jsonstore.read_json(_PATH, {"groups": {}})
    return data if isinstance(data, dict) else {"groups": {}}


def _write(data: dict[str, Any]) -> None:
    from app.core import jsonstore

    jsonstore.write_json(_PATH, data)


def list_groups() -> list[dict[str, Any]]:
    data = _read()
    out: list[dict[str, Any]] = []
    for gid, g in data.get("groups", {}).items():
        merged = json.loads(json.dumps(DEFAULTS))  # deep copy (nested list)
        merged.update(g)
        merged["id"] = gid
        out.append(merged)
    out.sort(key=lambda g: g.get("name", "").lower())
    return out


def get_group(group_id: str) -> dict[str, Any] | None:
    if not group_id:
        return None
    for g in list_groups():
        if g["id"] == group_id:
            return g
    return None


def upsert_group(group: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    groups = data.setdefault("groups", {})
    gid = group.get("id") or str(uuid.uuid4())
    existing = groups.get(gid, {})
    merged = dict(existing)
    for key in DEFAULTS:
        if key in group and group[key] is not None:
            merged[key] = group[key]
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    groups[gid] = merged
    _write(data)
    result = get_group(gid)
    assert result is not None
    return result


def delete_group(group_id: str) -> bool:
    """Delete a group and detach it from every member workload (the workloads themselves are
    NOT deleted — only their ``group_id`` is cleared). Returns False when not found."""
    data = _read()
    if group_id not in data.get("groups", {}):
        return False
    del data["groups"][group_id]
    _write(data)
    # Detach members (registry manages its own persistence).
    from app.workloads import registry as wl_registry

    wl_registry.clear_group(group_id)
    return True


# --------------------------------------------------------------------------- rollups
def rollup_from_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate member ``WorkloadProfile`` dicts into a group-level rollup (pure, cache-only).

    Mirrors the FleetCockpit fleet aggregation but scoped to one group: average composite
    health (+ band + distribution), total resources, worst-case criticality, environment mix,
    category composition and summed risk. Empty members → an all-zero/None rollup."""
    crit_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    bands = {"good": 0, "warn": 0, "poor": 0, "unknown": 0}
    scores: list[int] = []
    total_resources = 0
    cat_totals: dict[str, int] = {}
    env_totals: dict[str, int] = {}
    retirements_90d = 0
    criticals = 0
    worst_crit = ""
    analyzed = 0

    for p in profiles:
        health = p.get("health") or {}
        band = health.get("band") or "unknown"
        bands[band] = bands.get(band, 0) + 1
        score = health.get("score")
        if isinstance(score, (int, float)):
            scores.append(int(score))
        if p.get("analyzed"):
            analyzed += 1

        comp = p.get("composition") or {}
        total_resources += int(comp.get("total") or 0)
        for c in comp.get("by_category") or []:
            cat_totals[c["category"]] = cat_totals.get(c["category"], 0) + int(c.get("count") or 0)

        cls = p.get("classification") or {}
        env = cls.get("environment") or "unknown"
        env_totals[env] = env_totals.get(env, 0) + 1
        crit = (cls.get("criticality") or "").lower()
        if crit_rank.get(crit, 0) > crit_rank.get(worst_crit, 0):
            worst_crit = crit

        risk = p.get("risk") or {}
        retirements_90d += int(risk.get("retirements_90d") or 0)
        criticals += int(risk.get("criticals") or 0)

    avg = round(sum(scores) / len(scores)) if scores else None
    band = "unknown" if avg is None else ("good" if avg >= 80 else "warn" if avg >= 50 else "poor")
    return {
        "member_count": len(profiles),
        "analyzed_count": analyzed,
        "total_resources": total_resources,
        "health": {"avg_score": avg, "band": band, "distribution": bands},
        "criticality": worst_crit,
        "risk": {"retirements_90d": retirements_90d, "criticals": criticals},
        "by_category": sorted(
            ({"category": k, "count": v} for k, v in cat_totals.items()), key=lambda x: -x["count"]
        ),
        "by_environment": sorted(
            ({"environment": k, "count": v} for k, v in env_totals.items()), key=lambda x: -x["count"]
        ),
    }


# --------------------------------------------------------------------------- compare (drift)
# The composite-health signals compared per member (label + profile ``health`` key). Order
# drives the drift matrix rows on the group compare view.
_HEALTH_SIGNALS: tuple[tuple[str, str], ...] = (
    ("monitoring", "Monitoring"),
    ("telemetry", "Telemetry"),
    ("backupdr", "Backup / DR"),
    ("performance", "Performance"),
    ("ownership", "Ownership"),
    ("policy", "Policy"),
    ("tags", "Tags"),
)


def _compare_types(profiles: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Union of resource types across members with a per-member count matrix. ``drift`` marks
    a type present in some members but not all — the sharpest "PROD has X, DEV lacks it" cue."""
    counts: dict[str, dict[str, int]] = {}
    friendly: dict[str, str] = {}
    for p in profiles:
        pid = p.get("id", "")
        for row in (p.get("composition") or {}).get("by_type") or []:
            t = row.get("type")
            if not t:
                continue
            counts.setdefault(t, {})[pid] = int(row.get("count") or 0)
            friendly.setdefault(t, row.get("friendly") or t)
    out: list[dict[str, Any]] = []
    for t, c in counts.items():
        present = sum(1 for v in c.values() if v > 0)
        out.append({
            "type": t,
            "friendly": friendly.get(t, t),
            "counts": c,
            "present_in": present,
            "total": sum(c.values()),
            "drift": 0 < present < n,
        })
    out.sort(key=lambda x: (not x["drift"], -x["total"], x["friendly"].lower()))
    return out


def compare_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Align member ``WorkloadProfile`` dicts side-by-side and surface DRIFT between them — the
    "PROD vs DEV" view. Pure + cache-only. *Drift* on any axis means a thing is present/covered
    in SOME members but not all: the classic "PROD has a WAF that DEV lacks" (resource-type
    drift) or "DEV is monitored, PROD isn't" (health-signal coverage drift). Fewer than 2
    members simply yields no drift (nothing to compare)."""
    members: list[dict[str, Any]] = []
    for p in profiles:
        cls = p.get("classification") or {}
        health = p.get("health") or {}
        comp = p.get("composition") or {}
        risk = p.get("risk") or {}
        members.append({
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "environment": cls.get("environment") or "",
            "criticality": cls.get("criticality") or "",
            "data_classification": cls.get("data_classification") or "",
            "workload_type": cls.get("workload_type") or "",
            "total_resources": int(comp.get("total") or 0),
            "health_score": health.get("score"),
            "health_band": health.get("band") or "unknown",
            "retirements_90d": int(risk.get("retirements_90d") or 0),
            "criticals": int(risk.get("criticals") or 0),
            "analyzed": bool(p.get("analyzed")),
        })

    ids = [m["id"] for m in members]
    n = len(members)
    label_of = {m["id"]: (m["environment"] or m["name"] or "?") for m in members}

    # Health-signal coverage matrix (drift = covered by some members, not all).
    signals: list[dict[str, Any]] = []
    for key, label in _HEALTH_SIGNALS:
        values = {p.get("id", ""): (p.get("health") or {}).get(key) for p in profiles}
        covered = sum(1 for v in values.values() if isinstance(v, (int, float)))
        signals.append({"key": key, "label": label, "values": values, "drift": 0 < covered < n})

    # Resource-category matrix.
    cat_counts: dict[str, dict[str, int]] = {}
    for p in profiles:
        pid = p.get("id", "")
        for row in (p.get("composition") or {}).get("by_category") or []:
            c = row.get("category")
            if not c:
                continue
            cat_counts.setdefault(c, {})[pid] = int(row.get("count") or 0)
    categories: list[dict[str, Any]] = []
    for c, counts in cat_counts.items():
        present = sum(1 for v in counts.values() if v > 0)
        categories.append({
            "category": c, "counts": counts, "present_in": present,
            "total": sum(counts.values()), "drift": 0 < present < n,
        })
    categories.sort(key=lambda x: (not x["drift"], -x["total"], x["category"]))

    # Resource-type matrix (sharpest drift signal).
    types = _compare_types(profiles, n)

    # Human-readable callouts from the top drifting types.
    highlights: list[str] = []
    for t in types:
        if not t["drift"]:
            break  # drift types sort first, so we can stop at the first non-drift
        have = [label_of[i] for i in ids if t["counts"].get(i, 0) > 0]
        lack = [label_of[i] for i in ids if t["counts"].get(i, 0) == 0]
        if have and lack:
            highlights.append(f"{', '.join(have)} has {t['friendly']} that {', '.join(lack)} lacks")
        if len(highlights) >= 6:
            break

    scores = [m["health_score"] for m in members if isinstance(m["health_score"], (int, float))]
    return {
        "members": members,
        "signals": signals,
        "categories": categories,
        "types": types,
        "highlights": highlights,
        "summary": {
            "member_count": n,
            "drift_types": sum(1 for t in types if t["drift"]),
            "drift_categories": sum(1 for c in categories if c["drift"]),
            "drift_signals": sum(1 for s in signals if s["drift"]),
            "health_spread": (max(scores) - min(scores)) if len(scores) >= 2 else 0,
        },
    }


# --------------------------------------------------------------------------- auto-suggest
def suggest_groups(workloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest groups by detecting environment-family name patterns.

    Strips environment tokens from each *currently-ungrouped* workload's name and clusters by
    the remaining stem: "CRM PROD" + "CRM DEV" → stem "crm" → a "CRM" suggestion. Only names
    that actually carried an environment token participate (a reliable signal), and only
    clusters of 2+ workloads are returned. Pure + instant (no Azure calls)."""
    clusters: dict[str, list[dict[str, Any]]] = {}
    for w in workloads:
        if w.get("group_id"):
            continue  # already grouped
        name = (w.get("name") or "").strip()
        if not name:
            continue
        tokens = [t for t in re.split(r"[\s\-_/|:.]+", name) if t]
        env = (w.get("environment") or "").lower()
        kept = [t for t in tokens if t.lower() not in _ENV_TOKENS and t.lower() != env]
        stripped_any = len(kept) < len(tokens)
        stem = " ".join(kept).strip().lower()
        # Only cluster env-families: the name must have carried an environment token AND leave
        # a non-empty stem behind (so a workload literally named "prod" isn't clustered alone).
        if not stem or not stripped_any:
            continue
        clusters.setdefault(stem, []).append(w)

    out: list[dict[str, Any]] = []
    for stem, members in clusters.items():
        if len(members) < 2:
            continue
        display = " ".join(part.capitalize() for part in stem.split())
        out.append({
            "name": display,
            "stem": stem,
            "workload_ids": [m["id"] for m in members],
            "members": [
                {"id": m["id"], "name": m.get("name", ""), "environment": m.get("environment", "")}
                for m in members
            ],
        })
    out.sort(key=lambda s: (-len(s["workload_ids"]), s["name"]))
    return out
