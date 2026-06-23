"""Heuristic owner suggestions — infer probable owners for UNOWNED subjects, with evidence.

Pure / cache-only (no Azure scan): suggestions are derived from signals already collected
elsewhere, so the Suggestions tab is instant and the inference is explainable:

* **RBAC owners** — principals holding ``Owner`` / ``User Access Administrator`` /
  ``Contributor`` on a subscription the subject lives in (from the cached RBAC scan via
  :func:`app.rbac.compose.build_master_rows`). The strongest signal: a person with Owner on
  the subscription is the likeliest accountable owner.
* **Orphan tag owners** — an ``owner`` tag on a resource that isn't yet a managed owner
  (surfaced by the coverage scan). Suggest promoting the tag value to a real owner.

Each suggestion carries a confidence (0-1) and a list of human-readable ``evidence`` lines.
Accepting one (API) materialises the candidate as an :mod:`app.ownership.registry` owner
(``source`` = rbac/tag) and creates the assignment."""
from __future__ import annotations

import hashlib
from typing import Any

from app.ownership import resolve

# RBAC roles that imply accountability, most-accountable first (drives confidence).
_OWNER_ROLES = {
    "owner": 0.85,
    "user access administrator": 0.7,
    "contributor": 0.6,
}


def _sig_id(*parts: str) -> str:
    return "sug-" + hashlib.sha1("|".join(p for p in parts if p).encode()).hexdigest()[:16]  # noqa: S324


def _workload_subs(wl: dict[str, Any]) -> set[str]:
    """Subscription guids a workload touches, derived from its nodes (no Azure)."""
    subs: set[str] = set()
    for node in wl.get("nodes", []) or []:
        guid = resolve.sub_guid(node.get("subscription_id", "")) or resolve.sub_guid(node.get("id", ""))
        if guid:
            subs.add(guid)
    return subs


