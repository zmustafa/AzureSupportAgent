"""The resource join: five sources, five id shapes, one row.

This is where the module succeeds or fails, and it is unglamorous. Backup instances key on
``datasource_id`` and carry child suffixes (blob protection lives at
``…/storageAccounts/x/blobServices/default``); Site Recovery keys on the source resource id;
Advisor uses ``properties.resourceMetadata.resourceId``; assessment findings are already
lowercased. A mis-join is worse than no join, because it attributes one resource's
protection to another — so an id we do not recognize resolves to nothing and the row reports
``unknown``.

**The rule that prevents the worst failure in this module.** Backup Manager's snapshot is
explicitly user-triggered and per-scope: ``read_snapshot`` never computes. If it has not been
run, every backup fact is *unknown*, with provenance saying so — never "unprotected".
Rendering absent analysis as absent protection produces a full-estate false alarm, costs a
day, and permanently destroys trust in the number.
"""
from __future__ import annotations

from typing import Any

from app.backup_manager import service
from app.resiliency import derive, model

PROTECTION_PROTECTED = "protected"
PROTECTION_NOT_PROTECTED = "not_protected"
PROTECTION_UNKNOWN = "unknown"


def _child_suffixes() -> list[str]:
    """Datasource suffixes to strip, taken from Backup Manager's own eligibility map.

    Derived rather than copied: a second copy of this table drifts the moment a type is
    added, and the symptom — a storage account reported unprotected here and protected in
    Backup Manager — is silent."""
    from app.backup_manager.gaps import ELIGIBLE_TYPES

    out = {str(spec["child_suffix"]).lower()
           for spec in ELIGIBLE_TYPES.values() if spec.get("child_suffix")}
    return sorted(out, key=len, reverse=True)


def normalize_resource_id(raw: str) -> str:
    """Canonical, lowercased, child-suffix-stripped ARM id, or ``""`` if unrecognized."""
    text = service.canonical_id(str(raw or ""))
    if not text.startswith("/subscriptions/"):
        return ""
    for suffix in _child_suffixes():
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.rstrip("/")


