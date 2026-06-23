"""Build and apply an owner → Azure-tag plan from the ownership data, with a revertible
recovery revision.

Given a scope and a chosen tag KEY (e.g. ``owner``) and a VALUE SOURCE (the owner's display
name or email), this resolves the effective owner of every in-scope resource (reusing the
ownership resolver) and produces a per-resource before→after tag plan. Applying it:

  1. reads the CURRENT tags of the affected resources (the recovery copy),
  2. PATCH-Merges the owner tag onto each resource,
  3. records a :mod:`app.tagintel.revisions` revision so the change can be visualized + reverted.

Reuses the shared tag plumbing (:mod:`app.azure.tag_ops`) and resolver
(:mod:`app.ownership.resolve`). Honors the ownership write-back governance gate.
"""
from __future__ import annotations

from typing import Any

VALUE_SOURCES = ("display_name", "email")


def _owner_value(owners: list[dict[str, Any]], value_source: str) -> str:
    """Pick the tag value from the primary owner per the chosen source (fallback to name)."""
    if not owners:
        return ""
    o = owners[0]
    if value_source == "email":
        return o.get("email", "") or o.get("display_name", "")
    return o.get("display_name", "") or o.get("email", "")


def build_tag_plan(
    tenant_id: str,
    resources: list[dict[str, Any]],
    *,
    tag_key: str,
    value_source: str = "display_name",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Per-resource owner-tag plan. ``resources`` are inventory rows ({id,name,tags,...,
    subscription_id, resource_group, resource_type, tags}). For each resource we resolve its
    effective owner and, when found, stage the ``tag_key`` tag. Resources already carrying the
    same value are skipped; existing different values are only changed when ``overwrite``.
    """
    from app.ownership import resolve as own_resolve

    ctx = own_resolve.build_context(tenant_id)
    key = (tag_key or "owner").strip()
    items: list[dict[str, Any]] = []
    no_owner = 0
    for r in resources:
        rid = r.get("id", "")
        if not rid:
            continue
        tags = {str(k): str(v) for k, v in (r.get("tags") or {}).items()}
        res = own_resolve.resolve_owner(
            tenant_id, "resource", rid,
            tags=r.get("tags"), subscription_id=r.get("subscription_id", ""),
            resource_group=r.get("resource_group", ""), ctx=ctx,
        )
        if res.get("unowned"):
            no_owner += 1
            continue
        value = _owner_value(res.get("owners", []), value_source)
        if not value:
            no_owner += 1
            continue
        cur = tags.get(key)
        if cur == value:
            continue  # already correct
        if cur is not None and not overwrite:
            # Existing different value and overwrite disabled → record as a skipped conflict.
            items.append({
                "id": rid, "name": r.get("name", ""), "resource_group": r.get("resource_group", ""),
                "subscription_id": r.get("subscription_id", ""), "before": tags, "after": tags,
                "owner": value, "conflict": True, "current": cur, "skipped": True,
            })
            continue
        after = dict(tags)
        after[key] = value
        items.append({
            "id": rid, "name": r.get("name", ""), "resource_group": r.get("resource_group", ""),
            "subscription_id": r.get("subscription_id", ""), "before": tags, "after": after,
            "owner": value, "conflict": cur is not None, "current": cur or "", "skipped": False,
        })
    applicable = [it for it in items if not it.get("skipped")]
    return {
        "tag_key": key,
        "value_source": value_source,
        "items": items[:2000],
        "count": len(items),
        "applicable": len(applicable),
        "conflicts": sum(1 for it in items if it.get("conflict")),
        "no_owner": no_owner,
    }


async def apply_tag_plan(
    tenant_id: str,
    connection: dict[str, Any] | None,
    plan: dict[str, Any],
    *,
    actor: str = "",
    scope: str = "",
) -> dict[str, Any]:
    """Apply the applicable items of an owner-tag plan, capturing a revertible revision first.
    Returns ``{ok, applied, failed, total, revision, results}``."""
    from app.azure.tag_ops import read_current_tags, set_resource_tags
    from app.ownership.writeback import writeback_enabled
    from app.tagintel import revisions

    if not writeback_enabled():
        return {"ok": False, "error": "Owner-tag write-back is disabled. Enable it in Settings first.",
                "applied": 0, "failed": 0, "total": 0, "revision": None, "results": []}
    if connection is None:
        return {"ok": False, "error": "No Azure connection configured.",
                "applied": 0, "failed": 0, "total": 0, "revision": None, "results": []}

    key = plan.get("tag_key", "owner")
    targets = [it for it in plan.get("items", []) if not it.get("skipped")]
    rids = [it["id"] for it in targets]
    if not rids:
        return {"ok": True, "applied": 0, "failed": 0, "total": 0, "revision": None, "results": [],
                "note": "Nothing to apply."}

    # Recovery copy: the resources' CURRENT tags, read fresh from Azure.
    before, names, rerr = await read_current_tags(connection, rids)
    if rerr and not before:
        return {"ok": False, "error": rerr, "applied": 0, "failed": 0, "total": 0, "revision": None, "results": []}

    results: list[dict[str, Any]] = []
    after_state: dict[str, dict[str, str]] = {}
    applied = 0
    failed = 0
    for it in targets:
        rid = it["id"]
        value = it["owner"]
        ok, err = await set_resource_tags(connection, rid, {key: value}, operation="Merge")
        # The after-state reflects the merge onto the freshly-read before (authoritative).
        base = dict(before.get(rid.lower(), {}))
        base[key] = value
        after_state[rid.lower()] = base
        if ok:
            applied += 1
        else:
            failed += 1
        results.append({"id": rid, "ok": ok, "error": err})
        names.setdefault(rid.lower(), it.get("name", ""))

    revision = revisions.save_revision(
        tenant_id, connection.get("id", ""),
        source="ownership",
        description=f"Applied '{key}' owner tag to {applied} resource(s)",
        before={rid.lower(): before.get(rid.lower(), {}) for rid in rids},
        after=after_state,
        names=names,
        actor=actor,
        scope=scope,
        applied=applied,
        failed=failed,
    )
    return {"ok": failed == 0, "applied": applied, "failed": failed, "total": len(rids),
            "revision": revision, "results": results}
