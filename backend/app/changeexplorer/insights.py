"""Aggregate scored ChangeEvents into ChangeInsight summaries + facets for the UI tabs."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.changeexplorer.models import RISK_LABELS, make_insight

_LABEL_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Informational": 0}


def _top(counter: dict[str, int]) -> tuple[str, int]:
    if not counter:
        return "", 0
    k = max(counter, key=lambda x: counter[x])
    return k, counter[k]


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline counts + 'most X' rollups used by the Summary tab."""
    by_label = {l: 0 for l in RISK_LABELS}
    actors: dict[str, int] = defaultdict(int)
    types: dict[str, int] = defaultdict(int)
    cat_max: dict[str, int] = defaultdict(int)
    resources: set[str] = set()
    for e in events:
        by_label[e.get("riskLabel", "Informational")] = by_label.get(e.get("riskLabel", "Informational"), 0) + 1
        actors[e.get("actorDisplay") or e.get("actor", "unknown")] += 1
        types[e.get("resourceType", "")] += 1
        cat_max[e.get("category", "Unknown")] = max(cat_max[e.get("category", "Unknown")], int(e.get("riskScore", 0)))
        if e.get("resourceId"):
            resources.add(e["resourceId"])
    actor, actor_n = _top(actors)
    rtype, _ = _top(types)
    risky_cat = max(cat_max, key=lambda c: cat_max[c]) if cat_max else ""
    return {
        "total": len(events),
        "critical": by_label.get("Critical", 0),
        "high": by_label.get("High", 0),
        "medium": by_label.get("Medium", 0),
        "low": by_label.get("Low", 0),
        "informational": by_label.get("Informational", 0),
        "resources_changed": len(resources),
        "unique_actors": len(actors),
        "most_active_actor": actor,
        "most_active_actor_changes": actor_n,
        "most_changed_resource_type": rtype,
        "most_risky_category": risky_cat,
    }


