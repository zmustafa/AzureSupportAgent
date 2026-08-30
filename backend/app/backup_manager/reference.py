"""Versioned, admin-editable Backup Manager reference registry.

Persisted at ``backend/.data/backup_manager_reference.json`` with a bounded revision history,
seeded from :mod:`app.backup_manager.builtin_seed` on first load.  Identical machinery to the
AMBA / Telemetry / Backup-DR reference sets, but the document holds four related baselines:

* ``failure_kb``  — backup job error code -> cause / remediation / retryable
* ``vault_checks`` — the ransomware-readiness controls and their weights
* ``tiers`` + ``sla`` — RPO, retention, and drill floors used for compliance scoring
* ``limits`` + ``cost_rates`` + ``auto_protect_policies`` — planning inputs

Only structurally valid entries survive a write; unknown keys are dropped rather than trusted.
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backup_manager.builtin_seed import SEED_VERSION, seed_reference
from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "backup_manager_reference_revisions.json"

_MAX_REVISIONS = 50
_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_SEVERITIES = {"critical", "error", "warning", "info"}
_CATEGORIES = {
    "guest_agent", "network", "resource_state", "extension", "guest_os", "encryption",
    "configuration", "governance", "capacity", "transient", "workload", "rbac",
    "site_recovery", "other",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any] | None:
    data = jsonstore.read_json(_PATH, None)
    if isinstance(data, dict) and isinstance(data.get("failure_kb"), list):
        return data
    return None


def _read_revs() -> dict[str, Any]:
    data = jsonstore.read_json(_REV_PATH, {"revisions": []})
    return data if isinstance(data, dict) else {"revisions": []}


def _base_document() -> dict[str, Any]:
    doc = seed_reference()
    doc["version"] = 1
    doc["updated_at"] = _now()
    doc["updated_by"] = "system"
    return doc


def load_reference() -> dict[str, Any]:
    """Current reference document, seeding the built-in baseline on first use."""
    doc = _read()
    if doc is None:
        def _seed(stored: Any) -> dict[str, Any]:
            if isinstance(stored, dict) and isinstance(stored.get("failure_kb"), list):
                return stored
            return _base_document()

        doc = jsonstore.mutate_json(_PATH, None, _seed)
    return doc


# --------------------------------------------------------------------------- sanitisation
def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def _sanitize_failure_kb(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or not _CODE_RE.match(code) or code.lower() in seen:
            continue
        seen.add(code.lower())
        category = str(item.get("category") or "other").strip().lower()
        severity = str(item.get("severity") or "error").strip().lower()
        out.append({
            "code": code,
            "title": str(item.get("title") or code)[:160],
            "category": category if category in _CATEGORIES else "other",
            "severity": severity if severity in _SEVERITIES else "error",
            "cause": str(item.get("cause") or "")[:800],
            "remediation": str(item.get("remediation") or "")[:800],
            "auto_fix": bool(item.get("auto_fix", False)),
        })
    return out


def _sanitize_vault_checks(raw: Any) -> list[dict[str, Any]]:
    known = {c["id"]: c for c in seed_reference()["vault_checks"]}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("id") or "").strip()
        # Check ids drive concrete evaluation code, so only built-in ids are accepted;
        # an operator may retune weight/severity/copy but cannot invent a check.
        if check_id not in known or check_id in seen:
            continue
        seen.add(check_id)
        base = dict(known[check_id])
        severity = str(item.get("severity") or base["severity"]).strip().lower()
        base.update({
            "label": str(item.get("label") or base["label"])[:160],
            "weight": _clamp_int(item.get("weight", base["weight"]), 0, 100, base["weight"]),
            "severity": severity if severity in _SEVERITIES else base["severity"],
            "why": str(item.get("why") or base.get("why") or "")[:500],
        })
        out.append(base)
    # Never silently lose a control: re-append any built-in the payload omitted.
    for check_id, base in known.items():
        if check_id not in seen:
            out.append(dict(base))
    return out


def _sanitize_tiers(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tier_id = str(item.get("id") or "").strip().lower()
        if not tier_id or not _CODE_RE.match(tier_id) or tier_id in seen:
            continue
        seen.add(tier_id)
        out.append({
            "id": tier_id,
            "label": str(item.get("label") or tier_id)[:80],
            "rpo_hours": _clamp_int(item.get("rpo_hours"), 1, 8760, 24),
            "retention_days": _clamp_int(item.get("retention_days"), 1, 36500, 30),
            "require_offsite": bool(item.get("require_offsite", False)),
            "drill_days": _clamp_int(item.get("drill_days"), 0, 3650, 365),
        })
    return out or [dict(t) for t in seed_reference()["tiers"]]


def _sanitize_cost(raw: Any) -> dict[str, Any]:
    base = seed_reference()["cost_rates"]
    raw = raw if isinstance(raw, dict) else {}
    pi_raw = raw.get("protected_instance") if isinstance(raw.get("protected_instance"), dict) else {}
    st_raw = raw.get("storage_gb_month") if isinstance(raw.get("storage_gb_month"), dict) else {}
    return {
        "currency": str(raw.get("currency") or base["currency"])[:8],
        "as_of": str(raw.get("as_of") or base["as_of"])[:16],
        "estimate_only": True,
        "source": str(raw.get("source") or base["source"])[:300],
        # Empty means "infer the pricing region from where the vaults actually live".
        "price_region": str(raw.get("price_region") or base.get("price_region") or "")[:64],
        "protected_instance": {
            key: _clamp_float(pi_raw.get(key), 0.0, 10_000.0, value)
            for key, value in base["protected_instance"].items()
        },
        "storage_gb_month": {
            key: _clamp_float(st_raw.get(key), 0.0, 100.0, value)
            for key, value in base["storage_gb_month"].items()
        },
        "snapshot_gb_month": _clamp_float(raw.get("snapshot_gb_month"), 0.0, 100.0, base["snapshot_gb_month"]),
        "site_recovery_instance_month": _clamp_float(
            raw.get("site_recovery_instance_month"), 0.0, 10_000.0, base["site_recovery_instance_month"]
        ),
        "assumed_instance_gb": _clamp_float(raw.get("assumed_instance_gb"), 1.0, 100_000.0, base["assumed_instance_gb"]),
    }


def _sanitize_limits(raw: Any) -> dict[str, Any]:
    base = seed_reference()["limits"]
    raw = raw if isinstance(raw, dict) else {}
    out = {key: _clamp_int(raw.get(key), 1, 1_000_000, value) for key, value in base.items() if isinstance(value, int)}
    out["warn_at_pct"] = _clamp_int(raw.get("warn_at_pct"), 1, 100, base["warn_at_pct"])
    out["source"] = str(raw.get("source") or base["source"])[:300]
    return out


def _sanitize_sla(raw: Any) -> dict[str, Any]:
    base = seed_reference()["sla"]
    raw = raw if isinstance(raw, dict) else {}
    return {
        "job_sla_hours": _clamp_int(raw.get("job_sla_hours"), 1, 720, base["job_sla_hours"]),
        "chronic_failure_days": _clamp_int(raw.get("chronic_failure_days"), 1, 90, base["chronic_failure_days"]),
        "stale_recovery_point_hours": _clamp_int(raw.get("stale_recovery_point_hours"), 1, 8760, base["stale_recovery_point_hours"]),
        "drill_stale_days": _clamp_int(raw.get("drill_stale_days"), 1, 3650, base["drill_stale_days"]),
    }


def sanitize(raw: Any) -> dict[str, Any]:
    """Project an arbitrary payload onto the reference schema (drops anything unrecognized)."""
    raw = raw if isinstance(raw, dict) else {}
    seed = seed_reference()
    failure_kb = _sanitize_failure_kb(raw.get("failure_kb"))
    return {
        "seed_version": SEED_VERSION,
        "failure_kb": failure_kb or [dict(i) for i in seed["failure_kb"]],
        "vault_checks": _sanitize_vault_checks(raw.get("vault_checks")),
        "tiers": _sanitize_tiers(raw.get("tiers")),
        "default_tier": str(raw.get("default_tier") or seed["default_tier"])[:40],
        "limits": _sanitize_limits(raw.get("limits")),
        "cost_rates": _sanitize_cost(raw.get("cost_rates")),
        "auto_protect_policies": [dict(i) for i in seed["auto_protect_policies"]],
        "sla": _sanitize_sla(raw.get("sla")),
    }


# --------------------------------------------------------------------------- revisions
def _meta(rev: dict[str, Any]) -> dict[str, Any]:
    doc = rev.get("document", {}) or {}
    return {
        "id": rev["id"],
        "version": rev.get("version", 0),
        "created_at": rev.get("created_at", ""),
        "by": rev.get("by", ""),
        "reason": rev.get("reason", ""),
        "failure_code_count": len(doc.get("failure_kb", []) or []),
        "tier_count": len(doc.get("tiers", []) or []),
    }


def _snapshot(doc: dict[str, Any], *, reason: str, actor: str) -> None:
    revision = {
        "id": str(uuid.uuid4()),
        "version": doc.get("version", 0),
        "created_at": _now(),
        "by": actor or "",
        "reason": reason or "Edited",
        "document": copy.deepcopy({k: v for k, v in doc.items() if k not in ("version", "updated_at", "updated_by")}),
    }

    def _mutate(data: dict[str, Any]) -> None:
        revs = data.setdefault("revisions", [])
        revs.append(revision)
        if len(revs) > _MAX_REVISIONS:
            del revs[: len(revs) - _MAX_REVISIONS]

    jsonstore.mutate_json(_REV_PATH, {"revisions": []}, _mutate)


def list_revisions() -> list[dict[str, Any]]:
    revs = _read_revs().get("revisions", []) or []
    return [_meta(r) for r in reversed(revs)]


def save_reference(payload: Any, *, actor: str, reason: str = "") -> dict[str, Any]:
    """Persist a sanitized reference, snapshotting the outgoing version first."""
    sanitized = sanitize(payload)
    doc: dict[str, Any] = {}

    def _mutate(stored: Any) -> dict[str, Any]:
        current = (
            stored
            if isinstance(stored, dict) and isinstance(stored.get("failure_kb"), list)
            else _base_document()
        )
        _snapshot(current, reason=reason or "Edited", actor=actor)
        value = dict(sanitized)
        value["version"] = int(current.get("version", 0)) + 1
        value["updated_at"] = _now()
        value["updated_by"] = actor or ""
        doc.update(value)
        return value

    jsonstore.mutate_json(_PATH, None, _mutate)
    return doc


def restore_revision(revision_id: str, *, actor: str) -> dict[str, Any]:
    for rev in _read_revs().get("revisions", []) or []:
        if rev.get("id") == revision_id:
            return save_reference(
                rev.get("document", {}), actor=actor,
                reason=f"Restored revision {rev.get('version', '?')}",
            )
    raise ValueError("Revision not found.")


def reset_reference(*, actor: str) -> dict[str, Any]:
    return save_reference(seed_reference(), actor=actor, reason="Reset to built-in baseline")


# --------------------------------------------------------------------------- lookups
def failure_index() -> dict[str, dict[str, Any]]:
    """Lowercased error code -> knowledge-base entry."""
    return {str(item.get("code", "")).lower(): item for item in load_reference().get("failure_kb", []) or []}


def tier_index() -> dict[str, dict[str, Any]]:
    return {str(item.get("id", "")): item for item in load_reference().get("tiers", []) or []}


def tier_for(tier_id: str | None) -> dict[str, Any]:
    doc = load_reference()
    index = {str(item.get("id", "")): item for item in doc.get("tiers", []) or []}
    return index.get(str(tier_id or ""), index.get(str(doc.get("default_tier") or ""), next(iter(index.values()))))


def sla() -> dict[str, Any]:
    return dict(load_reference().get("sla") or seed_reference()["sla"])


def cost_rates() -> dict[str, Any]:
    return dict(load_reference().get("cost_rates") or seed_reference()["cost_rates"])


def limits() -> dict[str, Any]:
    return dict(load_reference().get("limits") or seed_reference()["limits"])


def vault_checks() -> list[dict[str, Any]]:
    return list(load_reference().get("vault_checks") or seed_reference()["vault_checks"])
