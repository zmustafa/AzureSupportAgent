"""Ownership coverage + a light ownership-policy engine.

Over a chosen scope (a workload or a subscription) this enumerates the in-scope Azure
resources (read-only Resource Graph, the same batched path the coverage detectors use),
resolves the effective owner of each via :mod:`app.ownership.resolve`, and rolls the result
up into:

* a coverage **scorecard** (% of resources with an effective owner),
* a **source breakdown** (direct / tag / workload / inherited / unowned),
* a **by-owner** rollup (who owns how much), and
* **policy findings** — an ownership-as-code check (e.g. "production resources must have an
  owner", "owned resources should have a primary owner", "tag-only owners aren't in the
  directory" — the orphan signal).

``compute_coverage`` is a pure function over already-fetched rows (unit-testable, also used
by the demo); ``collect_coverage`` resolves the scope and fetches the rows from Azure."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.ownership import resolve

log = logging.getLogger("app.ownership.coverage")

# Tag values that mark a production resource (drives the "prod must be owned" policy).
_PROD_VALUES = {"prod", "production", "prd", "live"}
_ENV_TAG_KEYS = ("environment", "Environment", "env", "Env", "tier", "Tier")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_production(tags: Any) -> bool:
    if not isinstance(tags, dict):
        return False
    for k in _ENV_TAG_KEYS:
        v = str(tags.get(k, "")).strip().lower()
        if v in _PROD_VALUES:
            return True
    return False


def compute_coverage(
    resources: list[dict[str, Any]],
    *,
    tenant_id: str,
    scope_kind: str,
    scope_id: str,
    scope_name: str = "",
    ctx: dict[str, Any] | None = None,
    demo: bool = False,
) -> dict[str, Any]:
    """Pure: resolve every resource's owner and roll up coverage + policy findings."""
    context = ctx or resolve.build_context(tenant_id)
    total = len(resources)
    owned = 0
    by_source: dict[str, int] = {"direct": 0, "tag": 0, "workload": 0, "inherited": 0, "none": 0}
    by_owner: dict[str, dict[str, Any]] = {}
    unowned: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []           # owner is a tag string not in the directory
    prod_unowned: list[dict[str, Any]] = []

    for r in resources:
        rid = r.get("id", "")
        tags = r.get("tags")
        res = resolve.resolve_owner(tenant_id, "resource", rid, tags=tags, ctx=context)
        src = res["source"]
        by_source[src] = by_source.get(src, 0) + 1
        row = {
            "id": rid,
            "name": r.get("name", "") or (rid.rsplit("/", 1)[-1] if rid else ""),
            "type": r.get("type", ""),
            "resource_group": r.get("resourceGroup", "") or r.get("resource_group", ""),
            "subscription_id": r.get("subscriptionId", "") or r.get("subscription_id", ""),
            "owner": "", "owner_source": src,
        }
        if res["unowned"]:
            unowned.append(row)
            if _is_production(tags):
                prod_unowned.append(row)
            continue
        owned += 1
        primary = next((o for o in res["owners"] if o["primary"]), res["owners"][0])
        label = primary.get("display_name") or primary.get("email") or "(unnamed)"
        row["owner"] = label
        key = primary.get("owner_id") or f"tag::{label}"
        bucket = by_owner.setdefault(key, {
            "owner_id": primary.get("owner_id", ""), "label": label,
            "email": primary.get("email", ""), "count": 0, "via": {},
        })
        bucket["count"] += 1
        bucket["via"][src] = bucket["via"].get(src, 0) + 1
        # An owner that exists ONLY as a resource tag (no registry record) is an "orphan
        # owner" — accountability is asserted but not tracked in the directory.
        if src == "tag" and not primary.get("owner_id"):
            orphans.append({**row, "tag_owner": label})

    coverage_pct = round(owned / total * 100) if total else None
    owners_sorted = sorted(by_owner.values(), key=lambda b: -b["count"])

    findings = _policy_findings(
        total=total, owned=owned, unowned=unowned, prod_unowned=prod_unowned,
        orphans=orphans, scope_kind=scope_kind, scope_name=scope_name or scope_id,
    )

    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "generated_at": _now_iso(),
        "demo": demo,
        "coverage_pct": coverage_pct,
        "kpis": {
            "total": total,
            "owned": owned,
            "unowned": len(unowned),
            "owners": len(by_owner),
            "orphan_owners": len(orphans),
            "prod_unowned": len(prod_unowned),
        },
        "by_source": by_source,
        "by_owner": owners_sorted,
        "unowned": unowned[:500],
        "orphans": orphans[:200],
        "findings": findings,
    }


