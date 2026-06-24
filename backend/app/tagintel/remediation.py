"""Safe tag remediation (F9): build a change-set, preview the exact before->after diff, and
generate PowerShell / Azure CLI / Resource Graph / rollback scripts.

A *change-set* is one or more tag operations (add / set / rename-key / normalize-value) applied
together. Named change-sets can be saved (``.data/tagintel_changesets.json``) and re-loaded to
preview / dry-run / generate-apply scripts repeatedly — so a team can curate a standard set of
key:value fixes once and replay it across scopes.

This module never writes to Azure. It produces a *plan* and the scripts a human runs (or that
a gated automation target executes) only after explicit approval — honoring the product rule
that tags are never modified without the user asking. Applied plans + a changelog are persisted
to ``.data/tagintel_plans.json`` for the audit trail and rollback.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "tagintel_plans.json"
_CS_PATH = Path(__file__).resolve().parents[2] / ".data" / "tagintel_changesets.json"

# Supported change operations.
OPS = ("add_tag", "set_tag", "rename_key", "normalize_value", "remove_key")

# Bounded concurrency for the live apply path. Writing many resources one-at-a-time is slow, so we
# fan the per-resource tag writes out across a small worker pool while still streaming a live
# per-resource status feed. Kept modest so a large plan never spawns an unbounded number of
# az-CLI subprocesses / ARM calls at once.
_APPLY_CONCURRENCY = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _apply_to_tags(tags: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW tag dict with the operation applied (pure)."""
    out = dict(tags)
    typ = op.get("type")
    key = op.get("key", "")
    if typ == "add_tag":
        if key and key not in out:
            out[key] = op.get("value", "")
    elif typ == "set_tag":
        if key:
            out[key] = op.get("value", "")
    elif typ == "rename_key":
        frm, to = op.get("key", ""), op.get("to_key", "")
        # Case-insensitive source match.
        match = next((k for k in out if k.lower() == frm.lower()), None)
        if match and to:
            out[to] = out.pop(match)
    elif typ == "normalize_value":
        match = next((k for k in out if k.lower() == key.lower()), None)
        frm, to = op.get("from_value"), op.get("to_value", "")
        if match and (frm is None or str(out[match]) == str(frm)):
            out[match] = to
    elif typ == "remove_key":
        # Delete the tag key entirely (case-insensitive match). The before→after diff then has the
        # key in `before` but absent from `after`, so the apply path emits an atomic Replace of the
        # remaining set (the only safe way to drop a key — see `_needs_replace`).
        match = next((k for k in out if k.lower() == key.lower()), None)
        if match:
            out.pop(match)
    return out


