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

_INDEX = Path(__file__).resolve().parents[2] / ".data" / "evidence_locker.json"
_BLOB_DIR = Path(__file__).resolve().parents[2] / ".data" / "evidence"

RETENTION_CLASSES = ("standard", "audit")
INCLUDE_KEYS = (
    "inventory", "properties", "changes", "metrics", "findings", "architecture", "memory", "activity",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_index() -> dict[str, Any]:
    if _INDEX.exists():
        try:
            data = json.loads(_INDEX.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"snapshots": {}}


def _write_index(data: dict[str, Any]) -> None:
    _INDEX.parent.mkdir(parents=True, exist_ok=True)
    _INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    _BLOB_DIR.mkdir(parents=True, exist_ok=True)
    # Write the content blob once.
    _blob_path(sid).write_text(_canonical(content), encoding="utf-8")
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
    data = _read_index()
    data.setdefault("snapshots", {})[sid] = meta
    _write_index(data)
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
    p = _blob_path(snapshot_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
    data = _read_index()
    m = data.get("snapshots", {}).get(snapshot_id)
    if not m or m.get("tenant_id") != tenant_id:
        return None
    m["deleted_at"] = _now()
    m["deleted_by"] = actor
    _write_index(data)
    return m


def restore(tenant_id: str, snapshot_id: str) -> dict[str, Any] | None:
    """Restore a trashed snapshot back to the locker."""
    data = _read_index()
    m = data.get("snapshots", {}).get(snapshot_id)
    if not m or m.get("tenant_id") != tenant_id or not m.get("deleted_at"):
        return None
    m.pop("deleted_at", None)
    m.pop("deleted_by", None)
    _write_index(data)
    return m


def purge(tenant_id: str, snapshot_id: str) -> bool:
    """Permanently delete a snapshot (metadata + content blob). Tenant-scoped."""
    data = _read_index()
    m = data.get("snapshots", {}).get(snapshot_id)
    if not m or m.get("tenant_id") != tenant_id:
        return False
    try:
        _blob_path(snapshot_id).unlink(missing_ok=True)
    except OSError:
        pass
    del data["snapshots"][snapshot_id]
    _write_index(data)
    return True


def empty_trash(tenant_id: str) -> int:
    """Permanently delete all trashed snapshots for the tenant. Returns the count."""
    data = _read_index()
    removed = 0
    for sid, m in list(data.get("snapshots", {}).items()):
        if m.get("tenant_id") == tenant_id and m.get("deleted_at"):
            try:
                _blob_path(sid).unlink(missing_ok=True)
            except OSError:
                pass
            del data["snapshots"][sid]
            removed += 1
    if removed:
        _write_index(data)
    return removed



def add_attachment(tenant_id: str, snapshot_id: str, attachment: dict[str, Any]) -> dict[str, Any] | None:
    """Record a ticket/RCA attachment on the metadata (does NOT touch the content blob/SHA)."""
    data = _read_index()
    m = data.get("snapshots", {}).get(snapshot_id)
    if not m or m.get("tenant_id") != tenant_id:
        return None
    m.setdefault("attachments", []).append({**attachment, "at": _now()})
    _write_index(data)
    return m


def add_share(tenant_id: str, snapshot_id: str, share: dict[str, Any]) -> dict[str, Any] | None:
    data = _read_index()
    m = data.get("snapshots", {}).get(snapshot_id)
    if not m or m.get("tenant_id") != tenant_id:
        return None
    m.setdefault("shares", []).append(share)
    _write_index(data)
    return m


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
    data = _read_index()
    removed = 0
    for sid, m in list(data.get("snapshots", {}).items()):
        if m.get("retention_class") == "audit":
            continue
        try:
            created = datetime.fromisoformat(m.get("created_at", ""))
        except (ValueError, TypeError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            try:
                _blob_path(sid).unlink(missing_ok=True)
            except OSError:
                pass
            del data["snapshots"][sid]
            removed += 1
    if removed:
        _write_index(data)
    return removed
