"""Verdict derivation: configuration in, per-scenario recovery answers out.

Pure. Takes already-collected facts and produces one :class:`~app.resiliency.model.Verdict`
per scenario per resource. No I/O, so the whole engine is testable without Azure and demo
mode exercises exactly the code a real tenant does.

The shape of the reasoning, per scenario:

* **instance / zone / region** — redundancy first, then replication, then restore-from-backup
  as the fallback. A backup in a locally-redundant vault is *not* a fallback for region loss:
  the backups die with the region, which is why that case yields ``none`` despite the
  resource being protected.
* **corruption / deletion** — redundancy is skipped entirely
  (:func:`model.redundancy_helps`), because ZRS, GRS and multi-region writes replicate the
  damage. Only a point-in-time copy counts, and for deletion, soft delete as well.
"""
from __future__ import annotations

import math
from typing import Any

from app.resiliency import model, rpo
from app.resiliency.model import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    Evidence,
    Verdict,
)

_GEO_REPLICATIONS = {"GRS", "RA-GRS", "GZRS", "RA-GZRS", "multi-region-write",
                     "multi-region-read", "global"}
_ZONE_REPLICATIONS = {"ZRS", "GZRS", "RA-GZRS", "global"}

# Replication values that represent REAL redundancy. "LRS" is the absence of it, and reading
# a non-empty string as "redundant" once credited an un-backed-up VM with automatic recovery
# from instance loss.
REDUNDANT_REPLICATIONS = _GEO_REPLICATIONS | _ZONE_REPLICATIONS

# Services where the platform replaces a failed instance without anyone doing anything. A VM
# or a disk is not one of them: if the instance dies, something has to recover it.
_SELF_HEALING_TYPES = frozenset({
    "microsoft.storage/storageaccounts",
    "microsoft.sql/servers/databases",
    "microsoft.sql/managedinstances",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.netapp/netappaccounts/capacitypools/volumes",
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.web/staticsites",
    "microsoft.containerservice/managedclusters",
    "microsoft.keyvault/vaults",
    "microsoft.cache/redis",
    "microsoft.cache/redisenterprise",
    "microsoft.search/searchservices",
    "microsoft.network/applicationgateways",
    "microsoft.network/loadbalancers",
    "microsoft.network/publicipaddresses",
    "microsoft.network/azurefirewalls",
    "microsoft.network/natgateways",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/bastionhosts",
    "microsoft.cdn/profiles",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.logic/workflows",
    "microsoft.app/containerapps",
    "microsoft.app/managedenvironments",
    "microsoft.desktopvirtualization/hostpools",
    "microsoft.containerregistry/registries",
    "microsoft.apimanagement/service",
    "microsoft.eventhub/namespaces",
    "microsoft.servicebus/namespaces",
    "microsoft.datafactory/factories",
    "microsoft.recoveryservices/vaults",
    "microsoft.dataprotection/backupvaults",
    # The scale set replaces a failed instance; the DATA on it is a separate question,
    # answered by the logical scenarios.
    "microsoft.compute/virtualmachinescalesets",
})

# A vault whose storage does not leave the region cannot serve a region-loss recovery.
_GEO_VAULT = {"georedundant", "geo-redundant", "geo"}


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _pitr(native: dict[str, Any]) -> tuple[int | None, str, str] | None:
    """Point-in-time recovery offered by the platform itself, if any."""
    kind = str((native or {}).get("kind") or "")
    if kind in ("none", "unknown", ""):
        return None
    interval = (native or {}).get("interval_minutes")
    if kind == "cosmos_continuous":
        return 1, CONFIDENCE_HIGH, "Cosmos DB continuous backup"
    if kind == "cosmos_periodic":
        return (int(interval) if interval else None), CONFIDENCE_HIGH, (
            f"Cosmos DB periodic backup every {int(interval)} min" if interval
            else "Cosmos DB periodic backup, interval not reported")
    if kind == "sql_pitr":
        return (int(interval) if interval else 10), CONFIDENCE_MEDIUM, (
            "SQL point-in-time restore (continuous log backup)")
    if kind == "pg_backup":
        return (int(interval) if interval else 10), CONFIDENCE_MEDIUM, (
            "PostgreSQL point-in-time restore")
    if kind == "storage_pitr":
        return (int(interval) if interval else 5), CONFIDENCE_HIGH, (
            "Storage point-in-time restore")
    if kind == "redis_rdb":
        return (int(interval) if interval else None), CONFIDENCE_MEDIUM, (
            f"Redis RDB persistence every {int(interval)} min" if interval
            else "Redis RDB persistence, frequency not reported")
    if kind == "redis_aof":
        return 1, CONFIDENCE_MEDIUM, "Redis append-only file persistence"
    if kind == "anf_snapshot":
        return None, CONFIDENCE_MEDIUM, (
            "Azure NetApp Files snapshot or backup policy; the interval is set on the policy")
    return None