def _apply_ops(tags: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a list of operations over a tag dict, in order (pure)."""
    out = dict(tags)
    for op in operations:
        out = _apply_to_tags(out, op)
    return out


def build_plan_ops(resources: list[dict[str, Any]], operations: list[dict[str, Any]],
                   resource_ids: list[str] | None = None) -> dict[str, Any]:
    """Dry-run a change-set (one or more operations applied in order) across the selected
    resource ids. Returns per-resource before/after with overwrite detection. An empty/None
    ``resource_ids`` targets every resource the change-set would actually change."""
    ops = [op for op in (operations or []) if op.get("type") in OPS]
    if not ops:
        raise ValueError("no valid operations in change-set")
    by_id = {(r.get("id") or "").lower(): r for r in resources}
    target_ids = [rid.lower() for rid in (resource_ids or [])] or list(by_id.keys())

    items: list[dict[str, Any]] = []
    overwrites = 0
    subs: set[str] = set()
    for rid in target_ids:
        r = by_id.get(rid)
        if not r:
            continue
        before = r.get("tags") or {}
        after = _apply_ops(before, ops)
        if after == before:
            continue  # no-op for this resource
        # Overwrite = an existing key's value changed (vs newly added).
        changed_keys = [k for k in after if k in before and str(after[k]) != str(before[k])]
        if changed_keys:
            overwrites += 1
        if r.get("subscription_id"):
            subs.add(r["subscription_id"])
        items.append({
            "id": r.get("id", ""), "name": r.get("name", ""), "type": r.get("type", ""),
            "resource_group": r.get("resource_group", ""), "subscription_id": r.get("subscription_id", ""),
            "before": before, "after": after, "overwrite": bool(changed_keys),
        })
    return {
        "operations": ops,
        "op": ops[0] if len(ops) == 1 else {},
        "items": items[:1000],
        "count": len(items),
        "overwrites": overwrites,
        "subscription_count": len(subs),
        "generated_at": _now(),
    }


def build_plan(resources: list[dict[str, Any]], op: dict[str, Any]) -> dict[str, Any]:
    """Dry-run a SINGLE operation (back-compat shim over ``build_plan_ops``). ``op.resource_ids``
    selects the targets."""
    if op.get("type") not in OPS:
        raise ValueError(f"unsupported op type: {op.get('type')}")
    return build_plan_ops(resources, [op], op.get("resource_ids"))


def _needs_replace(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when the change removes or RE-CASES a key — cases that ``Merge`` cannot express.

    Azure tag keys are CASE-INSENSITIVE, so a rename like ``costcenter`` → ``CostCenter`` looks
    like "remove costcenter, add CostCenter" to a Merge+Delete pair — and the Delete then strips
    the very key the Merge just wrote (they're the same key to Azure), wiping the tag. Worse,
    ``Merge`` can't change a key's case at all. The only correct primitive for a removal or a
    case-rename is a full ``Replace`` of the resource's complete desired tag set. A key present in
    ``before`` but absent from ``after`` (case-sensitive check — so a re-cased key counts) means we
    must Replace."""
    return any(k not in after for k in before)


def _ps_for_item(item: dict[str, Any]) -> list[str]:
    """PowerShell tag commands derived from the resource's before->after diff. Add/update-only
    changes use ``Merge`` (preserve tags we don't manage); removals or case-renames use a full
    ``Replace`` of the desired set (the only operation that can drop or re-case a key)."""
    rid = item["id"]
    before, after = item.get("before") or {}, item.get("after") or {}
    if after == before:
        return []  # no-op for this resource
    if _needs_replace(before, after):
        if not after:
            return [f"Update-AzTag -ResourceId '{rid}' -Tag @{{}} -Operation Replace"]
        pairs = "; ".join(f"'{k}' = '{v}'" for k, v in after.items())
        return [f"Update-AzTag -ResourceId '{rid}' -Tag @{{ {pairs} }} -Operation Replace"]
    merges = {k: v for k, v in after.items() if str(before.get(k)) != str(v)}
    pairs = "; ".join(f"'{k}' = '{v}'" for k, v in merges.items())
    return [f"Update-AzTag -ResourceId '{rid}' -Tag @{{ {pairs} }} -Operation Merge"]


def _cli_for_item(item: dict[str, Any]) -> list[str]:
    """Azure CLI tag commands derived from the resource's before->after diff. Add/update-only
    changes use ``Merge``; removals or case-renames use a full ``Replace`` of the desired set so a
    case-insensitive Delete can never strip the key it just wrote."""
    rid = item["id"]
    before, after = item.get("before") or {}, item.get("after") or {}
    if after == before:
        return []  # no-op for this resource
    if _needs_replace(before, after):
        if not after:
            # Every tag removed → clear them all in one call.
            return [f"az tag delete --resource-id '{rid}' --yes"]
        tags = " ".join(f"\"{k}={v}\"" for k, v in after.items())
        return [f"az tag update --resource-id '{rid}' --operation Replace --tags {tags}"]
    merges = {k: v for k, v in after.items() if str(before.get(k)) != str(v)}
    tags = " ".join(f"\"{k}={v}\"" for k, v in merges.items())
    return [f"az tag update --resource-id '{rid}' --operation Merge --tags {tags}"]


def _rollback_for_item(item: dict[str, Any]) -> list[str]:
    """Restore the resource's prior tag set EXACTLY via a full ``Replace`` of ``before`` — the
    same recovery the revision-revert path uses. Replace (not Merge/Delete) so a case-rename is
    undone cleanly and no stale key survives."""
    rid = item["id"]
    before = item.get("before") or {}
    if not before:
        return [f"Update-AzTag -ResourceId '{rid}' -Tag @{{}} -Operation Replace"]
    pairs = "; ".join(f"'{k}' = '{v}'" for k, v in before.items())
    return [f"Update-AzTag -ResourceId '{rid}' -Tag @{{ {pairs} }} -Operation Replace"]


def _describe_item(item: dict[str, Any]) -> str:
    """A short, human-readable description of the tag change for one resource (drives the live
    apply status feed): e.g. "add Owner=platform-team, set Environment=Production (was prod),
    remove temp"."""
    before, after = item.get("before") or {}, item.get("after") or {}
    parts: list[str] = []
    for k, v in after.items():
        if str(before.get(k)) != str(v):
            parts.append(f"set {k}={v} (was {before[k]})" if k in before else f"add {k}={v}")
    parts += [f"remove {k}" for k in before if k not in after]
    return ", ".join(parts) or "no change"


# Summary shape returned when a plan has nothing to do or can't run; shared by the streaming and
# drained code paths so both behave identically.
def _blocked(total: int, reason: str) -> dict[str, Any]:
    return {"applied": 0, "failed": 0, "total": total, "results": [], "blocked": True, "reason": reason}


async def apply_plan_stream(
    plan: dict[str, Any], connection: dict[str, Any] | None, *, actor: str = "",
    concurrency: int = _APPLY_CONCURRENCY,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming variant of :func:`apply_plan`. Yields a live status event for every resource as
    its tag write runs — ``{"event": "start"}`` → (``item_start`` → ``item_done``)* →
    ``{"event": "done", ...}`` — where the final ``done`` event carries the exact same summary
    keys :func:`apply_plan` returns. Same governance: a read-only / missing connection short-
    circuits to a single blocked ``done``. Never raises on a single-resource failure.

    Two guarantees this enforces:

    * **Snapshot-before-write.** Before any change runs we read each target resource's CURRENT
      tags fresh from Azure and rebase the plan on them (``before`` = the live snapshot, ``after``
      = the operations folded onto that snapshot). This makes the recovery revision exact even if
      the cached census drifted, and the writes idempotent. If the snapshot can't be captured we
      refuse to write (no blind changes).
    * **Multi-threaded apply.** The per-resource writes fan out across a bounded worker pool
      (``concurrency``) instead of running strictly one-at-a-time, so a large plan applies far
      faster. Per-resource status events still stream as each write completes, and the running
      ``applied``/``failed`` tallies on every ``item_done`` stay monotonic."""
    from app.azure.tag_ops import read_current_tags
    from app.exec.command_runner import run_command_capture

    items = plan.get("items", [])
    total = len(items)
    if not items:
        yield {"event": "done", "applied": 0, "failed": 0, "total": 0, "results": [], "blocked": False, "reason": ""}
        return
    if connection is None:
        yield {"event": "done",
               **_blocked(total, "No Azure connection is bound to this scope, so tag changes can't be applied.")}
        return
    if bool(connection.get("read_only")):
        yield {"event": "done",
               **_blocked(total, "The selected Azure connection is read-only. Enable writes on the connection "
                                 "(Settings → Azure Tenants) or pick a writable connection, then run again.")}
        return

    # SNAPSHOT — always read each resource's CURRENT tags fresh from Azure BEFORE writing, so the
    # recovery revision restores the EXACT prior state even if the cached census has drifted since
    # the last scan. A failed snapshot means we can't guarantee a clean revert, so we refuse to
    # write at all rather than risk an unrevertible change.
    rids = [it.get("id", "") for it in items if it.get("id")]
    snapshot, _snap_names, snap_err = await read_current_tags(connection, rids)
    if snap_err:
        yield {"event": "done",
               **_blocked(total, "Couldn't capture a snapshot of current tag values before applying "
                                 f"({snap_err}). No changes were made.")}
        return
    # Rebase every item on the authoritative live tags.
    ops = plan.get("operations") or ([plan["op"]] if plan.get("op") else [])
    for it in items:
        rid = (it.get("id") or "").lower()
        live = dict(snapshot.get(rid, {}))
        it["before"] = live
        if ops:
            it["after"] = _apply_ops(live, ops)

    # Drop items the live snapshot turned into no-ops (the cached plan thought there was a change,
    # but the resource's current tags already match the desired state — or the key it meant to
    # rename no longer exists). Applying these would write nothing yet count as "applied", which is
    # exactly the misleading "8 applied but my tag is gone" symptom. We skip them and report the
    # count separately so the tallies are truthful.
    applicable = [it for it in items if (it.get("before") or {}) != (it.get("after") or {})]
    skipped = total - len(applicable)
    work_total = len(applicable)
    if work_total == 0:
        yield {"event": "done", "applied": 0, "failed": 0, "total": 0, "skipped": skipped,
               "results": [], "blocked": False, "reason": "",
               "note": "Nothing to apply — every selected resource already matches the desired tags."}
        return

    yield {"event": "start", "total": work_total, "skipped": skipped,
           "connection": connection.get("name", ""),
           "subscription_count": plan.get("subscription_count", 0)}

    # Fan the writes out across a bounded worker pool. Each worker pushes its item_start /
    # item_done events onto a queue that this generator drains, so status still streams live even
    # though the writes run concurrently.
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _run_one(idx: int, it: dict[str, Any]) -> None:
        async with sem:
            await out_q.put({"event": "item_start", "index": idx, "total": work_total, "id": it.get("id", ""),
                             "name": it.get("name", ""), "type": it.get("type", ""),
                             "resource_group": it.get("resource_group", ""), "change": _describe_item(it)})
            ok = True
            err = ""
            try:
                for cmd in _cli_for_item(it):
                    cap = await run_command_capture(cmd, connection, read_only=False, confirm=True)
                    if not cap.ok:
                        ok = False
                        err = (cap.error or cap.stderr or "command failed").strip()[:300]
                        break
            except Exception as exc:  # noqa: BLE001 — never let one resource wedge the pool.
                ok = False
                err = str(exc)[:300]
            await out_q.put({"event": "item_done", "index": idx, "total": work_total, "id": it.get("id", ""),
                             "name": it.get("name", ""), "ok": ok, "error": err})

    tasks = [asyncio.create_task(_run_one(i, it)) for i, it in enumerate(applicable, start=1)]

    results: list[dict[str, Any]] = []
    applied = failed = 0
    pending = work_total  # number of item_done events still expected
    try:
        while pending > 0:
            ev = await out_q.get()
            if ev.get("event") == "item_done":
                if ev.get("ok"):
                    applied += 1
                else:
                    failed += 1
                results.append({"id": ev.get("id", ""), "name": ev.get("name", ""),
                                "ok": ev.get("ok", False), "error": ev.get("error", "")})
                pending -= 1
                yield {**ev, "applied": applied, "failed": failed}
            else:
                yield ev
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    yield {"event": "done", "applied": applied, "failed": failed, "total": work_total,
           "skipped": skipped, "results": results, "blocked": False, "reason": ""}


async def apply_plan(plan: dict[str, Any], connection: dict[str, Any] | None, *,
                     actor: str = "") -> dict[str, Any]:
    """Execute a plan's tag changes against Azure (the actual write path).

    Runs the per-resource ``az tag update`` commands through the shared command runner, which
    enforces governance: the connection's ``read_only`` flag blocks mutating commands and the
    admin ``command_execution_enabled`` setting must be on. We pass ``confirm=True`` because the
    caller has already obtained explicit user approval. Returns per-resource results plus a
    summary; never raises on a single-resource failure (best-effort, fully reported).

    Thin wrapper that drains :func:`apply_plan_stream` so the streaming and non-streaming apply
    routes share one execution path."""
    final = {"applied": 0, "failed": 0, "total": 0, "results": [], "blocked": False, "reason": ""}
    async for ev in apply_plan_stream(plan, connection, actor=actor):
        if ev.get("event") == "done":
            final = {k: v for k, v in ev.items() if k != "event"}
    return final


def generate_scripts(plan: dict[str, Any]) -> dict[str, Any]:
    """Generate PowerShell / Azure CLI / Resource Graph / rollback scripts for a plan. Driven by
    each resource's before->after diff, so single- and multi-operation change-sets both work."""
    items = plan.get("items", [])
    ps: list[str] = ["# Tag remediation — review before running. Requires 'Tag Contributor'.", "Connect-AzAccount  # if not already signed in"]
    cli: list[str] = ["# Tag remediation — review before running. Requires 'Tag Contributor'."]
    rollback: list[str] = ["# Rollback — restores tags to their pre-change values."]
    for it in items:
        ps += _ps_for_item(it)
        cli += _cli_for_item(it)
        rollback += _rollback_for_item(it)
    ids = ", ".join(f"'{it['id']}'" for it in items[:50])
    arg = ("resources\n| where id in~ (" + ids + ")\n| project id, name, type, resourceGroup, tags") if ids else "// no resources selected"
    return {
        "powershell": "\n".join(ps),
        "azcli": "\n".join(cli),
        "arg": arg,
        "rollback": "\n".join(rollback),
    }



# --------------------------------------------------------------------------- plan store


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def save_plan(tenant_id: str, plan: dict[str, Any], *, actor: str = "", approved: bool = False,
              applied: bool = False, result: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _read()
    bucket = data.setdefault(tenant_id or "default", [])
    record = {
        "id": uuid.uuid4().hex,
        "created_at": _now(),
        "actor": actor,
        "approved": approved,
        "applied": applied,
        "op": plan.get("op", {}),
        "operations": plan.get("operations", []),
        "count": plan.get("count", 0),
        "overwrites": plan.get("overwrites", 0),
        "result": result or None,
    }
    bucket.insert(0, record)
    del bucket[100:]
    _write(data)
    return record


def list_plans(tenant_id: str) -> list[dict[str, Any]]:
    return _read().get(tenant_id or "default", [])


# --------------------------------------------------------------------------- change-set store
# Named, reusable change-sets (a list of operations / key:value pairs) the user can save once
# and re-load to preview / dry-run / generate-apply scripts across scopes.


def _cs_read() -> dict[str, Any]:
    if _CS_PATH.exists():
        try:
            data = json.loads(_CS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _cs_write(data: dict[str, Any]) -> None:
    try:
        _CS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _clean_ops(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only valid operations, retaining just the fields each op type uses."""
    out: list[dict[str, Any]] = []
    for op in operations or []:
        typ = op.get("type")
        if typ not in OPS:
            continue
        if typ in ("add_tag", "set_tag"):
            if not op.get("key"):
                continue
            out.append({"type": typ, "key": str(op.get("key", "")), "value": str(op.get("value", ""))})
        elif typ == "rename_key":
            if not (op.get("key") and op.get("to_key")):
                continue
            out.append({"type": typ, "key": str(op["key"]), "to_key": str(op["to_key"])})
        elif typ == "normalize_value":
            if not (op.get("key") and op.get("to_value")):
                continue
            entry = {"type": typ, "key": str(op["key"]), "to_value": str(op["to_value"])}
            if op.get("from_value") not in (None, ""):
                entry["from_value"] = str(op["from_value"])
            out.append(entry)
    return out


def list_changesets(tenant_id: str) -> list[dict[str, Any]]:
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    rows = list(bucket["changesets"].values())
    rows.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return rows


def get_changeset(tenant_id: str, cs_id: str) -> dict[str, Any] | None:
    data = _cs_read()
    return _cs_bucket(data, tenant_id)["changesets"].get(cs_id)


# --------------------------------------------------------------------------- store layout
# A tenant's change-set store holds two maps: ``groups`` (folders, for organizing change-sets
# into ops initiatives) and ``changesets`` (the actual records). Legacy stores were a flat
# ``{cs_id: record}`` map; ``_cs_bucket`` migrates those in place on first access.


def _cs_bucket(data: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    key = tenant_id or "default"
    raw = data.get(key)
    if not isinstance(raw, dict):
        raw = {}
    if "changesets" in raw or "groups" in raw:
        raw.setdefault("groups", {})
        raw.setdefault("changesets", {})
    elif raw and all(isinstance(v, dict) and "operations" in v for v in raw.values()):
        # Legacy flat format → migrate to the grouped layout (everything starts ungrouped).
        raw = {"groups": {}, "changesets": dict(raw)}
    else:
        raw = {"groups": {}, "changesets": {}}
    data[key] = raw
    return raw


def _op_breakdown(operations: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for op in operations or []:
        t = op.get("type", "")
        if t:
            out[t] = out.get(t, 0) + 1
    return out


def _affected_keys(operations: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for op in operations or []:
        for k in (op.get("key", ""), op.get("to_key", "")):
            if k and k not in keys:
                keys.append(k)
    return keys


def save_changeset(tenant_id: str, cs: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Create or update (by id) a named change-set. Supports a group, free-form labels and a
    description in addition to the operations."""
    name = str(cs.get("name", "")).strip()
    if not name:
        raise ValueError("change-set name is required")
    ops = _clean_ops(cs.get("operations") or [])
    if not ops:
        raise ValueError("change-set needs at least one valid operation")
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    cs_id = cs.get("id") or uuid.uuid4().hex
    prior = bucket["changesets"].get(cs_id, {})
    # Validate the group exists (or clear it).
    group_id = str(cs.get("group_id", "") or "")
    if group_id and group_id not in bucket["groups"]:
        group_id = ""
    labels = [str(l).strip() for l in (cs.get("labels") or []) if str(l).strip()]
    labels = list(dict.fromkeys(labels))[:12]  # dedupe (order-preserving), cap at 12
    record = {
        "id": cs_id,
        "name": name,
        "description": str(cs.get("description", "")),
        "group_id": group_id,
        "labels": labels,
        "operations": ops,
        "op_breakdown": _op_breakdown(ops),
        "affected_keys": _affected_keys(ops),
        "actor": actor or prior.get("actor", ""),
        "created_at": prior.get("created_at") or _now(),
        "updated_at": _now(),
        "last_run": prior.get("last_run"),
        "run_count": prior.get("run_count", 0),
    }
    bucket["changesets"][cs_id] = record
    _cs_write(data)
    return record


def delete_changeset(tenant_id: str, cs_id: str) -> bool:
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    if cs_id in bucket["changesets"]:
        del bucket["changesets"][cs_id]
        _cs_write(data)
        return True
    return False


def duplicate_changeset(tenant_id: str, cs_id: str, *, actor: str = "") -> dict[str, Any] | None:
    """Clone a change-set (same group/labels/operations) under a new id + "(copy)" name."""
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    src = bucket["changesets"].get(cs_id)
    if not src:
        return None
    new_id = uuid.uuid4().hex
    record = {
        **src,
        "id": new_id,
        "name": f"{src.get('name', 'Change-set')} (copy)"[:200],
        "actor": actor or src.get("actor", ""),
        "created_at": _now(),
        "updated_at": _now(),
        "last_run": None,
        "run_count": 0,
    }
    bucket["changesets"][new_id] = record
    _cs_write(data)
    return record


def move_changeset(tenant_id: str, cs_id: str, group_id: str) -> dict[str, Any] | None:
    """Reassign a change-set to a group (``""`` = ungrouped)."""
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    rec = bucket["changesets"].get(cs_id)
    if not rec:
        return None
    gid = group_id or ""
    if gid and gid not in bucket["groups"]:
        return None
    rec["group_id"] = gid
    rec["updated_at"] = _now()
    _cs_write(data)
    return rec


def record_changeset_run(tenant_id: str, cs_id: str, summary: dict[str, Any]) -> None:
    """Stamp a change-set's last-run audit info (scope, applied/failed, when) — the cloud-ops
    sanity trail. No-op when the change-set isn't found (ad-hoc runs aren't tracked)."""
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    rec = bucket["changesets"].get(cs_id)
    if not rec:
        return
    rec["last_run"] = {**summary, "at": _now()}
    rec["run_count"] = int(rec.get("run_count", 0)) + 1
    _cs_write(data)


# --------------------------------------------------------------------------- import / export

EXPORT_KIND = "tagintel-changesets"
EXPORT_VERSION = 1


def export_changesets(tenant_id: str, ids: list[str] | None = None) -> dict[str, Any]:
    """Build a portable bundle of change-sets (plus the groups they reference) for download.

    The bundle is self-contained: each change-set keeps its name/description/labels/operations
    and a ``group_id`` that points at one of the bundled groups. Audit fields (ids, timestamps,
    last-run, actor) are intentionally dropped so a re-import is clean and non-destructive."""
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    rows = list(bucket["changesets"].values())
    if ids is not None:
        wanted = set(ids)
        rows = [c for c in rows if c.get("id") in wanted]
    used_groups = {c.get("group_id") for c in rows if c.get("group_id")}
    groups = [
        {"id": g["id"], "name": g.get("name", ""), "color": g.get("color", ""),
         "description": g.get("description", ""), "order": g.get("order", 0)}
        for g in bucket["groups"].values() if g["id"] in used_groups
    ]
    changesets = [
        {"name": c.get("name", ""), "description": c.get("description", ""),
         "group_id": c.get("group_id", ""), "labels": list(c.get("labels", []) or []),
         "operations": list(c.get("operations", []) or [])}
        for c in rows
    ]
    return {
        "kind": EXPORT_KIND,
        "version": EXPORT_VERSION,
        "exported_at": _now(),
        "groups": groups,
        "changesets": changesets,
    }


def import_changesets(tenant_id: str, payload: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """Import a bundle produced by :func:`export_changesets`.

    Non-destructive: every change-set is added as a NEW record (existing ones are never
    overwritten). Referenced groups are matched to an existing group by name (case-insensitive)
    or created. Returns ``{imported, groups_created, skipped, errors}``."""
    if not isinstance(payload, dict):
        raise ValueError("invalid import file — expected a change-set bundle")
    kind = payload.get("kind")
    if kind and kind != EXPORT_KIND:
        raise ValueError(f"unrecognized file (kind='{kind}') — expected a Tag Intelligence change-set export")
    raw_groups = payload.get("groups") or []
    raw_changesets = payload.get("changesets") or []
    if not isinstance(raw_changesets, list) or not raw_changesets:
        raise ValueError("the file contains no change-sets to import")

    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)

    # Map an exported group id -> local group id (reuse a same-named group, else create one).
    existing_by_name = {str(g.get("name", "")).strip().lower(): g["id"] for g in bucket["groups"].values()}
    group_id_map: dict[str, str] = {}
    groups_created = 0
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name", "")).strip()
        if not name:
            continue
        old_id = str(g.get("id", ""))
        key = name.lower()
        local_id = existing_by_name.get(key)
        if not local_id:
            local_id = uuid.uuid4().hex
            bucket["groups"][local_id] = {
                "id": local_id, "name": name,
                "color": str(g.get("color", "") or _GROUP_COLORS[len(bucket["groups"]) % len(_GROUP_COLORS)]),
                "description": str(g.get("description", "")),
                "order": int(g.get("order", len(bucket["groups"]))),
                "created_at": _now(), "updated_at": _now(),
            }
            existing_by_name[key] = local_id
            groups_created += 1
        if old_id:
            group_id_map[old_id] = local_id

    imported = 0
    skipped = 0
    errors: list[str] = []
    for c in raw_changesets:
        if not isinstance(c, dict):
            skipped += 1
            continue
        name = str(c.get("name", "")).strip()
        ops = _clean_ops(c.get("operations") or [])
        if not name:
            errors.append("a change-set is missing a name")
            skipped += 1
            continue
        if not ops:
            errors.append(f"'{name}' has no valid operations")
            skipped += 1
            continue
        gid = group_id_map.get(str(c.get("group_id", "")), "")
        labels = [str(l).strip() for l in (c.get("labels") or []) if str(l).strip()]
        labels = list(dict.fromkeys(labels))[:12]
        cs_id = uuid.uuid4().hex
        bucket["changesets"][cs_id] = {
            "id": cs_id, "name": name[:200], "description": str(c.get("description", "")),
            "group_id": gid, "labels": labels, "operations": ops,
            "op_breakdown": _op_breakdown(ops), "affected_keys": _affected_keys(ops),
            "actor": actor, "created_at": _now(), "updated_at": _now(),
            "last_run": None, "run_count": 0,
        }
        imported += 1

    _cs_write(data)
    return {"imported": imported, "groups_created": groups_created, "skipped": skipped, "errors": errors}


# --------------------------------------------------------------------------- groups (folders)


def list_groups(tenant_id: str) -> list[dict[str, Any]]:
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    counts: dict[str, int] = {}
    for cs in bucket["changesets"].values():
        gid = cs.get("group_id", "")
        if gid:
            counts[gid] = counts.get(gid, 0) + 1
    rows = []
    for g in bucket["groups"].values():
        rows.append({**g, "count": counts.get(g["id"], 0)})
    rows.sort(key=lambda g: (g.get("order", 0), g.get("name", "").lower()))
    return rows


_GROUP_COLORS = ("blue", "green", "amber", "violet", "rose", "cyan", "slate")


def save_group(tenant_id: str, group: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    name = str(group.get("name", "")).strip()
    if not name:
        raise ValueError("group name is required")
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    gid = group.get("id") or uuid.uuid4().hex
    prior = bucket["groups"].get(gid, {})
    color = str(group.get("color", "") or prior.get("color") or _GROUP_COLORS[len(bucket["groups"]) % len(_GROUP_COLORS)])
    record = {
        "id": gid,
        "name": name,
        "color": color,
        "description": str(group.get("description", "")),
        "order": int(group.get("order", prior.get("order", len(bucket["groups"])))),
        "created_at": prior.get("created_at") or _now(),
        "updated_at": _now(),
    }
    bucket["groups"][gid] = record
    _cs_write(data)
    return record


def delete_group(tenant_id: str, group_id: str) -> bool:
    """Delete a group; its change-sets are moved to ungrouped (never deleted with the group)."""
    data = _cs_read()
    bucket = _cs_bucket(data, tenant_id)
    if group_id not in bucket["groups"]:
        return False
    del bucket["groups"][group_id]
    for cs in bucket["changesets"].values():
        if cs.get("group_id") == group_id:
            cs["group_id"] = ""
    _cs_write(data)
    return True


