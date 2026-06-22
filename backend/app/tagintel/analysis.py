"""Pure tag-analysis functions (no I/O).

Every function here takes an already-collected list of *normalized resource dicts* (the shape
produced by ``app.inventory.service`` — ``{id, name, type, location, resource_group,
subscription_id, tags, workloads, ...}``) and returns plain JSON-able structures the API and
UI render. Keeping this layer pure makes it deterministic and unit-testable without Azure.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

# --------------------------------------------------------------------------- normalization

# Collapse a tag key to a comparison form so ``CostCenter`` / ``costcenter`` / ``Cost Center``
# / ``cost_center`` all map together (case + separators removed).
_SEP = re.compile(r"[\s_\-./]+")


def norm_key(key: str) -> str:
    return _SEP.sub("", (key or "").strip().lower())


def norm_value(val: str) -> str:
    return _SEP.sub("", (str(val) if val is not None else "").strip().lower())


# Canonical value synonym groups for the common low-cardinality governance tags. The first
# entry of each list is the canonical spelling we recommend normalizing to.
_VALUE_SYNONYMS: dict[str, list[list[str]]] = {
    "environment": [
        ["Production", "prod", "prd", "production", "live", "prdn"],
        ["Staging", "stage", "stg", "staging", "preprod", "pre-prod"],
        ["Development", "dev", "develop", "development", "devel"],
        ["Test", "test", "tst", "testing", "qa", "uat"],
        ["DR", "dr", "disasterrecovery", "disaster-recovery"],
        ["Sandbox", "sandbox", "sbx", "sand"],
    ],
}

# Substring heuristics mapping a tag key to the business *purpose* it most likely represents.
# Order matters (first match wins); used so the UI can group keys by what they're for.
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("billing", ("billingcode", "costcenter", "cost", "chargeback", "budget", "account", "invoice")),
    ("ownership", ("owner", "createdby", "managedby", "contact", "supportteam", "team", "steward", "custodian")),
    ("environment", ("environment", "env", "stage", "tier")),
    ("application", ("application", "app", "workload", "service", "servicename", "product", "project", "component", "system")),
    ("organization", ("businessunit", "bu", "department", "dept", "division", "organization", "org", "company")),
    ("security", ("dataclassification", "classification", "confidentiality", "compliance", "pii", "sensitivity", "security")),
    ("lifecycle", ("expiration", "expiry", "ttl", "decommission", "retire", "lifecycle", "createdon", "provisioned")),
    ("operations", ("backup", "dr", "patch", "patchgroup", "maintenance", "schedule", "monitoring", "criticality", "sla")),
]


def classify_key(key: str) -> str:
    """Best-guess business purpose of a tag key (billing / ownership / environment /
    application / organization / security / lifecycle / operations / other)."""
    nk = norm_key(key)
    for category, needles in _CATEGORY_RULES:
        if any(n in nk for n in needles):
            return category
    return "other"


def canonical_value(key: str, value: str) -> str | None:
    """If ``value`` is a known variant for a recognized key family (currently the
    environment family), return the canonical spelling; otherwise None."""
    family = "environment" if classify_key(key) == "environment" else None
    if not family:
        return None
    nv = norm_value(value)
    for group in _VALUE_SYNONYMS.get(family, []):
        canon = group[0]
        if nv in {norm_value(g) for g in group}:
            return canon
    return None


# --------------------------------------------------------------------------- census (F1)


def _iter_tags(resources: Iterable[dict[str, Any]]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    for r in resources:
        tags = r.get("tags") or {}
        if isinstance(tags, dict):
            for k, v in tags.items():
                yield k, ("" if v is None else str(v)), r


def census(resources: list[dict[str, Any]], sub_names: dict[str, str] | None = None,
           *, high_card_threshold: int = 50, top_values: int = 8) -> dict[str, Any]:
    """Estate-wide tag census: per-key counts, value distributions, coverage, cardinality, and
    purpose classification, plus untagged/partial rollups and scope-level coverage."""
    sub_names = sub_names or {}
    total = len(resources)
    tagged = sum(1 for r in resources if (r.get("tags") or {}))
    # Per-key aggregation.
    key_count: dict[str, int] = defaultdict(int)
    key_subs: dict[str, set[str]] = defaultdict(set)
    key_values: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    key_spellings: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # norm -> {original: count}
    for k, v, r in _iter_tags(resources):
        key_count[k] += 1
        sub = r.get("subscription_id") or ""
        if sub:
            key_subs[k].add(sub)
        key_values[k][v] += 1
        key_spellings[norm_key(k)][k] += 1

    keys: list[dict[str, Any]] = []
    for k in sorted(key_count, key=lambda x: (-key_count[x], x.lower())):
        values = key_values[k]
        distinct = len(values)
        cnt = key_count[k]
        top = [{"value": vv, "count": cc} for vv, cc in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))[:top_values]]
        # Casing/spelling variants of THIS key (other originals that normalize the same).
        variants = sorted([s for s in key_spellings[norm_key(k)] if s != k])
        keys.append({
            "key": k,
            "count": cnt,
            "coverage_pct": round(cnt / total * 100, 1) if total else 0,
            "subscription_count": len(key_subs[k]),
            "distinct_values": distinct,
            "category": classify_key(k),
            "high_cardinality": distinct >= high_card_threshold and distinct > cnt * 0.6,
            "single_subscription": len(key_subs[k]) == 1,
            "top_values": top,
            "casing_variants": variants,
        })

    # Scope-level coverage (resource-level rollup per container, since the resources query
    # carries only resource tags — container tags aren't part of this payload).
    sub_total: dict[str, int] = defaultdict(int)
    sub_tagged: dict[str, int] = defaultdict(int)
    rg_total: dict[str, int] = defaultdict(int)
    rg_tagged: dict[str, int] = defaultdict(int)
    untagged_sample: list[dict[str, Any]] = []
    for r in resources:
        sub = r.get("subscription_id") or ""
        rg = r.get("resource_group") or ""
        has = bool(r.get("tags") or {})
        sub_total[sub] += 1
        rg_key = f"{sub}/{rg}"
        rg_total[rg_key] += 1
        if has:
            sub_tagged[sub] += 1
            rg_tagged[rg_key] += 1
        elif len(untagged_sample) < 200:
            untagged_sample.append({"id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", ""),
                                    "resource_group": rg, "subscription_id": sub})

    by_sub = [{
        "id": s, "name": sub_names.get(s, s), "total": sub_total[s], "tagged": sub_tagged[s],
        "coverage_pct": round(sub_tagged[s] / sub_total[s] * 100, 1) if sub_total[s] else 0,
    } for s in sorted(sub_total, key=lambda x: (-sub_total[x], x))]
    by_rg = [{
        "key": rg, "total": rg_total[rg], "tagged": rg_tagged[rg],
        "coverage_pct": round(rg_tagged[rg] / rg_total[rg] * 100, 1) if rg_total[rg] else 0,
    } for rg in sorted(rg_total, key=lambda x: (rg_tagged[x] / rg_total[x] if rg_total[x] else 0, -rg_total[x]))[:100]]

    high_card = sum(1 for k in keys if k["high_cardinality"])
    single_sub = sum(1 for k in keys if k["single_subscription"])
    category_breakdown: dict[str, int] = defaultdict(int)
    for k in keys:
        category_breakdown[k["category"]] += 1

    return {
        "total_resources": total,
        "tagged_count": tagged,
        "untagged_count": total - tagged,
        "tag_coverage_pct": round(tagged / total * 100, 1) if total else 0,
        "distinct_keys": len(key_count),
        "distinct_pairs": sum(len(v) for v in key_values.values()),
        "keys": keys,
        "scope_coverage": {"by_subscription": by_sub, "by_resource_group": by_rg},
        "untagged_sample": untagged_sample,
        "category_breakdown": [{"category": c, "count": n} for c, n in sorted(category_breakdown.items(), key=lambda kv: -kv[1])],
        "flags": {
            "high_cardinality": high_card,
            "single_subscription": single_sub,
        },
    }


# --------------------------------------------------------------------------- hygiene (F2)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def key_clusters(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Near-duplicate tag-key clusters. Two kinds: exact normalization collisions (case /
    separator only — high confidence) and edit-distance neighbours (medium confidence)."""
    key_count: dict[str, int] = defaultdict(int)
    for k, _v, _r in _iter_tags(resources):
        key_count[k] += 1
    # Group by normalized form for the high-confidence (casing/separator) clusters.
    by_norm: dict[str, list[str]] = defaultdict(list)
    for k in key_count:
        by_norm[norm_key(k)].append(k)

    clusters: list[dict[str, Any]] = []
    grouped_norms: set[str] = set()
    for norm, members in by_norm.items():
        if len(members) > 1:
            grouped_norms.add(norm)
            canon = max(members, key=lambda m: (key_count[m], -len(m)))
            clusters.append({
                "canonical": canon,
                "members": sorted(members, key=lambda m: -key_count[m]),
                "counts": {m: key_count[m] for m in members},
                "affected": sum(key_count[m] for m in members),
                "confidence": "high",
                "reason": "Same key with different casing or separators.",
                "category": classify_key(canon),
            })

    # Edit-distance neighbours across DISTINCT normalized forms (e.g. ``Owner`` vs ``Owners``).
    norms = sorted(set(by_norm) - grouped_norms)
    used: set[str] = set()
    for i, a in enumerate(norms):
        if a in used or len(a) < 4:
            continue
        group = [a]
        for b in norms[i + 1:]:
            if b in used or len(b) < 4:
                continue
            if abs(len(a) - len(b)) <= 2 and _levenshtein(a, b) <= 1:
                group.append(b)
        if len(group) > 1:
            members = [max(by_norm[n], key=lambda m: key_count[m]) for n in group]
            for n in group:
                used.add(n)
            canon = max(members, key=lambda m: (key_count[m], -len(m)))
            clusters.append({
                "canonical": canon,
                "members": sorted(members, key=lambda m: -key_count[m]),
                "counts": {m: key_count[m] for m in members},
                "affected": sum(key_count[m] for m in members),
                "confidence": "medium",
                "reason": "Keys are one edit apart — likely the same tag.",
                "category": classify_key(canon),
            })
    clusters.sort(key=lambda c: -c["affected"])
    return clusters


