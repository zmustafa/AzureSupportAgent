"""Immutable snapshot store + locker index.

Index at ``.data/evidence_locker.json`` (append-only metadata); each snapshot's full content
is a write-once blob at ``.data/evidence/<id>.json``. The SHA-256 is computed over the
canonicalized content at creation and never recomputed-to-overwrite; ``verify_sha`` re-hashes
the stored blob on read to prove integrity. Content is never mutated after write; the only
removal is retention-expiry purge of non-audit-class snapshots."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_INDEX = Path(__file__).resolve().parents[2] / ".data" / "evidence_locker.json"
_BLOB_DIR = Path(__file__).resolve().parents[2] / ".data" / "evidence"

RETENTION_CLASSES = ("standard", "audit")
INCLUDE_KEYS = (
    "inventory", "properties", "changes", "metrics", "findings", "architecture", "memory", "activity",
    # An identity investigation frozen at a point in time. Extending the existing section
    # list rather than inventing a second snapshot type keeps diff, share and export working
    # unchanged — an auditor's evidence pack should not care what it is about.
    "identity",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_index() -> dict[str, Any]:
    data = jsonstore.read_json(_INDEX, {"snapshots": {}})
    return data if isinstance(data, dict) else {"snapshots": {}}


def _canonical(content: dict[str, Any]) -> str:
    """Stable canonical JSON for hashing (sorted keys, compact)."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_sha(content: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _blob_path(snapshot_id: str) -> Path:
    return _BLOB_DIR / f"{snapshot_id}.json"


def create_snapshot(
    *,
    tenant_id: str,
    name: str,
    scope: dict[str, Any],
    included: list[str],
    retention_class: str,
    tags: list[str],
    content: dict[str, Any],
    created_by: str,
    finding_links: list[str] | None = None,
    demo: bool = False,
) -> dict[str, Any]:
    """Write a write-once content blob + an immutable index entry. Returns the metadata."""
    sid = str(uuid.uuid4())
    sha = compute_sha(content)
    # Write the content blob once.
    jsonstore.write_json(_blob_path(sid), content, indent=None, separators=(",", ":"))
    size = _blob_path(sid).stat().st_size

    meta = {
        "id": sid,
        "tenant_id": tenant_id,
        "name": name or "Snapshot",
        "scope": scope or {},
        "included": [k for k in included if k in INCLUDE_KEYS],
        "retention_class": retention_class if retention_class in RETENTION_CLASSES else "standard",
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "finding_links": finding_links or [],
        "sha256": sha,
        "size": size,
        "section_counts": {k: _section_count(v) for k, v in content.items()},
        "created_by": created_by,
        "created_at": _now(),
        "attachments": [],   # appended on attach (ticket refs); does not change content/SHA
        "shares": [],        # share tokens (metadata only)
        "demo": demo,
    }
    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault("snapshots", {})[sid] = meta

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    return meta


def _section_count(section: Any) -> int:
    if isinstance(section, list):
        return len(section)
    if isinstance(section, dict):
        for k in ("resources", "items", "findings", "changes", "rows"):
            if isinstance(section.get(k), list):
                return len(section[k])
        return len(section)
    return 0


def get_meta(tenant_id: str, snapshot_id: str) -> dict[str, Any] | None:
    m = _read_index().get("snapshots", {}).get(snapshot_id)
    if m and m.get("tenant_id") == tenant_id:
        return m
    return None


def get_content(snapshot_id: str) -> dict[str, Any] | None:
    content = jsonstore.read_json(_blob_path(snapshot_id), None)
    return content if isinstance(content, dict) else None


def verify_sha(meta: dict[str, Any]) -> bool:
    """Re-hash the stored blob and compare to the recorded SHA (integrity proof)."""
    content = get_content(meta["id"])
    if content is None:
        return False
    return compute_sha(content) == meta.get("sha256")


