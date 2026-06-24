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

import asyncio
from typing import Any

VALUE_SOURCES = ("display_name", "email", "custom")

# Bounded concurrency for the live owner-tag apply path — fan the per-resource ARM writes out
# across a small worker pool instead of one-at-a-time, while keeping the count modest.
_APPLY_CONCURRENCY = 8


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
    custom_value: str = "",
) -> dict[str, Any]:
    """Per-resource owner-tag plan. ``resources`` are inventory rows ({id,name,tags,...,
    subscription_id, resource_group, resource_type, tags}). EVERY resource is returned in
    ``items`` with a ``status`` so the user always sees the full picture, even when nothing needs
    to change:
      * ``apply``       — will set/overwrite the tag,
      * ``ok``          — already carries the desired value (skipped),
      * ``conflict``    — has a different value and overwrite is off (skipped),
      * ``no_owner``    — no resolvable owner / no value (skipped).

    When ``value_source == "custom"`` the literal ``custom_value`` is staged on EVERY resource
    (no owner resolution needed) — used to stamp a fixed value (e.g. a team/cost-center) fleet-wide.
    """
    from app.ownership import resolve as own_resolve

    is_custom = value_source == "custom"
    custom = (custom_value or "").strip()
    ctx = None if is_custom else own_resolve.build_context(tenant_id)
    key = (tag_key or "owner").strip()
    items: list[dict[str, Any]] = []
    no_owner = 0
    for r in resources:
        rid = r.get("id", "")
        if not rid:
            continue
        tags = {str(k): str(v) for k, v in (r.get("tags") or {}).items()}
        base = {
            "id": rid, "name": r.get("name", ""), "resource_group": r.get("resource_group", ""),
            "subscription_id": r.get("subscription_id", ""),
        }
        # Resolve the value to stage.
        if is_custom:
            value = custom
        else:
            res = own_resolve.resolve_owner(
                tenant_id, "resource", rid,
                tags=r.get("tags"), subscription_id=r.get("subscription_id", ""),
                resource_group=r.get("resource_group", ""), ctx=ctx,
            )
            value = "" if res.get("unowned") else _owner_value(res.get("owners", []), value_source)
        cur = tags.get(key)

        if not value:
            # No resolvable owner / empty value — show it, but it can't be applied.
            no_owner += 1
            items.append({**base, "before": tags, "after": tags, "owner": "", "current": cur or "",
                          "conflict": False, "skipped": True, "status": "no_owner"})
            continue
        if cur == value:
            items.append({**base, "before": tags, "after": tags, "owner": value, "current": cur,
                          "conflict": False, "skipped": True, "status": "ok"})
            continue
        if cur is not None and not overwrite:
            # Existing different value and overwrite disabled → a skipped conflict.
            items.append({**base, "before": tags, "after": tags, "owner": value, "current": cur,
                          "conflict": True, "skipped": True, "status": "conflict"})
            continue
        after = dict(tags)
        after[key] = value
        items.append({**base, "before": tags, "after": after, "owner": value, "current": cur or "",
                      "conflict": cur is not None, "skipped": False, "status": "apply"})

    applicable = [it for it in items if not it.get("skipped")]
    return {
        "tag_key": key,
        "value_source": value_source,
        "items": items[:2000],
        "count": len(items),
        "applicable": len(applicable),
        "conflicts": sum(1 for it in items if it.get("conflict")),
        "no_owner": no_owner,
        "already_ok": sum(1 for it in items if it.get("status") == "ok"),
        "total_resources": len(items),
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
    from app.tagintel import revisions

    if connection is None:
        return {"ok": False, "error": "No Azure connection configured.",
                "applied": 0, "failed": 0, "total": 0, "revision": None, "results": []}

    key = plan.get("tag_key", "owner")
    targets = [it for it in plan.get("items", []) if not it.get("skipped")]
    rids = [it["id"] for it in targets]
    if not rids:
        return {"ok": True, "applied": 0, "failed": 0, "total": 0, "revision": None, "results": [],
                "note": "Nothing to apply."}

    # Recovery copy: the resources' CURRENT tags, read fresh from Azure. We ALWAYS snapshot before
    # writing so the revision can restore the exact prior state — a failed read aborts the apply
    # entirely rather than risk an unrevertible change.
    before, names, rerr = await read_current_tags(connection, rids)
    if rerr:
        return {"ok": False,
                "error": f"Couldn't capture a snapshot of current tags before applying ({rerr}). "
                         "No changes were made.",
                "applied": 0, "failed": 0, "total": 0, "revision": None, "results": []}

    # Apply across a bounded worker pool (multi-threaded) rather than strictly sequentially.
    sem = asyncio.Semaphore(_APPLY_CONCURRENCY)

    async def _apply_one(it: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
        async with sem:
            try:
                ok, err = await set_resource_tags(connection, it["id"], {key: it["owner"]}, operation="Merge")
            except Exception as exc:  # noqa: BLE001 — report, never wedge the pool.
                ok, err = False, str(exc)[:300]
            return it, ok, err

    outcomes = await asyncio.gather(*[_apply_one(it) for it in targets])

    results: list[dict[str, Any]] = []
    after_state: dict[str, dict[str, str]] = {}
    applied = 0
    failed = 0
    for it, ok, err in outcomes:
        rid = it["id"]
        # The after-state reflects the merge onto the freshly-read before (authoritative).
        base = dict(before.get(rid.lower(), {}))
        base[key] = it["owner"]
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
