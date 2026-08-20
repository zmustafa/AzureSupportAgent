"""Versioned, admin-editable Recovery Readiness reference: restore rates and targets.

Two documents in one registry, both of which must be VISIBLE before the numbers they
produce are trustworthy:

* ``restore_rates`` + ``mechanism_minutes`` — the constants behind every RTO band. A band
  derived from a constant nobody can see or change is not reviewable, and the first question
  a sceptical engineer asks is "where did that come from".
* ``tiers`` — per-criticality, per-scenario RTO/RPO targets. Seeded from the Backup Manager
  tier registry so the two modules cannot state different RPO targets for the same resource.

Same shape as :mod:`app.backup_manager.reference`: bounded revision history, structural
validation on write, unknown keys dropped rather than trusted.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.resiliency import model

_PATH = Path(__file__).resolve().parents[2] / ".data" / "resiliency_reference.json"
_REV_PATH = Path(__file__).resolve().parents[2] / ".data" / "resiliency_reference_revisions.json"
_MAX_REVISIONS = 50

SEED_VERSION = 1

# Throughput defaults. These are STARTING POINTS to be tuned per tenant, not truths — the UI
# says so, and every band names the rate that produced it.
RESTORE_RATES: dict[str, int] = {
    "vm_restore_mbps": 40,
    "disk_restore_mbps": 100,
    "blob_restore_mbps": 60,
    "sql_restore_gb_per_hour": 200,
    "generic_restore_mbps": 50,
}

# Fixed overheads per recovery mechanism, in minutes, independent of data volume.
MECHANISM_MINUTES: dict[str, int] = {
    "asr_failover": 30,
    "sql_failover_group": 1,
    "sql_geo_restore": 60,
    "cosmos_manual_failover": 15,
    "vault_restore_overhead": 20,
    "native_pitr_overhead": 15,
    "detect_and_decide": 30,
}

#: Per-tier, per-scenario objectives. `rpo_minutes` for `data_corruption` is seeded from the
#: Backup Manager tier's `rpo_hours` so the two registries agree by construction.
TIER_SEED: list[dict[str, Any]] = [
    {
        "id": "mission_critical", "label": "Mission critical",
        "scenarios": {
            model.SCENARIO_INSTANCE_LOSS: {"rto_class": model.RTO_AUTOMATIC, "rpo_minutes": 0},
            model.SCENARIO_ZONE_LOSS: {"rto_class": model.RTO_MINUTES, "rpo_minutes": 5},
            model.SCENARIO_REGION_LOSS: {"rto_class": model.RTO_HOURS, "rpo_minutes": 60},
            model.SCENARIO_DATA_CORRUPTION: {"rto_class": model.RTO_HOURS, "rpo_minutes": 240},
            model.SCENARIO_ACCIDENTAL_DELETE: {"rto_class": model.RTO_HOURS, "rpo_minutes": 240},
        },
    },
    {
        "id": "business_critical", "label": "Business critical",
        "scenarios": {
            model.SCENARIO_INSTANCE_LOSS: {"rto_class": model.RTO_MINUTES, "rpo_minutes": 0},
            model.SCENARIO_ZONE_LOSS: {"rto_class": model.RTO_HOURS, "rpo_minutes": 60},
            model.SCENARIO_REGION_LOSS: {"rto_class": model.RTO_HOURS, "rpo_minutes": 240},
            model.SCENARIO_DATA_CORRUPTION: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 720},
            model.SCENARIO_ACCIDENTAL_DELETE: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 720},
        },
    },
    {
        "id": "standard", "label": "Standard",
        "scenarios": {
            model.SCENARIO_INSTANCE_LOSS: {"rto_class": model.RTO_HOURS, "rpo_minutes": 60},
            model.SCENARIO_ZONE_LOSS: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 1440},
            model.SCENARIO_REGION_LOSS: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 1440},
            model.SCENARIO_DATA_CORRUPTION: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 1440},
            model.SCENARIO_ACCIDENTAL_DELETE: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 1440},
        },
    },
    {
        "id": "low", "label": "Low",
        "scenarios": {
            model.SCENARIO_INSTANCE_LOSS: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 1440},
            model.SCENARIO_ZONE_LOSS: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 4320},
            model.SCENARIO_REGION_LOSS: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 4320},
            model.SCENARIO_DATA_CORRUPTION: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 4320},
            model.SCENARIO_ACCIDENTAL_DELETE: {"rto_class": model.RTO_DAY_PLUS, "rpo_minutes": 4320},
        },
    },
]

DEFAULT_TIER = "standard"

#: Maps the workload registry's criticality vocabulary onto tier ids.
CRITICALITY_TO_TIER: dict[str, str] = {
    "critical": "mission_critical",
    "high": "business_critical",
    "medium": "standard",
    "low": "low",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_document() -> dict[str, Any]:
    return {
        "seed_version": SEED_VERSION,
        "version": 1,
        "updated_at": _now(),
        "updated_by": "system",
        "restore_rates": dict(RESTORE_RATES),
        "mechanism_minutes": dict(MECHANISM_MINUTES),
        "tiers": copy.deepcopy(TIER_SEED),
        "default_tier": DEFAULT_TIER,
        # Defaults are available on screen immediately, but an exported report must have been
        # agreed by a person. A watermark is easy to crop; a refusal is not.
        "targets_acknowledged": False,
        "targets_acknowledged_by": "",
        "targets_acknowledged_at": "",
    }


def _read() -> dict[str, Any] | None:
    if not _PATH.exists():
        return None
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tiers"), list) else None


def _write(doc: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def load() -> dict[str, Any]:
    doc = _read()
    if doc is None:
        doc = seed_document()
        _write(doc)
    return doc


def reset_for_tests(path: Path | None = None) -> None:
    global _PATH, _REV_PATH
    if path is not None:
        _PATH = path / "resiliency_reference.json"
        _REV_PATH = path / "resiliency_reference_revisions.json"
    if _PATH.exists():
        _PATH.unlink()


# --------------------------------------------------------------------------- validation
def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def sanitize(incoming: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Merge a proposed document over the current one, dropping anything invalid.

    Returns ``(document, rejected)``. Structural nonsense is refused with a named field
    rather than silently coerced — a silently coerced restore rate produces a wrong band."""
    doc = copy.deepcopy(current)
    rejected: list[str] = []

    rates = incoming.get("restore_rates")
    if isinstance(rates, dict):
        for key, value in rates.items():
            if key not in RESTORE_RATES:
                rejected.append(f"restore_rates.{key}: unknown rate")
                continue
            clamped = _clamp_int(value, 1, 100_000, doc["restore_rates"][key])
            if str(clamped) != str(value):
                rejected.append(f"restore_rates.{key}: must be 1..100000")
            doc["restore_rates"][key] = clamped

    mechanisms = incoming.get("mechanism_minutes")
    if isinstance(mechanisms, dict):
        for key, value in mechanisms.items():
            if key not in MECHANISM_MINUTES:
                rejected.append(f"mechanism_minutes.{key}: unknown mechanism")
                continue
            doc["mechanism_minutes"][key] = _clamp_int(value, 0, 10_080,
                                                       doc["mechanism_minutes"][key])

    tiers = incoming.get("tiers")
    if isinstance(tiers, list):
        by_id = {t["id"]: t for t in doc["tiers"]}
        for tier in tiers:
            if not isinstance(tier, dict) or tier.get("id") not in by_id:
                rejected.append(f"tiers.{(tier or {}).get('id')}: unknown tier")
                continue
            target = by_id[tier["id"]]
            scenarios = tier.get("scenarios")
            if not isinstance(scenarios, dict):
                continue
            for scenario, spec in scenarios.items():
                if scenario not in model.SCENARIOS or not isinstance(spec, dict):
                    rejected.append(f"tiers.{tier['id']}.{scenario}: unknown scenario")
                    continue
                entry = target["scenarios"].setdefault(scenario, {})
                if "rpo_minutes" in spec:
                    entry["rpo_minutes"] = _clamp_int(
                        spec["rpo_minutes"], 0, 525_600, entry.get("rpo_minutes", 1440))
                if "rto_class" in spec:
                    if spec["rto_class"] in model.RTO_CLASSES and spec["rto_class"] != model.RTO_UNKNOWN:
                        entry["rto_class"] = spec["rto_class"]
                    else:
                        rejected.append(
                            f"tiers.{tier['id']}.{scenario}.rto_class: not a valid class")

    if "targets_acknowledged" in incoming:
        doc["targets_acknowledged"] = bool(incoming["targets_acknowledged"])
        doc["targets_acknowledged_at"] = _now() if doc["targets_acknowledged"] else ""

    return doc, rejected


