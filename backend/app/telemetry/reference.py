"""Versioned, admin-editable telemetry reference set (recommended diagnostic categories).

Persisted at backend/.data/telemetry_reference.json on the Azure Files volume, with a
bounded revision history (telemetry_reference_revisions.json). Seeded from
builtin_seed.BUILTIN_TYPES on first load. Maintained independently of the AMBA reference
(sibling file, identical machinery) so the two can evolve separately."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.telemetry.builtin_seed import BUILTIN_SEED_VERSION, builtin_reference

_PATH = Path(__file__).resolve().parents[2] / ".data" / "telemetry_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "telemetry_reference_revisions.json"

_MAX_REVISIONS = 50
_KINDS = ("log", "metric")
_GROUPS = ("audit", "security", "operational", "performance")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any] | None:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("types"), dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _write(doc: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _read_revs() -> dict[str, Any]:
    if _REV_PATH.exists():
        try:
            data = json.loads(_REV_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"revisions": []}


def _write_revs(data: dict[str, Any]) -> None:
    _REV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REV_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_reference() -> dict[str, Any]:
    doc = _read()
    if doc is None:
        doc = builtin_reference()
        _write(doc)
    return doc


def _sanitize_category(raw: dict[str, Any]) -> dict[str, Any] | None:
    key = str(raw.get("key", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not key:
        return None
    kind = raw.get("kind")
    group = raw.get("group")
    return {
        "key": key[:80],
        "name": name[:120] or key,
        "kind": kind if kind in _KINDS else "log",
        "group": group if group in _GROUPS else "operational",
        "recommended": bool(raw.get("recommended", True)),
        "why": str(raw.get("why", "") or "")[:600],
    }


def _sanitize_types(raw_types: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw_types, dict):
        return out
    for arm_type, spec in raw_types.items():
        t = str(arm_type).strip().lower()
        if not t or not isinstance(spec, dict):
            continue
        cats_in = spec.get("categories")
        cats: list[dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(cats_in, list):
            for c in cats_in:
                if not isinstance(c, dict):
                    continue
                clean = _sanitize_category(c)
                if clean and clean["key"] not in seen:
                    seen.add(clean["key"])
                    cats.append(clean)
        out[t] = {
            "display": str(spec.get("display", arm_type) or arm_type)[:120],
            "note": str(spec.get("note", "") or "")[:300],
            "categories": cats,
        }
    return out


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    types = rev.get("types", {}) or {}
    cat_count = sum(len(t.get("categories", []) or []) for t in types.values())
    return {
        "id": rev["id"],
        "version": rev.get("version", 0),
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "type_count": len(types),
        "category_count": cat_count,
    }


def _snapshot(doc: dict[str, Any], *, reason: str, actor: str) -> None:
    data = _read_revs()
    revs = data.setdefault("revisions", [])
    revs.append(
        {
            "id": str(uuid.uuid4()),
            "version": doc.get("version", 0),
            "created_at": _now(),
            "by": actor or "",
            "reason": reason or "Edited",
            "types": copy.deepcopy(doc.get("types", {})),
            "builtin_seed_version": doc.get("builtin_seed_version", BUILTIN_SEED_VERSION),
        }
    )
    if len(revs) > _MAX_REVISIONS:
        del revs[: len(revs) - _MAX_REVISIONS]
    _write_revs(data)


def save_reference(types: Any, *, actor: str, reason: str = "Edited") -> dict[str, Any]:
    current = load_reference()
    doc = {
        "version": int(current.get("version", 0)) + 1,
        "updated_at": _now(),
        "updated_by": actor or "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "types": _sanitize_types(types),
    }
    _write(doc)
    _snapshot(doc, reason=reason, actor=actor)
    return doc


def list_revisions() -> list[dict[str, Any]]:
    revs = _read_revs().get("revisions", [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(revision_id: str) -> dict[str, Any] | None:
    for r in _read_revs().get("revisions", []):
        if r.get("id") == revision_id:
            return r
    return None


def restore_revision(revision_id: str, *, actor: str) -> dict[str, Any] | None:
    rev = get_revision(revision_id)
    if rev is None:
        return None
    return save_reference(rev.get("types", {}), actor=actor, reason=f"Restored revision {rev.get('version')}")


def reset_to_builtin(*, actor: str) -> dict[str, Any]:
    seed = builtin_reference()
    return save_reference(seed.get("types", {}), actor=actor, reason="Reset to built-in seed")


def reference_for_type(arm_type: str) -> dict[str, Any] | None:
    return load_reference().get("types", {}).get((arm_type or "").lower())
