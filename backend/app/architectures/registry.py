"""Architectures registry (JSON).

An *architecture* is a saved diagram of an application: nodes (resources), edges
(relationships), and groups (containers). It may be authored manually or reverse-
engineered from a workload by AI. Persisted under backend/.data/architectures.json,
consistent with the workbooks/playbooks registries. No secrets, so no encryption.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architectures.json"

# Lifecycle states (fixed workflow). draft -> in_review -> ready; archived is terminal
# but restorable. AI-generated and blank architectures both start as 'draft'.
VALID_STATES = ("draft", "in_review", "ready", "archived")

DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "workload_id": "",
    "workload_name": "",
    "connection_id": "",
    "tenant_id": "",
    "source": "manual",  # manual | ai
    "state": "draft",  # draft | in_review | ready | archived
    "category_id": "",  # solution/collection id (empty = Uncategorized)
    "nodes": [],   # [{id, arm_id, name, type, category, layer, resource_group, subscription_id, location, sku, meta, group_id, x, y}]
    "edges": [],   # [{id, source, target, label, kind, dashed}]
    "groups": [],  # [{id, name, kind, color, x, y, w, h}]
    "ai": {},      # {model, generated_at, rationale, confidence, resource_count}
    "state_changed_by": "",
    "state_changed_at": "",
    "deleted_at": "",  # soft-delete marker (Trash); empty = active
    "created_by": "",
    "updated_by": "",
    "created_at": "",
    "updated_at": "",
}

# Fields copied wholesale on upsert (everything except server-managed timestamps/id).
_FIELDS = [k for k in DEFAULTS if k not in ("created_at", "updated_at")]


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
    return {"architectures": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(aid: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = aid
    return merged


def list_architectures(tenant_id: str | None = None, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    data = _read()
    out = [_merge(aid, a) for aid, a in data.get("architectures", {}).items()]
    # Hide trashed (soft-deleted) architectures from the active list — and thus from every
    # consumer (canvas, memory index, evidence collector, …) — unless explicitly asked.
    if not include_deleted:
        out = [a for a in out if not a.get("deleted_at")]
    if tenant_id is not None:
        out = [a for a in out if (a.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda a: a.get("updated_at", ""), reverse=True)
    return out


def list_trashed_architectures(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Architectures currently in the Trash (soft-deleted), most-recently-deleted first."""
    out = [a for a in list_architectures(tenant_id, include_deleted=True) if a.get("deleted_at")]
    out.sort(key=lambda a: a.get("deleted_at", ""), reverse=True)
    return out