def _rbac_owners_by_sub(tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    """{sub_guid: [{principal_id, name, upn, role, weight, privileged}]} from the cached RBAC
    scan, restricted to accountability-implying roles. Empty when nothing's been scanned."""
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        from app.rbac import compose
        rows = compose.build_master_rows(tenant_id)
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        sub = str(r.get("subscriptionId") or "").lower()
        role = str(r.get("roleName") or "").strip().lower()
        if not sub or role not in _OWNER_ROLES:
            continue
        ptype = str(r.get("effectivePrincipalType") or "").lower()
        # People + groups make sense as owners; skip pure service principals (noise) unless
        # nothing else is found (handled by ranking, not here).
        name = r.get("effectivePrincipalName") or r.get("principalDisplayName") or ""
        pid = r.get("effectivePrincipalId") or r.get("principalId") or ""
        if not (name or pid):
            continue
        out.setdefault(sub, []).append({
            "principal_id": pid,
            "name": name or pid,
            "upn": r.get("effectivePrincipalUserPrincipalName") or "",
            "ptype": ptype,
            "role": r.get("roleName") or role.title(),
            "weight": _OWNER_ROLES[role],
            "privileged": bool(r.get("roleIsPrivileged")),
        })
    return out


def _rank_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by principal, keep the strongest role, prefer users/groups over SPs."""
    best: dict[str, dict[str, Any]] = {}
    for c in cands:
        key = (c["principal_id"] or c["name"]).lower()
        cur = best.get(key)
        if cur is None or c["weight"] > cur["weight"]:
            best[key] = c
    ranked = sorted(
        best.values(),
        key=lambda c: (c["ptype"] == "serviceprincipal", -c["weight"], c["name"].lower()),
    )
    return ranked


def suggest_for_tenant(tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Suggestions for every UNOWNED workload + architecture, plus orphan-tag promotions."""
    ctx = resolve.build_context(tenant_id)
    owners_by_sub = _rbac_owners_by_sub(tenant_id)
    out: list[dict[str, Any]] = []

    # 1. Unowned workloads → RBAC owners on their subscriptions.
    try:
        from app.workloads.registry import list_workloads
        for wl in list_workloads():
            res = resolve.resolve_owner(tenant_id, "workload", wl["id"], ctx=ctx)
            if not res["unowned"]:
                continue
            subs = _workload_subs(wl)
            pool: list[dict[str, Any]] = []
            for s in subs:
                pool.extend(owners_by_sub.get(s, []))
            for cand in _rank_candidates(pool)[:3]:
                out.append(_suggestion(
                    subject_kind="workload", subject_id=wl["id"], subject_name=wl.get("name", ""),
                    cand=cand, scope_label=f"{len(subs)} subscription(s)",
                ))
    except Exception:  # noqa: BLE001
        pass

    # 2. Unowned architectures → RBAC owners on their subscriptions (from arch nodes).
    try:
        from app.architectures.registry import list_architectures
        for arch in list_architectures(tenant_id):
            res = resolve.resolve_owner(tenant_id, "architecture", arch["id"], ctx=ctx)
            if not res["unowned"]:
                continue
            subs: set[str] = set()
            for n in arch.get("nodes", []) or []:
                meta = n.get("meta") or n.get("data") or {}
                guid = resolve.sub_guid(str(meta.get("subscription_id", "")) or str(meta.get("arm_id", "")) or str(n.get("id", "")))
                if guid:
                    subs.add(guid)
            pool = []
            for s in subs:
                pool.extend(owners_by_sub.get(s, []))
            for cand in _rank_candidates(pool)[:2]:
                out.append(_suggestion(
                    subject_kind="architecture", subject_id=arch["id"], subject_name=arch.get("name", ""),
                    cand=cand, scope_label=f"{len(subs)} subscription(s)",
                ))
    except Exception:  # noqa: BLE001
        pass

    return out[:limit]


def _owner_tag_value(tags: Any) -> str:
    """The owner string from a resource's tags ("" if none) — reuses the resolver heuristic."""
    return resolve.owner_from_tags(tags)


def _cached_inventory_resources(tenant_id: str) -> list[dict[str, Any]]:
    """Every resource across ALL cached inventory snapshots for the tenant (no Azure scan).

    The inventory cache is keyed ``tenant|connection[|scope]``; we union the resources from
    every snapshot whose key starts with this tenant, de-duped by resource id. This lets
    owner-tag suggestions work off whatever inventory the user has already scanned."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from app.inventory import cache as invcache
        data = invcache._load()  # noqa: SLF001 - internal read of the snapshot store
    except Exception:  # noqa: BLE001
        return []
    prefix = f"{tenant_id or 'default'}|"
    for key, entry in (data or {}).items():
        if not str(key).startswith(prefix):
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        for r in (payload or {}).get("resources", []) or []:
            rid = str(r.get("id", "")).lower()
            if rid and rid not in out:
                out[rid] = r
    return list(out.values())


def inventory_tag_suggestions(tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Suggest owners for UNOWNED workloads from the ``owner`` tag on their member resources.

    This is the most direct ownership signal and needs no RBAC scan — it reads the cached
    inventory, groups owner-tagged resources by workload (via each resource's ``workloads``
    link, falling back to sub/RG/resource membership), and proposes the dominant owner."""
    ctx = resolve.build_context(tenant_id)
    resources = _cached_inventory_resources(tenant_id)
    if not resources:
        return []

    # workload_id -> Counter(owner_tag_value)
    from collections import Counter
    by_workload: dict[str, Counter] = {}
    wl_names: dict[str, str] = {}
    wl_index = ctx["wl_index"]
    for r in resources:
        owner = _owner_tag_value(r.get("tags"))
        if not owner:
            continue
        # Skip junk owner tags that aren't a person/team (URLs, automation markers).
        low = owner.lower()
        if low.startswith(("http://", "https://", "www.")) or len(owner) > 80:
            continue
        # Prefer the resource's own workload link; fall back to membership resolution.
        linked = r.get("workloads") or []
        wls: list[tuple[str, str]] = []
        if isinstance(linked, list) and linked:
            for w in linked:
                if isinstance(w, dict) and w.get("id"):
                    wls.append((w["id"], w.get("name", "")))
        if not wls:
            meta = resolve.workload_for_resource(r.get("id", ""), wl_index)
            if meta.get("workload_id"):
                wls.append((meta["workload_id"], meta.get("workload_name", "")))
        for wid, wname in wls:
            by_workload.setdefault(wid, Counter())[owner] += 1
            if wname:
                wl_names[wid] = wname

    out: list[dict[str, Any]] = []
    for wid, counter in by_workload.items():
        res = resolve.resolve_owner(tenant_id, "workload", wid, ctx=ctx)
        if not res["unowned"]:
            continue
        owner, count = counter.most_common(1)[0]
        total = sum(counter.values())
        # More agreement among tagged resources => higher confidence (cap 0.75).
        conf = round(min(0.75, 0.45 + 0.1 * min(count, 3)), 2)
        out.append({
            "id": _sig_id("tagwl", wid, owner),
            "subject_kind": "workload",
            "subject_id": wid,
            "subject_name": wl_names.get(wid, ""),
            "candidate": {
                "kind": "team" if any(t in owner.lower() for t in ("team", "group", "ops", "squad")) else "person",
                "display_name": owner,
                "email": owner if "@" in owner else "",
                "source": "tag",
                "link": {},
            },
            "role": "technical",
            "confidence": conf,
            "evidence": [
                f"{count} of {total} tagged resource(s) in this workload carry `owner={owner}`.",
            ],
            "signal": "inventory_tag",
        })
    return out[:limit]


def _suggestion(*, subject_kind: str, subject_id: str, subject_name: str, cand: dict[str, Any], scope_label: str) -> dict[str, Any]:
    conf = round(min(0.95, cand["weight"] + (0.05 if cand["privileged"] else 0)), 2)
    evidence = [
        f"Holds **{cand['role']}** on {scope_label} this {subject_kind} spans (cached RBAC scan).",
    ]
    if cand["ptype"]:
        evidence.append(f"Directory principal type: {cand['ptype']}.")
    return {
        "id": _sig_id(subject_kind, subject_id, cand["principal_id"] or cand["name"]),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "subject_name": subject_name,
        "candidate": {
            "kind": "team" if cand["ptype"] == "group" else "person",
            "display_name": cand["name"],
            "email": cand["upn"],
            "source": "rbac",
            "link": {k: v for k, v in {
                "entra_object_id": cand["principal_id"],
                "upn": cand["upn"],
            }.items() if v},
        },
        "role": "technical",
        "confidence": conf,
        "evidence": evidence,
        "signal": "rbac_owner",
    }


def orphan_tag_suggestions(tenant_id: str) -> list[dict[str, Any]]:
    """Promote orphan owner-tags (from any cached ownership coverage snapshot) to managed
    owners. Reads the coverage cache only — no Azure scan."""
    from app.ownership import cache

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        data = cache._read()  # noqa: SLF001 - internal read of the snapshot store
    except Exception:  # noqa: BLE001
        return out
    bucket = data.get(tenant_id or "default", {})
    for snap in bucket.values():
        if not isinstance(snap, dict):
            continue
        for orphan in snap.get("orphans", []) or []:
            label = str(orphan.get("tag_owner") or "").strip()
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            out.append({
                "id": _sig_id("orphan", label),
                "subject_kind": "resource",
                "subject_id": orphan.get("id", ""),
                "subject_name": orphan.get("name", ""),
                "candidate": {
                    "kind": "person", "display_name": label,
                    "email": label if "@" in label else "", "source": "tag", "link": {},
                },
                "role": "technical",
                "confidence": 0.5,
                "evidence": [f"Named in an `owner` tag on resource **{orphan.get('name','')}** but not yet a managed owner."],
                "signal": "orphan_tag",
            })
    return out