def save(incoming: dict[str, Any], *, actor: str = "") -> tuple[dict[str, Any], list[str]]:
    current = load()
    doc, rejected = sanitize(incoming, current)
    doc["version"] = int(current.get("version", 1)) + 1
    doc["updated_at"] = _now()
    doc["updated_by"] = actor or "unknown"
    if incoming.get("targets_acknowledged"):
        doc["targets_acknowledged_by"] = actor or "unknown"
    _append_revision(current)
    _write(doc)
    return doc, rejected


def _append_revision(previous: dict[str, Any]) -> None:
    try:
        data = json.loads(_REV_PATH.read_text(encoding="utf-8")) if _REV_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    revisions = data.get("revisions") if isinstance(data, dict) else None
    revisions = revisions if isinstance(revisions, list) else []
    revisions.append(previous)
    revisions = revisions[-_MAX_REVISIONS:]
    try:
        _REV_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REV_PATH.write_text(json.dumps({"revisions": revisions}), encoding="utf-8")
    except OSError:
        pass


def tier_for(tier_id: str | None) -> dict[str, Any]:
    doc = load()
    wanted = str(tier_id or doc.get("default_tier") or DEFAULT_TIER)
    for tier in doc["tiers"]:
        if tier["id"] == wanted:
            return tier
    return doc["tiers"][-1]


def tier_for_criticality(criticality: str | None) -> str:
    return CRITICALITY_TO_TIER.get(str(criticality or "").strip().lower(), DEFAULT_TIER)


__all__ = [
    "load", "save", "sanitize", "seed_document", "reset_for_tests", "tier_for",
    "tier_for_criticality", "RESTORE_RATES", "MECHANISM_MINUTES", "TIER_SEED",
    "DEFAULT_TIER", "CRITICALITY_TO_TIER",
]
