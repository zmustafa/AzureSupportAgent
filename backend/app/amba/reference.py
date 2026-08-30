"""Versioned, admin-editable AMBA reference set registry.

Persisted at backend/.data/amba_reference.json on the Azure Files volume (survives
deploys/restarts), consistent with the other JSON registries. Seeded from
builtin_seed.BUILTIN_TYPES on first load. Every save bumps ``version`` and appends a
snapshot to a bounded revision history so an admin can review and restore earlier
versions, or reset back to the built-in seed — all without a redeploy.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.amba.builtin_seed import (
    ALERT_TYPES,
    AMBA_CATEGORIES,
    BUILTIN_SEED_VERSION,
    OPERATORS,
    PATTERNS,
    SEVERITIES,
    TIERS,
    builtin_reference,
)
from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "amba_reference_revisions.json"

_MAX_REVISIONS = 50
_SEV_LABEL = {0: "critical", 1: "error", 2: "warning", 3: "info", 4: "info"}
_SEV_NUM = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any] | None:
    data = jsonstore.read_json(_PATH, None)
    if isinstance(data, dict) and isinstance(data.get("types"), dict):
        return data
    return None


def _read_revs() -> dict[str, Any]:
    data = jsonstore.read_json(_REV_PATH, {"revisions": []})
    return data if isinstance(data, dict) else {"revisions": []}


def load_reference() -> dict[str, Any]:
    """Return the active reference document, seeding it from the built-in set on first use.

    Seed v9 replaced the hand-curated 37-type seed with the full upstream AMBA catalog and
    a substantially wider alert schema (numeric severity, criterion type, separate evaluation
    frequency, activity-log/log-search facts, tiers, patterns). Merging the two shapes would
    produce half-populated entries, so crossing that boundary performs a clean reset — the
    prior document is snapshotted into the revision history first, so nothing is lost and an
    admin can diff or restore it from the Reference Set screen.
    """
    doc = _read()
    if doc is not None and int(doc.get("builtin_seed_version", 0) or 0) >= BUILTIN_SEED_VERSION:
        return doc

    def _mutate(stored: Any) -> dict[str, Any]:
        if not isinstance(stored, dict) or not isinstance(stored.get("types"), dict):
            return builtin_reference()
        version = int(stored.get("builtin_seed_version", 0) or 0)
        if version < _SCHEMA_RESET_VERSION:
            _snapshot(
                stored,
                reason=(
                    f"Auto-archived before upgrading to built-in seed v{BUILTIN_SEED_VERSION} "
                    f"(AMBA {builtin_reference().get('amba_release', '')})"
                ),
                actor="system",
            )
            fresh = builtin_reference()
            fresh["version"] = int(stored.get("version", 0)) + 1
            fresh["updated_at"] = _now()
            fresh["updated_by"] = "system"
            return fresh
        if version < BUILTIN_SEED_VERSION:
            builtin = builtin_reference()
            types = stored.setdefault("types", {})
            for arm_type, spec in builtin.get("types", {}).items():
                if arm_type not in types:
                    types[arm_type] = copy.deepcopy(spec)
                    continue
                existing = types[arm_type].setdefault("alerts", [])
                have = {alert.get("key") for alert in existing}
                for alert in spec.get("alerts", []) or []:
                    if alert.get("key") not in have:
                        existing.append(copy.deepcopy(alert))
            stored["builtin_seed_version"] = BUILTIN_SEED_VERSION
            stored["amba_release"] = builtin.get(
                "amba_release", stored.get("amba_release", "")
            )
        return stored

    return jsonstore.mutate_json(_PATH, None, _mutate)


# Crossing this built-in seed version rebuilds the reference from scratch because the alert
# schema itself changed shape.
_SCHEMA_RESET_VERSION = 9


def _clean_dimensions(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for dim in raw[:8]:
        if not isinstance(dim, dict) or not dim.get("name"):
            continue
        out.append(
            {
                "name": str(dim["name"])[:64],
                "operator": "Exclude" if str(dim.get("operator")) == "Exclude" else "Include",
                "values": [str(v)[:64] for v in (dim.get("values") or [])[:16]],
            }
        )
    return out


def _clean_links(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")[:400]
        if url.startswith(("http://", "https://")):
            out.append({"name": str(item.get("name") or "")[:160], "url": url})
    return out


def _sanitize_alert(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate one admin-submitted alert definition against the extended AMBA schema."""
    key = str(raw.get("key", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not key or not name:
        return None

    threshold = raw.get("threshold")
    try:
        threshold = float(threshold) if threshold is not None and threshold != "" else None
    except (TypeError, ValueError):
        threshold = None

    severity = raw.get("severity")
    severity_num = raw.get("severity_num")
    if not isinstance(severity_num, int) or not 0 <= severity_num <= 4:
        severity_num = _SEV_NUM.get(str(severity), 2)
    if severity not in SEVERITIES:
        severity = _SEV_LABEL.get(severity_num, "info")

    alert_type = str(raw.get("alert_type") or "metric")
    criterion = str(raw.get("criterion_type") or "")
    if criterion not in ("StaticThresholdCriterion", "DynamicThresholdCriterion", ""):
        criterion = ""

    sensitivity = raw.get("alert_sensitivity")
    if sensitivity not in ("Low", "Medium", "High", None):
        sensitivity = None

    failing = raw.get("failing_periods")
    if isinstance(failing, dict):
        def _int_or_none(value: Any) -> int | None:
            try:
                return max(1, min(24, int(value)))
            except (TypeError, ValueError):
                return None

        failing = {
            "number_of_evaluation_periods": _int_or_none(failing.get("number_of_evaluation_periods")),
            "min_failing_periods_to_alert": _int_or_none(failing.get("min_failing_periods_to_alert")),
        }
    else:
        failing = None

    activity = raw.get("activity_log")
    activity = {str(k)[:64]: v for k, v in list(activity.items())[:12]} if isinstance(activity, dict) else {}

    window_size = str(raw.get("window_size") or raw.get("window") or "PT5M")[:16]
    return {
        "key": key[:64],
        "guid": str(raw.get("guid", "") or "")[:64],
        "name": name[:160],
        "description": str(raw.get("description", "") or "")[:1000],
        "why": str(raw.get("why", "") or "")[:2000],
        "alert_type": alert_type if alert_type in ALERT_TYPES else "metric",
        "amba_category": raw.get("amba_category") if raw.get("amba_category") in AMBA_CATEGORIES else "availability",
        "severity": severity,
        "severity_num": severity_num,
        "tier": raw.get("tier") if raw.get("tier") in TIERS else "recommended",
        "patterns": [p for p in (raw.get("patterns") or []) if p in PATTERNS][:8],
        "metric": str(raw.get("metric", "") or "")[:200],
        "metric_namespace": str(raw.get("metric_namespace", "") or "")[:200],
        "counter_name": str(raw.get("counter_name", "") or "")[:200],
        "operator": raw.get("operator") if raw.get("operator") in OPERATORS else "GreaterThan",
        "threshold": threshold,
        "unit": str(raw.get("unit", "") or "")[:16],
        "criterion_type": criterion,
        "alert_sensitivity": sensitivity,
        "failing_periods": failing,
        "auto_mitigate": None if raw.get("auto_mitigate") is None else bool(raw.get("auto_mitigate")),
        "time_aggregation": str(raw.get("time_aggregation") or raw.get("aggregation") or "")[:16],
        "window_size": window_size,
        "evaluation_frequency": str(raw.get("evaluation_frequency") or window_size)[:16],
        "dimensions": _clean_dimensions(raw.get("dimensions")),
        "dimension_filter": str(raw.get("dimension_filter", "") or "")[:200],
        "activity_log": activity,
        "log_query": str(raw.get("log_query", "") or "")[:8000],
        "visible": bool(raw.get("visible", True)),
        "verified": bool(raw.get("verified", False)),
        "default_enabled": bool(raw.get("default_enabled", True)),
        "requires_action_group": bool(raw.get("requires_action_group", True)),
        "deployable": bool(raw.get("deployable", True)),
        "references": _clean_links(raw.get("references")),
        "deployments": raw.get("deployments") if isinstance(raw.get("deployments"), list) else [],
        "policy_alert_name": str(raw.get("policy_alert_name", "") or "")[:160],
        "policy_scope": str(raw.get("policy_scope", "") or "")[:32],
        "threshold_override_tag": str(raw.get("threshold_override_tag", "") or "")[:160],
        "amba_tags": [str(t)[:40] for t in (raw.get("amba_tags") or [])][:16],
        "source": "local" if str(raw.get("source")) == "local" else "amba",
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
            "source": "local" if str(spec.get("source")) == "local" else "amba",
            "provider": str(spec.get("provider", "") or "")[:60],
            "service": str(spec.get("service", "") or "")[:80],
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
    revision = {
        "id": str(uuid.uuid4()),
        "version": doc.get("version", 0),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "types": copy.deepcopy(doc.get("types", {})),
        "builtin_seed_version": doc.get("builtin_seed_version", BUILTIN_SEED_VERSION),
    }

    def _mutate(data: dict[str, Any]) -> None:
        revs = data.setdefault("revisions", [])
        revs.append(revision)
        if len(revs) > _MAX_REVISIONS:
            del revs[: len(revs) - _MAX_REVISIONS]

    jsonstore.mutate_json(_REV_PATH, {"revisions": []}, _mutate)


def save_reference(types: Any, *, actor: str, reason: str = "Edited") -> dict[str, Any]:
    """Replace the reference's type→alerts map, bump the version, snapshot the result."""
    new_types = _sanitize_types(types)
    doc: dict[str, Any] = {}

    def _mutate(stored: Any) -> dict[str, Any]:
        current = stored if isinstance(stored, dict) else builtin_reference()
        value = {
            "version": int(current.get("version", 0)) + 1,
            "updated_at": _now(),
            "updated_by": actor or "",
            "builtin_seed_version": BUILTIN_SEED_VERSION,
            "amba_release": current.get("amba_release", ""),
            "amba_source": current.get("amba_source", ""),
            "amba_imported_at": current.get("amba_imported_at", ""),
            "types": new_types,
        }
        doc.update(value)
        return value

    jsonstore.mutate_json(_PATH, None, _mutate)
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
