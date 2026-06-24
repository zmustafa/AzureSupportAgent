"""Tag revisions — the shared recovery + revert keystone for every tag mutation.

Whenever the app writes tags to Azure (ownership owner-tag apply OR tag-intelligence
remediation), it first captures the affected resources' CURRENT tags as a *revision* (the
recovery copy), then records the new tags it set. A revision can be **reverted**: each
resource's tags are restored to the captured ``before`` state via an ARM ``Replace`` (so the
prior tag set is reinstated exactly — added keys removed, changed keys restored, deleted keys
re-added). The revert itself snapshots the current state first, so it is redo-able.

Stored bounded in ``.data/tag_revisions.json`` keyed by ``tenant : connection``. This is the
single source of truth the UI lists, visualizes (before→after diff) and reverts from.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "tag_revisions.json"
_MAX_PER_KEY = 100  # bounded history per tenant:connection

# Bounded concurrency for the revert write fan-out (mirrors the apply path). Keeps a revert over
# many resources fast (a few seconds) so it never blows past a client/proxy connection timeout.
_REVERT_CONCURRENCY = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _key(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or 'default'}|{connection_id or ''}"


def _summary(rev: dict[str, Any]) -> dict[str, Any]:
    """Compact list view (no per-resource tag maps)."""
    return {
        "id": rev["id"],
        "created_at": rev["created_at"],
        "actor": rev.get("actor", ""),
        "source": rev.get("source", ""),
        "description": rev.get("description", ""),
        "connection_id": rev.get("connection_id", ""),
        "scope": rev.get("scope", ""),
        "resource_count": len(rev.get("before", {})),
        "applied": rev.get("applied", 0),
        "failed": rev.get("failed", 0),
        "status": rev.get("status", "applied"),
        "reverted_at": rev.get("reverted_at", ""),
        "reverted_by": rev.get("reverted_by", ""),
        "reverts_id": rev.get("reverts_id", ""),
    }


def save_revision(
    tenant_id: str,
    connection_id: str,
    *,
    source: str,
    description: str,
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    names: dict[str, str] | None = None,
    actor: str = "",
    scope: str = "",
    applied: int = 0,
    failed: int = 0,
    reverts_id: str = "",
) -> dict[str, Any]:
    """Persist a recovery revision (the ``before`` is the recovery copy). Returns its summary."""
    data = _read()
    bucket = data.setdefault(_key(tenant_id, connection_id), [])
    rev = {
        "id": uuid.uuid4().hex,
        "created_at": _now(),
        "actor": actor,
        "source": source,
        "description": description,
        "connection_id": connection_id,
        "tenant_id": tenant_id or "default",
        "scope": scope,
        "before": {k.lower(): v for k, v in (before or {}).items()},
        "after": {k.lower(): v for k, v in (after or {}).items()},
        "names": names or {},
        "applied": applied,
        "failed": failed,
        "status": "applied",
        "reverts_id": reverts_id,
    }
    bucket.insert(0, rev)
    del bucket[_MAX_PER_KEY:]
    _write(data)
    return _summary(rev)


def list_revisions(tenant_id: str, connection_id: str = "", source: str = "") -> list[dict[str, Any]]:
    """All revisions for a tenant (optionally a single connection / source), newest first."""
    data = _read()
    out: list[dict[str, Any]] = []
    for key, bucket in data.items():
        ktenant, kconn = (key.split("|", 1) + [""])[:2]
        if (ktenant or "default") != (tenant_id or "default"):
            continue
        if connection_id and kconn != connection_id:
            continue
        for rev in bucket:
            if source and rev.get("source") != source:
                continue
            out.append(_summary(rev))
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out


def get_revision(tenant_id: str, rev_id: str) -> dict[str, Any] | None:
    """Full revision (incl. before/after tag maps) for visualization."""
    data = _read()
    for key, bucket in data.items():
        ktenant = (key.split("|", 1) + [""])[0]
        if (ktenant or "default") != (tenant_id or "default"):
            continue
        for rev in bucket:
            if rev.get("id") == rev_id:
                return rev
    return None


def diff_rows(rev: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-resource before→after rows for the UI (added / changed / removed tag keys)."""
    rows: list[dict[str, Any]] = []
    before = rev.get("before", {})
    after = rev.get("after", {})
    names = rev.get("names", {})
    for rid in sorted(set(before) | set(after)):
        b = before.get(rid, {})
        a = after.get(rid, {})
        added = {k: a[k] for k in a if k not in b}
        removed = {k: b[k] for k in b if k not in a}
        changed = {k: {"from": b[k], "to": a[k]} for k in a if k in b and str(a[k]) != str(b[k])}
        if not (added or removed or changed):
            continue
        rows.append({
            "id": rid,
            "name": names.get(rid, "") or names.get(rid.lower(), ""),
            "before": b,
            "after": a,
            "added": added,
            "removed": removed,
            "changed": changed,
        })
    return rows