def get_architecture(architecture_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None:
        return None
    if not include_deleted and raw.get("deleted_at"):
        return None
    return _merge(architecture_id, raw)


def upsert_architecture(
    arch: dict[str, Any], *, actor: str = "", reason: str = "Edited", skip_activity: bool = False
) -> dict[str, Any]:
    data = _read()
    archs = data.setdefault("architectures", {})
    aid = arch.get("id") or str(uuid.uuid4())
    existing = archs.get(aid, {})
    merged = dict(existing)
    for key in _FIELDS:
        if key in arch and arch[key] is not None:
            merged[key] = arch[key]
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    # Record who last modified it (and who created it on first write).
    if actor:
        merged["updated_by"] = actor
        if not existing:
            merged.setdefault("created_by", actor)
    merged.pop("id", None)
    archs[aid] = merged
    _write(data)
    result = get_architecture(aid)
    assert result is not None
    # Auto-snapshot a revision of the new version (deduped by content signature).
    from app.architectures import revisions

    snapshot_reason = "Created" if not existing else reason
    revisions.snapshot(aid, result, reason=snapshot_reason, actor=actor)
    if not skip_activity:
        _log_upsert_activity(aid, existing, result, reason=reason, actor=actor)
    return result


def _diagram_signature(rec: dict[str, Any]) -> str:
    """Fingerprint of just the visual diagram (nodes/edges/groups) for change detection."""
    return json.dumps(
        {"nodes": rec.get("nodes", []), "edges": rec.get("edges", []), "groups": rec.get("groups", [])},
        sort_keys=True,
    )


def _log_upsert_activity(
    aid: str, existing: dict[str, Any], result: dict[str, Any], *, reason: str, actor: str
) -> None:
    """Translate an upsert into management-activity events (created/renamed/edited/AI)."""
    from app.architectures import activity

    if not existing:
        if reason == "Generated by AI":
            wl = result.get("workload_name") or ""
            detail = f"Generated by AI{f' from workload “{wl}”' if wl else ''}"
            activity.log(aid, activity.AI_GENERATED, detail, actor)
        else:
            activity.log(aid, activity.CREATED, "Created architecture", actor)
        return
    old_name = existing.get("name", "")
    new_name = result.get("name", "")
    if old_name != new_name:
        activity.log(
            aid, activity.RENAMED, f"Renamed from “{old_name}” to “{new_name}”",
            actor, meta={"from": old_name, "to": new_name},
        )
    if reason == "AI enhanced":
        activity.log(aid, activity.AI_ENHANCED, "Refined the diagram with AI", actor)
    elif _diagram_signature(existing) != _diagram_signature(result):
        n, e = len(result.get("nodes", []) or []), len(result.get("edges", []) or [])
        activity.log(aid, activity.EDITED, f"Edited the diagram ({n} resources, {e} links)", actor)


def delete_architecture(architecture_id: str, *, actor: str = "") -> bool:
    """Soft-delete: move the architecture to the Trash (set ``deleted_at``). It's hidden
    everywhere but fully restorable — revisions and activity are PRESERVED — until purged.
    Returns False if not found or already trashed."""
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None or raw.get("deleted_at"):
        return False
    raw["deleted_at"] = _now()
    raw["updated_at"] = _now()
    if actor:
        raw["updated_by"] = actor
    _write(data)
    from app.architectures import activity

    activity.log(architecture_id, activity.TRASHED, "Moved to Trash", actor)
    return True


def restore_architecture(architecture_id: str, *, actor: str = "") -> dict[str, Any] | None:
    """Restore a trashed architecture back into the active list. Returns None if not found
    or not currently trashed."""
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None or not raw.get("deleted_at"):
        return None
    raw["deleted_at"] = ""
    raw["updated_at"] = _now()
    if actor:
        raw["updated_by"] = actor
    _write(data)
    from app.architectures import activity

    activity.log(architecture_id, activity.RESTORED, "Restored from Trash", actor)
    return get_architecture(architecture_id)


def purge_architecture(architecture_id: str) -> bool:
    """Permanently delete an architecture (hard delete) along with its revisions and
    activity log. Works regardless of trash state. Returns False if not found."""
    data = _read()
    if architecture_id in data.get("architectures", {}):
        del data["architectures"][architecture_id]
        _write(data)
        from app.architectures import activity, revisions

        revisions.delete_for(architecture_id)
        activity.delete_for(architecture_id)
        return True
    return False


def empty_architecture_trash(tenant_id: str | None = None) -> int:
    """Permanently delete every trashed architecture (optionally tenant-scoped). Returns
    the count removed."""
    trashed = list_trashed_architectures(tenant_id)
    n = 0
    for a in trashed:
        if purge_architecture(a["id"]):
            n += 1
    return n


def set_state(architecture_id: str, state: str, actor: str) -> dict[str, Any] | None:
    """Update only the lifecycle state (read-modify-write; never touches the diagram)."""
    if state not in VALID_STATES:
        raise ValueError(f"Invalid state '{state}'.")
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None:
        return None
    old_state = raw.get("state", "draft")
    raw["state"] = state
    raw["state_changed_by"] = actor
    raw["state_changed_at"] = _now()
    raw["updated_by"] = actor
    raw["updated_at"] = _now()
    _write(data)
    result = get_architecture(architecture_id)
    from app.architectures import activity, revisions

    if result is not None:
        revisions.snapshot(architecture_id, result, reason=f"State \u2192 {state.replace('_', ' ')}", actor=actor)
        if old_state != state:
            activity.log(
                architecture_id, activity.STATE_CHANGED,
                f"Status changed from {_STATE_LABEL.get(old_state, old_state)} to {_STATE_LABEL.get(state, state)}",
                actor, meta={"from": old_state, "to": state},
            )
    return result


def set_category(architecture_id: str, category_id: str, actor: str = "") -> dict[str, Any] | None:
    """Assign the architecture to a collection/solution (empty = Uncategorized)."""
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None:
        return None
    old_category = raw.get("category_id", "")
    raw["category_id"] = category_id or ""
    if actor:
        raw["updated_by"] = actor
    raw["updated_at"] = _now()
    _write(data)
    result = get_architecture(architecture_id)
    from app.architectures import activity, revisions

    if result is not None:
        revisions.snapshot(architecture_id, result, reason="Category changed", actor=actor)
        if (old_category or "") != (category_id or ""):
            activity.log(
                architecture_id, activity.CATEGORY_CHANGED,
                f"Moved from {_category_name(old_category)} to {_category_name(category_id)}",
                actor, meta={"from": old_category or "", "to": category_id or ""},
            )
    return result


def set_workload(
    architecture_id: str, workload_id: str, workload_name: str = "", actor: str = ""
) -> dict[str, Any] | None:
    """Link the architecture to a workload (empty = unlinked). Never touches the diagram."""
    data = _read()
    raw = data.get("architectures", {}).get(architecture_id)
    if raw is None:
        return None
    old_id = raw.get("workload_id", "")
    old_name = raw.get("workload_name", "")
    raw["workload_id"] = workload_id or ""
    raw["workload_name"] = workload_name or ""
    if actor:
        raw["updated_by"] = actor
    raw["updated_at"] = _now()
    _write(data)
    result = get_architecture(architecture_id)
    from app.architectures import activity, revisions

    if result is not None and (old_id or "") != (workload_id or ""):
        revisions.snapshot(architecture_id, result, reason="Workload link changed", actor=actor)
        if workload_id:
            detail = f"Linked to workload \u201c{workload_name or workload_id}\u201d"
        else:
            detail = (
                f"Unlinked from workload \u201c{old_name or old_id}\u201d" if old_id else "Unlinked from workload"
            )
        activity.log(
            architecture_id, activity.WORKLOAD_CHANGED, detail,
            actor, meta={"from": old_id or "", "to": workload_id or ""},
        )
    return result


_STATE_LABEL = {"draft": "Draft", "in_review": "In Review", "ready": "Ready", "archived": "Archived"}


def _category_name(category_id: str | None) -> str:
    """Human label for a collection id (“Uncategorized” when empty/unknown)."""
    if not category_id:
        return "Uncategorized"
    try:
        from app.architectures import collections

        coll = collections.get_collection(category_id)
        if coll:
            return f"“{coll.get('name', category_id)}”"
    except Exception:  # noqa: BLE001
        pass
    return "a category"


def clear_category(category_id: str, actor: str = "") -> int:
    """Reassign every architecture in a category back to Uncategorized. Returns count.

    Records an activity event + a revision snapshot per affected architecture (consistent
    with ``set_category``) so the move out of a deleted collection isn't a silent history gap.
    """
    if not category_id:
        return 0
    data = _read()
    affected: list[str] = []
    for aid, raw in data.get("architectures", {}).items():
        if (raw.get("category_id") or "") == category_id:
            raw["category_id"] = ""
            raw["updated_at"] = _now()
            if actor:
                raw["updated_by"] = actor
            affected.append(aid)
    if affected:
        _write(data)
        from app.architectures import activity, revisions

        old_name = _category_name(category_id)
        for aid in affected:
            result = get_architecture(aid)
            if result is None:
                continue
            revisions.snapshot(aid, result, reason="Category changed", actor=actor)
            activity.log(
                aid, activity.CATEGORY_CHANGED,
                f"Moved from {old_name} to Uncategorized (collection deleted)",
                actor, meta={"from": category_id, "to": ""},
            )
    return len(affected)


_CLONE_KEYS = (
    "description", "workload_id", "workload_name", "connection_id",
    "source", "category_id", "nodes", "edges", "groups", "ai",
)


def clone_architecture(architecture_id: str, *, actor: str, tenant_id: str = "") -> dict[str, Any] | None:
    """Duplicate an architecture into a fresh Draft copy (own revision history)."""
    src = get_architecture(architecture_id)
    if src is None:
        return None
    payload: dict[str, Any] = {k: src.get(k) for k in _CLONE_KEYS}
    payload["name"] = f"{src.get('name', 'Architecture')} (copy)"
    payload["state"] = "draft"  # a clone starts as a new draft
    payload["tenant_id"] = tenant_id or src.get("tenant_id", "")
    payload["created_by"] = actor
    cloned = upsert_architecture(payload, actor=actor, reason="Cloned", skip_activity=True)
    from app.architectures import activity

    activity.log(
        cloned["id"], activity.CLONED, f"Cloned from “{src.get('name', 'architecture')}”",
        actor, meta={"source_id": architecture_id, "source_name": src.get("name", "")},
    )
    # Also note on the source that a copy was made (useful provenance trail).
    activity.log(
        architecture_id, activity.CLONED_TO, f"Cloned to “{cloned.get('name', '')}”",
        actor, meta={"clone_id": cloned["id"], "clone_name": cloned.get("name", "")},
    )
    return cloned


def restore_revision(architecture_id: str, revision_id: str, actor: str) -> dict[str, Any] | None:
    """Restore a past revision's content onto the live architecture. The pre-restore
    version is itself snapshotted (via the upsert auto-snapshot) so nothing is lost."""
    from app.architectures import activity, revisions

    if get_architecture(architecture_id) is None:
        return None
    rev = revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        return None
    payload = {
        "id": architecture_id,
        "name": rev.get("name", ""),
        "description": rev.get("description", ""),
        "source": rev.get("source", "manual"),
        "state": rev.get("state", "draft"),
        "category_id": rev.get("category_id", ""),
        "nodes": rev.get("nodes", []),
        "edges": rev.get("edges", []),
        "groups": rev.get("groups", []),
        "ai": rev.get("ai", {}),
    }
    result = upsert_architecture(payload, actor=actor, reason="Restored from history", skip_activity=True)
    rev_when = rev.get("created_at", "")
    activity.log(
        architecture_id, activity.RESTORED,
        f"Restored a previous version ({rev.get('reason', 'edit')})",
        actor, meta={"revision_id": revision_id, "revision_at": rev_when},
    )
    return result