def value_clusters(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-key value-variant clusters (e.g. Prod/PRD/Production). High confidence when the
    variants match a known synonym family, medium when they only differ by case/separators."""
    key_values: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for k, v, _r in _iter_tags(resources):
        if v:
            key_values[k][v] += 1

    out: list[dict[str, Any]] = []
    for key, values in key_values.items():
        # Group raw values by normalized form.
        by_norm: dict[str, list[str]] = defaultdict(list)
        for v in values:
            by_norm[norm_value(v)].append(v)
        # Casing/separator collisions.
        variants: list[dict[str, Any]] = []
        for _norm, members in by_norm.items():
            if len(members) > 1:
                canon = canonical_value(key, members[0]) or max(members, key=lambda m: values[m])
                variants.append({
                    "canonical": canon,
                    "members": sorted(members, key=lambda m: -values[m]),
                    "affected": sum(values[m] for m in members),
                    "confidence": "high",
                })
        # Known synonym families (Prod/PRD/Production) even when not a pure casing collision.
        if classify_key(key) == "environment":
            fam_groups: dict[str, list[str]] = defaultdict(list)
            for v in values:
                c = canonical_value(key, v)
                if c and norm_value(c) != norm_value(v):
                    fam_groups[c].append(v)
            for canon, members in fam_groups.items():
                allm = sorted(set(members), key=lambda m: -values[m])
                if allm:
                    variants.append({
                        "canonical": canon,
                        "members": allm,
                        "affected": sum(values[m] for m in allm),
                        "confidence": "high",
                    })
        if variants:
            out.append({
                "key": key,
                "category": classify_key(key),
                "distinct_values": len(values),
                "variants": sorted(variants, key=lambda x: -x["affected"]),
            })
    out.sort(key=lambda x: -sum(v["affected"] for v in x["variants"]))
    return out


# --------------------------------------------------------------------------- grouping (F3)


def workload_inference(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Infer candidate workload groupings and score confidence by the strength of the signal:
    explicit application/workload tag (high) > resource-group cohesion (medium) > name prefix
    (low). Resources already attributed to a defined workload are reported as confirmed."""
    app_keys = {"application", "app", "workload", "service", "servicename", "product", "project", "system"}
    groups: dict[str, dict[str, Any]] = {}

    def _bucket(gid: str, label: str, signal: str, confidence: str) -> dict[str, Any]:
        return groups.setdefault(gid, {
            "id": gid, "label": label, "signal": signal, "confidence": confidence,
            "resource_ids": set(), "types": defaultdict(int), "subscriptions": set(),
        })

    confirmed = 0
    for r in resources:
        rid = r.get("id", "")
        tags = {k.lower(): v for k, v in (r.get("tags") or {}).items()}
        wls = r.get("workloads") or []
        if wls:
            confirmed += 1
            g = _bucket(f"wl:{wls[0]['id']}", wls[0]["name"], "Defined workload membership", "confirmed")
        else:
            app_val = next((str(tags[k]) for k in app_keys if k in tags and tags[k]), "")
            if app_val:
                g = _bucket(f"tag:{norm_value(app_val)}", app_val, "Application/workload tag", "high")
            elif r.get("resource_group"):
                rg = r["resource_group"]
                g = _bucket(f"rg:{r.get('subscription_id','')}/{rg}", rg, "Resource-group cohesion", "medium")
            else:
                name = r.get("name", "")
                prefix = re.split(r"[-_]", name)[0] if name else ""
                if len(prefix) >= 3:
                    g = _bucket(f"name:{prefix.lower()}", f"{prefix}*", "Name-prefix heuristic", "low")
                else:
                    continue
        g["resource_ids"].add(rid)
        g["types"][r.get("type", "")] += 1
        if r.get("subscription_id"):
            g["subscriptions"].add(r["subscription_id"])

    out = []
    for g in groups.values():
        ids = g["resource_ids"]
        if len(ids) < 2 and g["confidence"] in ("low", "medium"):
            continue  # ignore singletons from weak signals (noise)
        types = sorted(g["types"].items(), key=lambda kv: -kv[1])
        out.append({
            "id": g["id"], "label": g["label"], "signal": g["signal"], "confidence": g["confidence"],
            "resource_count": len(ids), "subscription_count": len(g["subscriptions"]),
            "top_types": [{"type": t, "count": c} for t, c in types[:5]],
            "needs_review": g["confidence"] in ("low",),
        })
    out.sort(key=lambda x: (-{"confirmed": 3, "high": 2, "medium": 1, "low": 0}[x["confidence"]], -x["resource_count"]))
    return {
        "confirmed_resources": confirmed,
        "inferred_groups": out,
        "summary": {
            "confirmed": sum(1 for g in out if g["confidence"] == "confirmed"),
            "high": sum(1 for g in out if g["confidence"] == "high"),
            "medium": sum(1 for g in out if g["confidence"] == "medium"),
            "low": sum(1 for g in out if g["confidence"] == "low"),
        },
    }
