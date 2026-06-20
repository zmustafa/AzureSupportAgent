"""Versioned, admin-editable AMBA reference set registry.

Persisted at backend/.data/amba_reference.json on the Azure Files volume (survives
deploys/restarts), consistent with the other JSON registries. Seeded from
builtin_seed.BUILTIN_TYPES on first load. Every save bumps ``version`` and appends a
snapshot to a bounded revision history so an admin can review and restore earlier
versions, or reset back to the built-in seed — all without a redeploy.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.amba.builtin_seed import BUILTIN_SEED_VERSION, builtin_reference

_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_reference_revisions.json"

_MAX_REVISIONS = 50
_AMBA_CATEGORIES = ("availability", "performance", "security")
_SEVERITIES = ("critical", "error", "warning", "info")
_OPERATORS = ("GreaterThan", "LessThan", "GreaterOrLessThan", "Equals")


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
    """Return the active reference document, seeding it from the built-in set on first use."""
    doc = _read()
    if doc is None:
        doc = builtin_reference()
        _write(doc)
        return doc
    # Additive upgrade: when the built-in seed version advances, merge in any NEW built-in
    # types and any NEW alert keys into existing types. This is purely additive — it never
    # overwrites or removes a user's edits — so newly-shipped recommended alerts (e.g. the
    # Key Vault status-code metrics) appear without resetting customizations.
    if int(doc.get("builtin_seed_version", 0) or 0) < BUILTIN_SEED_VERSION:
        builtin = builtin_reference()
        types = doc.setdefault("types", {})
        changed = False
        for t, spec in builtin.get("types", {}).items():
            if t not in types:
                types[t] = copy.deepcopy(spec)
                changed = True
                continue
            existing = types[t].setdefault("alerts", [])
            have = {a.get("key") for a in existing}
            for a in spec.get("alerts", []) or []:
                if a.get("key") not in have:
                    existing.append(copy.deepcopy(a))
                    changed = True
        doc["builtin_seed_version"] = BUILTIN_SEED_VERSION
        if changed:
            _write(doc)
    return doc


def _sanitize_alert(raw: dict[str, Any]) -> dict[str, Any] | None:
    key = str(raw.get("key", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not key or not name:
        return None
    cat = raw.get("amba_category")
    sev = raw.get("severity")
    op = raw.get("operator")
    threshold = raw.get("threshold")
    try:
        threshold = float(threshold) if threshold is not None and threshold != "" else None
    except (TypeError, ValueError):
        threshold = None
    return {
        "key": key[:64],
        "name": name[:160],
        "amba_category": cat if cat in _AMBA_CATEGORIES else "availability",
        "signal": "log" if str(raw.get("signal")) == "log" else "metric",
        "metric": str(raw.get("metric", "") or "")[:200],
        "operator": op if op in _OPERATORS else "GreaterThan",
        "threshold": threshold,
        "unit": str(raw.get("unit", "") or "")[:16],
        "window": str(raw.get("window", "PT5M") or "PT5M")[:16],
        "severity": sev if sev in _SEVERITIES else "warning",
        "requires_action_group": bool(raw.get("requires_action_group", True)),
        "dimension_filter": str(raw.get("dimension_filter", "") or "")[:200],
        "why": str(raw.get("why", "") or "")[:600],
    }


def _sanitize_types(raw_types: Any) -> dict[str, Any]:
    """Validate + normalize a submitted types map; drop malformed entries."""
    out: dict[str, Any] = {}
    if not isinstance(raw_types, dict):
        return out
    for arm_type, spec in raw_types.items():
        t = str(arm_type).strip().lower()
        if not t or not isinstance(spec, dict):
            continue
        alerts_in = spec.get("alerts")
        alerts: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        if isinstance(alerts_in, list):
            for a in alerts_in:
                if not isinstance(a, dict):
                    continue
                clean = _sanitize_alert(a)
                if clean and clean["key"] not in seen_keys:
                    seen_keys.add(clean["key"])
                    alerts.append(clean)
        out[t] = {
            "display": str(spec.get("display", arm_type) or arm_type)[:120],
            "category": str(spec.get("category", "other") or "other")[:40],
            "alerts": alerts,
        }
    return out


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    types = rev.get("types", {}) or {}
    alert_count = sum(len(t.get("alerts", []) or []) for t in types.values())
    return {
        "id": rev["id"],
        "version": rev.get("version", 0),
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "type_count": len(types),
        "alert_count": alert_count,
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
    """Replace the reference's type→alerts map, bump the version, snapshot the result."""
    current = load_reference()
    new_types = _sanitize_types(types)
    doc = {
        "version": int(current.get("version", 0)) + 1,
        "updated_at": _now(),
        "updated_by": actor or "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "types": new_types,
    }
    _write(doc)
    _snapshot(doc, reason=reason, actor=actor)
    return doc


def list_revisions() -> list[dict[str, Any]]:
    """Revision metadata, newest first."""
    revs = _read_revs().get("revisions", [])
    return [_meta(r) for r in reversed(revs)]


def get_revision(revision_id: str) -> dict[str, Any] | None:
    for r in _read_revs().get("revisions", []):
        if r.get("id") == revision_id:
            return r
    return None


def restore_revision(revision_id: str, *, actor: str) -> dict[str, Any] | None:
    """Restore a prior revision's types as a NEW version (non-destructive)."""
    rev = get_revision(revision_id)
    if rev is None:
        return None
    return save_reference(rev.get("types", {}), actor=actor, reason=f"Restored revision {rev.get('version')}")


def reset_to_builtin(*, actor: str) -> dict[str, Any]:
    """Reset the reference back to the built-in seed as a NEW version."""
    seed = builtin_reference()
    return save_reference(seed.get("types", {}), actor=actor, reason="Reset to built-in seed")


def reference_for_type(arm_type: str) -> dict[str, Any] | None:
    """Return the reference spec for an ARM type (lowercased lookup), or None."""
    return load_reference().get("types", {}).get((arm_type or "").lower())