def _index(rows: list[dict[str, Any]], *keys: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        for key in keys:
            rid = normalize_resource_id(str(row.get(key) or ""))
            if rid:
                out.setdefault(rid, row)
                break
    return out


def _eligible_types() -> set[str]:
    from app.backup_manager.gaps import ELIGIBLE_TYPES

    return set(ELIGIBLE_TYPES)


def build_rows(
    config: list[dict[str, Any]],
    *,
    backup: list[dict[str, Any]] | None = None,
    asr: list[dict[str, Any]] | None = None,
    advisor: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    backup_known: bool = True,
    backup_reason: str = "",
) -> list[dict[str, Any]]:
    """One joined row per resource, with per-scenario verdicts attached.

    ``backup_known=False`` means the backup estate was never read for this scope. Every
    protection fact then reports ``unknown`` and no resource is called unprotected.
    """
    backup_index = _index(backup or [], "datasource_id", "id")
    asr_index = _index(asr or [], "source_id", "datasource_id", "id")
    advisor_index: dict[str, list[dict[str, Any]]] = {}
    for rec in advisor or []:
        rid = normalize_resource_id(str(rec.get("resource_id") or ""))
        if rid:
            advisor_index.setdefault(rid, []).append(rec)
    finding_index: dict[str, list[dict[str, Any]]] = {}
    for finding in findings or []:
        rid = normalize_resource_id(str(finding.get("resource_id") or ""))
        if rid:
            finding_index.setdefault(rid, []).append(finding)

    eligible = _eligible_types()
    rows: list[dict[str, Any]] = []

    for item in config:
        rid = normalize_resource_id(str(item.get("id") or ""))
        if not rid:
            continue
        rtype = str(item.get("type") or "").lower()
        item_backup = backup_index.get(rid)
        item_asr = asr_index.get(rid)

        if not backup_known:
            state, reason = PROTECTION_UNKNOWN, (
                backup_reason or "Backup Manager has not analyzed this scope.")
            item_backup = None
        elif item_backup is not None:
            state, reason = PROTECTION_PROTECTED, ""
        elif rtype in eligible:
            state, reason = PROTECTION_NOT_PROTECTED, ""
        else:
            # Not in the eligibility map: we did not look, which is not the same as
            # looking and finding nothing.
            state, reason = PROTECTION_UNKNOWN, (
                "This resource type is not mapped to a backup datasource, so its protection "
                "state was not determined.")

        config_for_derive = dict(item)
        if state == PROTECTION_UNKNOWN:
            config_for_derive["protection_state"] = "unknown"
            if str((item.get("native_backup") or {}).get("kind") or "") == "none":
                # A type we cannot map, whose native backup we also could not read.
                config_for_derive["native_backup"] = {"kind": "unknown"}

        verdicts = derive.verdicts_for(config_for_derive, backup=item_backup, asr=item_asr)

        rows.append({
            "id": rid,
            "name": item.get("name", ""),
            "type": rtype,
            "location": item.get("location", ""),
            "resource_group": item.get("resource_group", ""),
            "subscription_id": item.get("subscription_id", ""),
            "redundancy": {
                "zones": item.get("zones") or [],
                "zone_redundant": item.get("zone_redundant"),
                "replication": item.get("replication", ""),
                "sku": item.get("sku", ""),
            },
            "protection": {
                "state": state,
                "reason": reason,
                "policy_name": (item_backup or {}).get("policy_name", ""),
                "frequency": _frequency(item_backup, item),
                "retention_days": (item_backup or {}).get("retention_days"),
                "recovery_point_age_hours": (item_backup or {}).get("recovery_point_age_hours"),
                "vault_redundancy": (item_backup or {}).get("vault_redundancy", ""),
                "native_backup": item.get("native_backup") or {"kind": "unknown"},
            },
            "dr": {
                "replicated": bool(item_asr),
                "rpo_seconds": (item_asr or {}).get("rpo_seconds"),
                "replication_health": (item_asr or {}).get("replication_health", ""),
                "last_test_failover_age_days": (item_asr or {}).get("last_test_failover_age_days"),
            },
            "advisor": advisor_index.get(rid, []),
            "findings": finding_index.get(rid, []),
            "size_gb": item.get("size_gb"),
            "verdicts": {s: v.as_dict() for s, v in verdicts.items()},
            "worst": _worst(verdicts),
            "demo_profile": item.get("demo_profile", ""),
        })

    rows.sort(key=lambda r: (r["name"] or r["id"]).lower())
    return rows


def _frequency(backup: dict[str, Any] | None, config: dict[str, Any]) -> str:
    """Human backup frequency — the column that started this whole feature."""
    from app.resiliency import rpo as rpo_mod

    if backup:
        _minutes, summary = rpo_mod.parse_schedule_interval(backup.get("schedule_raw"))
        if summary:
            return summary
    native = config.get("native_backup") or {}
    interval = native.get("interval_minutes")
    kind = str(native.get("kind") or "")
    if kind in ("none", "unknown", "") or not interval:
        return ""
    if interval % 1440 == 0:
        return f"Every {interval // 1440}d (platform)"
    if interval % 60 == 0:
        return f"Every {interval // 60}h (platform)"
    return f"Every {interval}m (platform)"


def _worst(verdicts: dict[str, model.Verdict]) -> dict[str, Any]:
    """The headline for a row: its worst applicable scenario."""
    applicable = [v for v in verdicts.values() if v.applicable]
    classes = [v.rto_class for v in applicable]
    rto_class, undetermined = model.worst_rto(classes)
    culprit = ""
    for scenario, v in verdicts.items():
        if v.applicable and v.rto_class == rto_class:
            culprit = scenario
            break
    return {
        "rto_class": rto_class,
        "scenario": culprit,
        "undetermined": undetermined,
        "no_recovery_path": [s for s, v in verdicts.items()
                             if v.applicable and v.rto_class == model.RTO_NONE],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts per scenario. Four buckets, never two: `undetermined` is its own answer."""
    by_scenario: dict[str, dict[str, int]] = {}
    for scenario in model.SCENARIOS:
        counts = {"determined": 0, "no_recovery_path": 0, "undetermined": 0,
                  "not_applicable": 0, "total": 0}
        for row in rows:
            v = row["verdicts"].get(scenario) or {}
            counts["total"] += 1
            if not v.get("applicable", True):
                counts["not_applicable"] += 1
            elif v.get("rto_class") == model.RTO_UNKNOWN:
                counts["undetermined"] += 1
            else:
                counts["determined"] += 1
                if v.get("rto_class") == model.RTO_NONE:
                    counts["no_recovery_path"] += 1
        by_scenario[scenario] = counts

    protection = {"protected": 0, "not_protected": 0, "unknown": 0}
    for row in rows:
        protection[row["protection"]["state"]] = protection.get(row["protection"]["state"], 0) + 1

    worst_scenario = max(
        by_scenario.items(), key=lambda kv: kv[1]["no_recovery_path"], default=("", {}))
    return {
        "resources": len(rows),
        "by_scenario": by_scenario,
        "protection": protection,
        "worst": {"scenario": worst_scenario[0],
                  "no_recovery_path": (worst_scenario[1] or {}).get("no_recovery_path", 0)},
    }


__all__ = ["normalize_resource_id", "build_rows", "summarize",
           "PROTECTION_PROTECTED", "PROTECTION_NOT_PROTECTED", "PROTECTION_UNKNOWN"]