def _policy_findings(
    *, total: int, owned: int, unowned: list[dict[str, Any]], prod_unowned: list[dict[str, Any]],
    orphans: list[dict[str, Any]], scope_kind: str, scope_name: str,
) -> list[dict[str, Any]]:
    """Evaluate the built-in ownership policy over the computed coverage."""
    out: list[dict[str, Any]] = []
    if total == 0:
        return out
    if prod_unowned:
        out.append({
            "id": "prod_unowned",
            "severity": "error",
            "title": f"{len(prod_unowned)} production resource(s) have no owner",
            "detail": "Production resources must have an accountable owner. Assign one (directly, "
                      "via the workload, or via an ancestor scope).",
            "count": len(prod_unowned),
            "resources": [r["id"] for r in prod_unowned[:25]],
        })
    if unowned:
        sev = "warning" if owned else "error"
        out.append({
            "id": "unowned_resources",
            "severity": sev,
            "title": f"{len(unowned)} of {total} resource(s) are unowned",
            "detail": "Unowned resources have no accountable contact for incidents, retirements, "
                      "cost or security follow-up.",
            "count": len(unowned),
            "resources": [r["id"] for r in unowned[:25]],
        })
    if orphans:
        out.append({
            "id": "orphan_tag_owners",
            "severity": "info",
            "title": f"{len(orphans)} resource(s) name an owner only in tags",
            "detail": "These resources carry an owner tag but the owner isn't tracked in the "
                      "directory. Promote the tag owner to a managed owner so notifications, "
                      "leaver-detection and attestation can reach them.",
            "count": len(orphans),
            "resources": [r["id"] for r in orphans[:25]],
        })
    return out


def empty_snapshot(scope_kind: str, scope_id: str, *, error: str = "", never_loaded: bool = False) -> dict[str, Any]:
    return {
        "scope_kind": scope_kind, "scope_id": scope_id, "scope_name": "",
        "generated_at": _now_iso(), "demo": False, "coverage_pct": None,
        "kpis": {"total": 0, "owned": 0, "unowned": 0, "owners": 0, "orphan_owners": 0, "prod_unowned": 0},
        "by_source": {"direct": 0, "tag": 0, "workload": 0, "inherited": 0, "none": 0},
        "by_owner": [], "unowned": [], "orphans": [], "findings": [],
        "error": error, "never_loaded": never_loaded,
    }


async def collect_coverage(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    tenant_id: str,
) -> dict[str, Any]:
    """Resolve the scope, fetch its resources from Resource Graph, and compute coverage."""
    from app.assessments.runner import _resolve_scope, query_resources_batched, scope_predicate_batches

    scope_name = ""
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        scope_name = workload.get("name", "")
        if scope.get("error") and not scope.get("predicate"):
            return empty_snapshot(scope_kind, scope_id, error=scope["error"])
        predicates = scope_predicate_batches(scope)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{scope_id}'"]
        scope_name = scope_id
    else:
        return empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    try:
        resources = await query_resources_batched(
            predicates, connection,
            projection="id, name, type, resourceGroup, subscriptionId, location, tags",
        )
    except RuntimeError as exc:
        return empty_snapshot(scope_kind, scope_id, error=str(exc)[:300])

    return compute_coverage(
        resources, tenant_id=tenant_id, scope_kind=scope_kind, scope_id=scope_id,
        scope_name=scope_name,
    )