def _mark_reverted(tenant_id: str, rev_id: str, actor: str) -> None:
    data = _read()
    for key, bucket in data.items():
        ktenant = (key.split("|", 1) + [""])[0]
        if (ktenant or "default") != (tenant_id or "default"):
            continue
        for rev in bucket:
            if rev.get("id") == rev_id:
                rev["status"] = "reverted"
                rev["reverted_at"] = _now()
                rev["reverted_by"] = actor
                _write(data)
                return


async def revert_revision(
    tenant_id: str,
    rev_id: str,
    connection: dict[str, Any] | None,
    *,
    actor: str = "",
) -> dict[str, Any]:
    """Restore every resource in the revision to its captured ``before`` tag set (ARM Replace).

    Also records a NEW revision capturing the pre-revert state (so a revert is itself
    revertible — i.e. redo). Returns ``{ok, reverted, failed, total, results, new_revision}``."""
    rev = get_revision(tenant_id, rev_id)
    if rev is None:
        return {"ok": False, "error": "Revision not found.", "reverted": 0, "failed": 0, "total": 0, "results": []}
    if rev.get("status") == "reverted":
        return {"ok": False, "error": "This revision has already been reverted.", "reverted": 0, "failed": 0, "total": 0, "results": []}
    if connection is None:
        return {"ok": False, "error": "No Azure connection configured.", "reverted": 0, "failed": 0, "total": 0, "results": []}

    from app.azure.tag_ops import read_current_tags, set_resource_tags

    before = rev.get("before", {})
    rids = list(before.keys())
    # Snapshot CURRENT state first so the revert is redo-able.
    cur_tags, cur_names, _err = await read_current_tags(connection, rids)

    # Restore each resource's prior tag set via ARM Replace, fanned out across a bounded worker
    # pool. A sequential revert acquired an ARM token + made an ARM round-trip PER resource, so a
    # revision spanning many resources took tens of seconds and could blow past a client/proxy
    # connection timeout — surfacing in the UI as "TypeError: Failed to fetch". Concurrency keeps
    # the whole revert fast (a few seconds) regardless of resource count, while the per-resource
    # results stay in their original order for a deterministic, redo-able inverse revision.
    sem = asyncio.Semaphore(_REVERT_CONCURRENCY)

    async def _revert_one(rid: str) -> tuple[str, bool, str]:
        target = before.get(rid, {})  # the recovery copy = restore exactly this set
        async with sem:
            try:
                ok, err = await set_resource_tags(connection, rid, target, operation="Replace")
            except Exception as exc:  # noqa: BLE001 — report, never wedge the pool.
                ok, err = False, str(exc)[:300]
        return rid, ok, err

    outcomes = await asyncio.gather(*[_revert_one(rid) for rid in rids])

    results: list[dict[str, Any]] = []
    reverted = 0
    failed = 0
    after_state: dict[str, dict[str, str]] = {}
    for rid, ok, err in outcomes:
        after_state[rid] = before.get(rid, {})
        if ok:
            reverted += 1
        else:
            failed += 1
        results.append({"id": rid, "ok": ok, "error": err})

    _mark_reverted(tenant_id, rev_id, actor)
    # Record the inverse as a new applied revision (before = pre-revert current state).
    new_summary = save_revision(
        tenant_id, rev.get("connection_id", ""),
        source=f"revert:{rev.get('source', '')}",
        description=f"Revert of: {rev.get('description', '')[:160]}",
        before={rid: cur_tags.get(rid, {}) for rid in rids},
        after=after_state,
        names={**cur_names, **rev.get("names", {})},
        actor=actor,
        scope=rev.get("scope", ""),
        applied=reverted,
        failed=failed,
        reverts_id=rev_id,
    )
    return {
        "ok": failed == 0,
        "reverted": reverted,
        "failed": failed,
        "total": len(rids),
        "results": results,
        "new_revision": new_summary,
    }
