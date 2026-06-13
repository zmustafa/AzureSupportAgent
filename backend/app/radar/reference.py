"""Versioned, admin-editable Radar reference registry.

Persisted at backend/.data/radar_reference.json on the Azure Files volume, with a bounded
revision history. Seeded from builtin_seed on first load. Holds the event classification
rules + the Azure OpenAI/Foundry model-lifecycle table. Sibling of the coverage-detector
reference files (identical machinery)."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.radar.builtin_seed import (
    BREAKING_CHANGE,
    BUILTIN_SEED_VERSION,
    RETIREMENT,
    builtin_reference,
)

_PATH = Path(__file__).resolve().parents[2] / ".data" / "radar_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "radar_reference_revisions.json"

_MAX_REVISIONS = 50
_STAGES = {"preview", "ga", "deprecated", "retired"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any] | None:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("classification_rules"), list):
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


def _sanitize_rules(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        kws = r.get("keywords")
        keywords = [str(k).strip().lower() for k in kws if str(k).strip()] if isinstance(kws, list) else []
        ct = str(r.get("change_type", RETIREMENT)).strip().lower()
        if ct not in (RETIREMENT, BREAKING_CHANGE):
            ct = RETIREMENT
        out.append(
            {
                "id": str(r.get("id", "") or "")[:80],
                "keywords": keywords[:20],
                "change_type": ct,
                "service": str(r.get("service", "") or "")[:120],
                "replacement": str(r.get("replacement", "") or "")[:500],
                "migration_url": str(r.get("migration_url", "") or "")[:500],
                "planned_date": str(r.get("planned_date", "") or "")[:32],
            }
        )
    return out


def _sanitize_models(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for m in raw:
        if not isinstance(m, dict):
            continue
        model = str(m.get("model", "") or "").strip()
        if not model:
            continue
        stage = str(m.get("stage", "ga")).strip().lower()
        if stage not in _STAGES:
            stage = "ga"
        out.append(
            {
                "model": model[:120],
                "version": str(m.get("version", "") or "")[:60],
                "stage": stage,
                "ga_date": str(m.get("ga_date", "") or "")[:32],
                "deprecation_date": str(m.get("deprecation_date", "") or "")[:32],
                "retirement_date": str(m.get("retirement_date", "") or "")[:32],
                "replacement": str(m.get("replacement", "") or "")[:500],
            }
        )
    return out


def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rev["id"],
        "version": rev.get("version", 0),
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "rule_count": len(rev.get("classification_rules", []) or []),
        "model_count": len(rev.get("model_lifecycle", []) or []),
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
            "classification_rules": copy.deepcopy(doc.get("classification_rules", [])),
            "model_lifecycle": copy.deepcopy(doc.get("model_lifecycle", [])),
            "builtin_seed_version": doc.get("builtin_seed_version", BUILTIN_SEED_VERSION),
        }
    )
    if len(revs) > _MAX_REVISIONS:
        del revs[: len(revs) - _MAX_REVISIONS]
    _write_revs(data)


def save_reference(
    *, classification_rules: Any, model_lifecycle: Any, actor: str, reason: str = "Edited"
) -> dict[str, Any]:
    current = load_reference()
    doc = {
        "version": int(current.get("version", 0)) + 1,
        "updated_at": _now(),
        "updated_by": actor or "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "classification_rules": _sanitize_rules(classification_rules),
        "model_lifecycle": _sanitize_models(model_lifecycle),
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
    return save_reference(
        classification_rules=rev.get("classification_rules", []),
        model_lifecycle=rev.get("model_lifecycle", []),
        actor=actor,
        reason=f"Restored revision {rev.get('version')}",
    )


def reset_to_builtin(*, actor: str) -> dict[str, Any]:
    seed = builtin_reference()
    return save_reference(
        classification_rules=seed.get("classification_rules", []),
        model_lifecycle=seed.get("model_lifecycle", []),
        actor=actor,
        reason="Reset to built-in seed",
    )