def _vault_backup(backup: dict[str, Any] | None) -> tuple[int | None, str, Evidence] | None:
    """RPO from a vault-backed policy, reconciled against the newest recovery point."""
    if not backup:
        return None
    configured, summary = rpo.parse_schedule_interval(backup.get("schedule_raw"))
    minutes, confidence, drift = rpo.observed_vs_configured(
        configured, backup.get("recovery_point_age_hours"))
    if minutes is None:
        return None
    detail = summary or "Vault backup"
    policy = str(backup.get("policy_name") or "")
    if policy:
        detail = f"{detail} ({policy})"
    return minutes, confidence, drift or Evidence(
        kind=model.EV_BACKUP_POLICY, detail=detail, source="Backup Manager")


def _infra_verdict(
    scenario: str, config: dict[str, Any], backup: dict[str, Any] | None,
    asr: dict[str, Any] | None,
) -> Verdict:
    """instance / zone / region loss: redundancy, then replication, then restore."""
    replication = str(config.get("replication") or "")
    zone_redundant = config.get("zone_redundant")
    zones = config.get("zones") or []

    # --- redundancy that removes the failure entirely --------------------------------
    if scenario == model.SCENARIO_ZONE_LOSS:
        if zone_redundant is True or replication in _ZONE_REPLICATIONS or len(zones) > 1:
            detail = (f"Zone-redundant ({replication})" if replication in _ZONE_REPLICATIONS
                      else (f"Deployed across zones {', '.join(zones)}" if len(zones) > 1
                            else "Zone-redundant configuration"))
            return model.verdict(
                scenario, rpo_minutes=0, rpo_state=model.RPO_KNOWN,
                rto_class=model.RTO_AUTOMATIC, confidence=CONFIDENCE_HIGH,
                basis=(Evidence(model.EV_ZONE_CONFIG, detail, "Resource Graph"),))

    if scenario == model.SCENARIO_INSTANCE_LOSS:
        rtype = str(config.get("type") or "").lower()
        if rtype in _SELF_HEALING_TYPES:
            return model.verdict(
                scenario, rpo_minutes=0, rpo_state=model.RPO_KNOWN,
                rto_class=model.RTO_AUTOMATIC, confidence=CONFIDENCE_MEDIUM,
                basis=(Evidence(model.EV_SKU,
                                "Platform-managed service; a failed instance is replaced "
                                "without operator action.", "Resource Graph"),))
        if zone_redundant is True or len(zones) > 1 or replication in REDUNDANT_REPLICATIONS:
            return model.verdict(
                scenario, rpo_minutes=0, rpo_state=model.RPO_KNOWN,
                rto_class=model.RTO_AUTOMATIC, confidence=CONFIDENCE_MEDIUM,
                basis=(Evidence(model.EV_SKU,
                                f"Redundant across instances ({replication or 'multiple zones'})",
                                "Resource Graph"),))

    if scenario == model.SCENARIO_REGION_LOSS:
        if replication in ("multi-region-write", "global"):
            return model.verdict(
                scenario, rpo_minutes=0, rpo_state=model.RPO_KNOWN,
                rto_class=model.RTO_AUTOMATIC, confidence=CONFIDENCE_HIGH,
                basis=(Evidence(model.EV_REPLICATION, f"Active in multiple regions ({replication})",
                                "Resource Graph"),))
        if asr and _token(asr.get("protection_state")) in ("protected", "replicationinprogress"):
            seconds = asr.get("rpo_seconds")
            # Round UP: a 30-second RPO is not zero data loss, and rounding to zero states a
            # stronger guarantee than the platform makes.
            minutes = math.ceil(float(seconds) / 60) if seconds is not None else None
            return model.verdict(
                scenario,
                rpo_minutes=minutes if minutes is not None else None,
                rpo_state=model.RPO_KNOWN if minutes is not None else model.RPO_UNKNOWN,
                rto_class=model.RTO_HOURS, confidence=CONFIDENCE_HIGH,
                basis=(Evidence(model.EV_REPLICATION,
                                (f"Site Recovery replication, measured RPO {seconds}s"
                                 if seconds is not None else "Site Recovery replication"),
                                "Site Recovery"),))
        if replication in _GEO_REPLICATIONS:
            minutes, confidence, detail = rpo.native_rpo(
                "storage_grs" if replication.endswith("GRS") or replication.endswith("GZRS")
                else "sql_geo_restore")
            return model.verdict(
                scenario, rpo_minutes=minutes, rpo_state=model.RPO_KNOWN,
                rto_class=model.RTO_HOURS, confidence=confidence,
                basis=(Evidence(model.EV_REPLICATION,
                                f"{replication} — {detail}", "Resource Graph"),))

    # --- fall back to restoring from backup ------------------------------------------
    vault = _vault_backup(backup)
    if vault:
        minutes, confidence, evidence = vault
        if scenario == model.SCENARIO_REGION_LOSS:
            redundancy = _token((backup or {}).get("vault_redundancy"))
            if redundancy and redundancy not in _GEO_VAULT:
                # The single most under-appreciated finding this module produces: the
                # resource IS protected, and the backups are in the region that just failed.
                return model.verdict(
                    scenario, rpo_state=model.RPO_NONE, rto_class=model.RTO_NONE,
                    confidence=CONFIDENCE_HIGH,
                    basis=(Evidence(model.EV_VAULT_REDUNDANCY,
                                    "Backups are held in a locally-redundant vault, so they "
                                    "are lost with the region.", "Backup Manager"), evidence))
        return model.verdict(
            scenario, rpo_minutes=minutes, rpo_state=model.RPO_KNOWN,
            rto_class=model.RTO_DAY_PLUS, confidence=confidence, basis=(evidence,))

    native = _pitr(config.get("native_backup") or {})
    if native:
        minutes, confidence, detail = native
        geo = bool((config.get("native_backup") or {}).get("geo_redundant"))
        if scenario == model.SCENARIO_REGION_LOSS and not geo:
            return model.verdict(
                scenario, rpo_state=model.RPO_NONE, rto_class=model.RTO_NONE,
                confidence=CONFIDENCE_MEDIUM,
                basis=(Evidence(model.EV_NATIVE_BACKUP,
                                f"{detail}, held in-region only.", "Resource Graph"),))
        return model.verdict(
            scenario, rpo_minutes=minutes, rpo_state=model.RPO_KNOWN,
            rto_class=model.RTO_HOURS, confidence=confidence,
            basis=(Evidence(model.EV_NATIVE_BACKUP, detail, "Resource Graph"),))

    if config.get("protection_state") == "unknown":
        return model.verdict(scenario)

    return model.verdict(
        scenario, rpo_state=model.RPO_NONE, rto_class=model.RTO_NONE, confidence=CONFIDENCE_HIGH,
        basis=(Evidence(model.EV_NO_PROTECTION,
                        "No redundancy, replication or backup was found for this resource.",
                        "Recovery Readiness"),))