def list_snapshots(
    tenant_id: str,
    *,
    workload_id: str | None = None,
    creator: str | None = None,
    tag: str | None = None,
    finding: str | None = None,
    retention_class: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _read_index().get("snapshots", {}).values():
        if m.get("tenant_id") != tenant_id:
            continue
        if not include_deleted and m.get("deleted_at"):
            continue
        if workload_id and (m.get("scope", {}).get("kind") != "workload" or m["scope"].get("id") != workload_id):
            continue
        if creator and m.get("created_by") != creator:
            continue
        if tag and tag not in (m.get("tags") or []):
            continue
        if finding and finding not in (m.get("finding_links") or []):
            continue
        if retention_class and m.get("retention_class") != retention_class:
            continue
        out.append(m)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def list_trashed(tenant_id: str) -> list[dict[str, Any]]:
    """Soft-deleted snapshots for the tenant, most-recently-trashed first."""
    out = [
        m for m in _read_index().get("snapshots", {}).values()
        if m.get("tenant_id") == tenant_id and m.get("deleted_at")
    ]
    out.sort(key=lambda m: m.get("deleted_at", ""), reverse=True)
    return out


def soft_delete(tenant_id: str, snapshot_id: str, *, actor: str = "") -> dict[str, Any] | None:
    """Move a snapshot to Trash (sets deleted_at; content blob + SHA are preserved)."""
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        meta = data.get("snapshots", {}).get(snapshot_id)
        if not meta or meta.get("tenant_id") != tenant_id:
            return
        meta["deleted_at"] = _now()
        meta["deleted_by"] = actor
        result.update(meta)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    return result or None


def restore(tenant_id: str, snapshot_id: str) -> dict[str, Any] | None:
    """Restore a trashed snapshot back to the locker."""
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        meta = data.get("snapshots", {}).get(snapshot_id)
        if not meta or meta.get("tenant_id") != tenant_id or not meta.get("deleted_at"):
            return
        meta.pop("deleted_at", None)
        meta.pop("deleted_by", None)
        result.update(meta)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    return result or None


def purge(tenant_id: str, snapshot_id: str) -> bool:
    """Permanently delete a snapshot (metadata + content blob). Tenant-scoped."""
    deleted = False

    def _mutate(data: dict[str, Any]) -> None:
        nonlocal deleted
        meta = data.get("snapshots", {}).get(snapshot_id)
        if meta and meta.get("tenant_id") == tenant_id:
            del data["snapshots"][snapshot_id]
            deleted = True

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    if deleted:
        try:
            _blob_path(snapshot_id).unlink(missing_ok=True)
        except OSError:
            pass
    return deleted


def empty_trash(tenant_id: str) -> int:
    """Permanently delete all trashed snapshots for the tenant. Returns the count."""
    removed: list[str] = []

    def _mutate(data: dict[str, Any]) -> None:
        for sid, meta in list(data.get("snapshots", {}).items()):
            if meta.get("tenant_id") == tenant_id and meta.get("deleted_at"):
                del data["snapshots"][sid]
                removed.append(sid)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    for sid in removed:
        try:
            _blob_path(sid).unlink(missing_ok=True)
        except OSError:
            pass
    return len(removed)



def add_attachment(tenant_id: str, snapshot_id: str, attachment: dict[str, Any]) -> dict[str, Any] | None:
    """Record a ticket/RCA attachment on the metadata (does NOT touch the content blob/SHA)."""
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        meta = data.get("snapshots", {}).get(snapshot_id)
        if not meta or meta.get("tenant_id") != tenant_id:
            return
        meta.setdefault("attachments", []).append({**attachment, "at": _now()})
        result.update(meta)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    return result or None


def add_share(tenant_id: str, snapshot_id: str, share: dict[str, Any]) -> dict[str, Any] | None:
    result: dict[str, Any] = {}

    def _mutate(data: dict[str, Any]) -> None:
        meta = data.get("snapshots", {}).get(snapshot_id)
        if not meta or meta.get("tenant_id") != tenant_id:
            return
        meta.setdefault("shares", []).append(share)
        result.update(meta)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    return result or None


def find_by_share_token(token: str) -> dict[str, Any] | None:
    for m in _read_index().get("snapshots", {}).values():
        for s in m.get("shares", []) or []:
            if s.get("token") == token:
                return m
    return None


def purge_expired(*, standard_days: int) -> int:
    """Remove non-audit-class snapshots older than ``standard_days``. Audit-class is never
    auto-purged here. Returns the number removed."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, standard_days))
    removed: list[str] = []

    def _mutate(data: dict[str, Any]) -> None:
        for sid, meta in list(data.get("snapshots", {}).items()):
            if meta.get("retention_class") == "audit":
                continue
            try:
                created = datetime.fromisoformat(meta.get("created_at", ""))
            except (ValueError, TypeError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                del data["snapshots"][sid]
                removed.append(sid)

    jsonstore.mutate_json(_INDEX, {"snapshots": {}}, _mutate)
    for sid in removed:
        try:
            _blob_path(sid).unlink(missing_ok=True)
        except OSError:
            pass
    return len(removed)