def build_insights(run_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive ChangeInsight rows for the Risk Insights / Summary tabs."""
    out: list[dict[str, Any]] = []
    if not events:
        return out
    ranked = sorted(events, key=lambda e: -int(e.get("riskScore", 0)))

    # Highest-risk cluster.
    top = ranked[0]
    out.append(make_insight(
        run_id, "highest_risk", f"Highest-risk change: {top.get('resourceName','')}",
        f"{top.get('plainEnglishSummary','')} {top.get('whyRisk','')}".strip(),
        top.get("riskLabel", "Low"), [top.get("changeId", "")],
    ))

    # Critical/high rollup.
    high = [e for e in ranked if e.get("riskLabel") in ("Critical", "High")]
    if high:
        out.append(make_insight(
            run_id, "high_risk_rollup", f"{len(high)} high-risk change(s) need review first",
            "These changes touch ingress, certificates, DNS, networking, identity or RBAC and "
            "should be reviewed before lower-risk changes.",
            "High" if high[0].get("riskLabel") != "Critical" else "Critical",
            [e.get("changeId", "") for e in high[:25]],
        ))

    # Risk by category (top category by summed score).
    cat_sum: dict[str, int] = {}
    for e in events:
        cat_sum[e.get("category", "Unknown")] = cat_sum.get(e.get("category", "Unknown"), 0) + int(e.get("riskScore", 0))
    if cat_sum:
        c = max(cat_sum, key=lambda x: cat_sum[x])
        out.append(make_insight(
            run_id, "risk_by_category", f"{c} carries the most aggregate risk",
            f"{c} changes account for the largest share of total risk in this window.",
            "Medium", [e.get("changeId", "") for e in events if e.get("category") == c][:25],
        ))

    # Unknown-actor changes (governance flag) — only GENUINELY unknown callers, NOT Azure
    # platform / automation writes (those have no human caller by design and would be false alarms
    # on a forensic screen).
    unknown = [e for e in events if e.get("actorType") == "Unknown" and e.get("actorKind") != "AzurePlatform"]
    if unknown:
        out.append(make_insight(
            run_id, "unknown_actor", f"{len(unknown)} change(s) by an unknown actor",
            "The initiating identity could not be determined for these changes; review the source events.",
            "Medium", [e.get("changeId", "") for e in unknown[:25]],
        ))

    # Deletions.
    dels = [e for e in events if "delete" in (e.get("operation", "") or "").lower()]
    if dels:
        out.append(make_insight(
            run_id, "deletions", f"{len(dels)} deletion(s) detected",
            "Deletions generally carry higher risk than writes and should be confirmed as intended.",
            "High", [e.get("changeId", "") for e in dels[:25]],
        ))
    return out


def facets(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Distinct filter values for the UI (risk, category, actor, resource type)."""
    risks = sorted({e.get("riskLabel", "") for e in events if e.get("riskLabel")}, key=lambda l: -_LABEL_RANK.get(l, 0))
    cats = sorted({e.get("category", "") for e in events if e.get("category")})
    actors = sorted({e.get("actor", "") for e in events if e.get("actor")})
    rtypes = sorted({e.get("resourceType", "") for e in events if e.get("resourceType")})
    return {"risks": risks, "categories": cats, "actors": actors, "resource_types": rtypes}


def by_resource(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changed-resources rollup for the Resources tab."""
    groups: dict[str, dict[str, Any]] = {}
    for e in events:
        rid = e.get("resourceId", "")
        g = groups.setdefault(rid, {
            "resourceId": rid, "resourceName": e.get("resourceName", ""), "resourceType": e.get("resourceType", ""),
            "resourceGroup": e.get("resourceGroup", ""), "subscriptionId": e.get("subscriptionId", ""),
            "changes": 0, "highestRiskScore": 0, "highestRiskLabel": "Informational",
            "lastChanged": "", "lastActor": "", "role": e.get("dependencyRole", ""),
        })
        g["changes"] += 1
        if int(e.get("riskScore", 0)) > g["highestRiskScore"]:
            g["highestRiskScore"] = int(e.get("riskScore", 0))
            g["highestRiskLabel"] = e.get("riskLabel", "Informational")
        if e.get("eventTime", "") > g["lastChanged"]:
            g["lastChanged"] = e.get("eventTime", "")
            g["lastActor"] = e.get("actor", "")
    return sorted(groups.values(), key=lambda g: -g["highestRiskScore"])


def by_actor(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Actor rollup for the Actors tab.

    Grouping is keyed on a STABLE identity (object-id when known, else the caller string) so the
    same principal isn't split across rows, while the row DISPLAYS the resolved friendly name when
    available. Carries the refined kind, originating IP(s) and on-behalf-of user for forensics."""
    groups: dict[str, dict[str, Any]] = {}
    for e in events:
        key = e.get("actorObjectId", "") or e.get("actor", "") or "unknown"
        g = groups.setdefault(key, {
            "actor": e.get("actorDisplay", "") or e.get("actor", "unknown"),
            "actorId": e.get("actorObjectId", "") or (e.get("actor", "") if e.get("actor") != "unknown" else ""),
            "actorType": e.get("actorKind", "") or e.get("actorType", "Unknown"),
            "actorResolved": bool(e.get("actorResolved", False)),
            "changes": 0, "highestRiskScore": 0, "highestRiskLabel": "Informational",
            "categories": set(), "resources": set(), "ips": set(), "onBehalfOf": set(),
            "firstChange": "", "lastChange": "",
        })
        g["changes"] += 1
        if int(e.get("riskScore", 0)) > g["highestRiskScore"]:
            g["highestRiskScore"] = int(e.get("riskScore", 0))
            g["highestRiskLabel"] = e.get("riskLabel", "Informational")
        g["categories"].add(e.get("category", ""))
        g["resources"].add(e.get("resourceId", ""))
        if e.get("actorIp"):
            g["ips"].add(e["actorIp"])
        if e.get("actorOnBehalfOf"):
            g["onBehalfOf"].add(e["actorOnBehalfOf"])
        # Prefer a resolved display name if any event for this actor carried one.
        if e.get("actorResolved") and e.get("actorDisplay"):
            g["actor"] = e["actorDisplay"]
            g["actorResolved"] = True
        t = e.get("eventTime", "")
        if t and (not g["firstChange"] or t < g["firstChange"]):
            g["firstChange"] = t
        if t > g["lastChange"]:
            g["lastChange"] = t
    out = []
    for g in groups.values():
        out.append({
            **g,
            "categories": sorted(c for c in g["categories"] if c),
            "resources": len([r for r in g["resources"] if r]),
            "ips": sorted(g["ips"]),
            "onBehalfOf": sorted(g["onBehalfOf"]),
        })
    return sorted(out, key=lambda g: -g["highestRiskScore"])