def _logical_verdict(
    scenario: str, config: dict[str, Any], backup: dict[str, Any] | None,
) -> Verdict:
    """corruption / deletion: redundancy is skipped entirely, by design."""
    candidates: list[tuple[int | None, str, Evidence]] = []

    vault = _vault_backup(backup)
    if vault:
        candidates.append(vault)

    native = _pitr(config.get("native_backup") or {})
    if native:
        minutes, confidence, detail = native
        candidates.append((minutes, confidence,
                           Evidence(model.EV_NATIVE_BACKUP, detail, "Resource Graph")))

    if candidates:
        # The best recovery point wins — the reader recovers from whichever is freshest.
        minutes, confidence, evidence = min(
            candidates, key=lambda c: (c[0] is None, c[0] if c[0] is not None else 0))
        rto = model.RTO_HOURS if (minutes is not None and minutes <= 60) else model.RTO_DAY_PLUS
        basis = [evidence]
        if scenario == model.SCENARIO_ACCIDENTAL_DELETE and backup and backup.get("soft_delete"):
            basis.append(Evidence(model.EV_SOFT_DELETE,
                                  "Vault soft delete is enabled.", "Backup Manager"))
        return model.verdict(
            scenario, rpo_minutes=minutes, rpo_state=model.RPO_KNOWN, rto_class=rto,
            confidence=confidence, basis=tuple(basis))

    if scenario == model.SCENARIO_ACCIDENTAL_DELETE and config.get("soft_delete"):
        return model.verdict(
            scenario, rpo_minutes=0, rpo_state=model.RPO_KNOWN, rto_class=model.RTO_HOURS,
            confidence=CONFIDENCE_MEDIUM,
            basis=(Evidence(model.EV_SOFT_DELETE,
                            "Soft delete is enabled on the service.", "Resource Graph"),))

    kind = str((config.get("native_backup") or {}).get("kind") or "unknown")
    if kind == "unknown" and backup is None:
        # We could not map this type to any protection source. Not a claim of either extreme.
        return model.verdict(scenario)

    # Redundancy deliberately contributes nothing here. A flawlessly replicated resource
    # with no point-in-time copy has no recovery path from corruption, and saying so is the
    # single most valuable thing this module does.
    return model.verdict(
        scenario, rpo_state=model.RPO_NONE, rto_class=model.RTO_NONE, confidence=CONFIDENCE_HIGH,
        basis=(Evidence(model.EV_NO_PROTECTION,
                        "No point-in-time copy exists. Redundancy replicates the change and "
                        "cannot recover from it.", "Recovery Readiness"),))


def verdicts_for(
    config: dict[str, Any],
    *,
    backup: dict[str, Any] | None = None,
    asr: dict[str, Any] | None = None,
) -> dict[str, Verdict]:
    """Every scenario for one resource."""
    rtype = str(config.get("type") or "")
    out: dict[str, Verdict] = {}
    for scenario in model.SCENARIOS:
        ok, why = model.applies(rtype, scenario, config)
        if not ok:
            out[scenario] = model.not_applicable(scenario, why)
            continue
        if model.redundancy_helps(scenario):
            out[scenario] = _infra_verdict(scenario, config, backup, asr)
        else:
            out[scenario] = _logical_verdict(scenario, config, backup)
    return out


__all__ = ["verdicts_for"]
