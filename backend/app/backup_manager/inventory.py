"""Live Backup Manager estate collection.

Azure exposes the whole backup estate through the Resource Graph ``recoveryservicesresources``
table — protected items, backup instances, policies, jobs, Site Recovery replicated items and
recovery plans — which is what Backup Center itself is built on.  One tenant-wide sweep
therefore replaces per-vault enumeration and scales to thousands of instances.

Every source is collected independently and fail-soft: an unsupported table or a permission
gap on one source degrades that section (recorded in ``errors``) instead of failing the sweep.

``collect_estate`` is cached; ``build_estate`` is a pure function over already-fetched rows so
the demo seed and the unit tests exercise exactly the production shaping code.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.backup_manager import cache as inventory_cache
from app.backup_manager import service

log = logging.getLogger("app.backup_manager.inventory")

#: ``progress(phase, message)`` — supplied by the refresh job so the operator can watch a
#: sweep advance. Collection works identically when it is ``None``.
ProgressFn = Callable[[str, str], Awaitable[None]]

#: Human labels for the nine Resource Graph sources, used in progress lines.
SOURCE_LABELS = {
    "vaults": "vault",
    "rsv_items": "Recovery Services protected item",
    "dp_instances": "Backup vault instance",
    "rsv_jobs": "Recovery Services job",
    "dp_jobs": "Backup vault job",
    "rsv_policies": "Recovery Services policy",
    "dp_policies": "Backup vault policy",
    "replication": "Site Recovery replicated item",
    "recovery_plans": "recovery plan",
}

# ARG type names (always compared lowercase).
T_RSV_ITEM = "microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers/protecteditems"
T_RSV_JOB = "microsoft.recoveryservices/vaults/backupjobs"
T_RSV_POLICY = "microsoft.recoveryservices/vaults/backuppolicies"
T_DP_INSTANCE = "microsoft.dataprotection/backupvaults/backupinstances"
T_DP_JOB = "microsoft.dataprotection/backupvaults/backupjobs"
T_DP_POLICY = "microsoft.dataprotection/backupvaults/backuppolicies"
T_ASR_ITEM = "microsoft.recoveryservices/vaults/replicationfabrics/replicationprotectioncontainers/replicationprotecteditems"
T_ASR_PLAN = "microsoft.recoveryservices/vaults/replicationrecoveryplans"

# ARG retains backup jobs for a rolling window only. Anything older needs the Log Analytics
# Backup Reports path (see app.backup_manager.reports), which the UI states explicitly.
ARG_JOB_WINDOW_DAYS = 7

SUCCESS_STATES = {"completed", "succeeded", "success", "completedwithwarnings", "completedwitherrors"}
FAILED_STATES = {"failed", "cancelled", "canceled", "completedwitherrors"}
RUNNING_STATES = {"inprogress", "in progress", "running", "started", "accepted"}


def _q(type_name: str, projection: str, *, extra: str = "") -> str:
    """Build a defensive ARG query: ``properties`` is re-parsed from its string form so the
    projection is identical whether ARG hands back dynamic or string properties."""
    return (
        "recoveryservicesresources\n"
        f"| where type =~ '{type_name}'\n"
        "| extend p = parse_json(tostring(properties))\n"
        f"{extra}"
        f"| project {projection}"
    )


RSV_ITEM_QUERY = _q(T_RSV_ITEM, """
    id, name, resourceGroup, subscriptionId, location,
    friendlyName = tostring(p.friendlyName),
    datasourceId = tolower(tostring(p.sourceResourceId)),
    backupManagementType = tostring(p.backupManagementType),
    workloadType = tostring(p.workloadType),
    protectedItemType = tostring(p.protectedItemType),
    protectionState = tostring(p.protectionState),
    protectionStatus = tostring(p.protectionStatus),
    healthStatus = tostring(p.healthStatus),
    lastBackupStatus = tostring(p.lastBackupStatus),
    lastBackupTime = tostring(p.lastBackupTime),
    lastRecoveryPoint = tostring(p.lastRecoveryPoint),
    policyId = tolower(tostring(p.policyId)),
    policyName = tostring(p.policyName),
    isArchiveEnabled = tostring(p.isArchiveEnabled),
    lastBackupErrorCode = tostring(p.lastBackupErrorDetail.errorCode),
    lastBackupErrorMessage = tostring(p.lastBackupErrorDetail.errorString)
""".strip())

DP_INSTANCE_QUERY = _q(T_DP_INSTANCE, """
    id, name, resourceGroup, subscriptionId, location,
    friendlyName = tostring(p.friendlyName),
    datasourceId = tolower(tostring(p.dataSourceInfo.resourceID)),
    datasourceType = tostring(p.dataSourceInfo.datasourceType),
    datasourceName = tostring(p.dataSourceInfo.resourceName),
    protectionState = tostring(p.currentProtectionState),
    protectionStatus = tostring(p.protectionStatus.status),
    protectionErrorCode = tostring(p.protectionStatus.errorDetails.code),
    protectionErrorMessage = tostring(p.protectionStatus.errorDetails.message),
    policyId = tolower(tostring(p.policyInfo.policyId)),
    policyName = tostring(p.policyInfo.name),
    provisioningState = tostring(p.provisioningState)
""".strip())

RSV_JOB_QUERY = _q(T_RSV_JOB, """
    id, name, resourceGroup, subscriptionId,
    operation = tostring(p.operation),
    status = tostring(p.status),
    startTime = tostring(p.startTime),
    endTime = tostring(p.endTime),
    duration = tostring(p.duration),
    entityFriendlyName = tostring(p.entityFriendlyName),
    backupManagementType = tostring(p.backupManagementType),
    errorCode = tostring(p.errorDetails[0].errorCode),
    errorMessage = tostring(p.errorDetails[0].errorString),
    extendedErrorCode = tostring(p.extendedInfo.propertyBag['Error Code']),
    jobType = tostring(p.jobType)
""".strip())

DP_JOB_QUERY = _q(T_DP_JOB, """
    id, name, resourceGroup, subscriptionId,
    operation = tostring(p.operation),
    status = tostring(p.status),
    startTime = tostring(p.startTime),
    endTime = tostring(p.endTime),
    duration = tostring(p.duration),
    entityFriendlyName = tostring(p.backupInstanceFriendlyName),
    datasourceId = tolower(tostring(p.dataSourceId)),
    datasourceType = tostring(p.dataSourceType),
    errorCode = tostring(p.extendedInfo.additionalDetails.errorCode),
    errorMessage = tostring(p.errorDetails[0].message)
""".strip())

RSV_POLICY_QUERY = _q(T_RSV_POLICY, """
    id, name, resourceGroup, subscriptionId, location,
    backupManagementType = tostring(p.backupManagementType),
    policyType = tostring(p.policyType),
    protectedItemsCount = toint(p.protectedItemsCount),
    timeZone = tostring(p.timeZone),
    instantRpRetentionRangeInDays = toint(p.instantRpRetentionRangeInDays),
    schedulePolicy = tostring(p.schedulePolicy),
    retentionPolicy = tostring(p.retentionPolicy),
    workLoadType = tostring(p.workLoadType)
""".strip())

DP_POLICY_QUERY = _q(T_DP_POLICY, """
    id, name, resourceGroup, subscriptionId, location,
    datasourceTypes = tostring(p.datasourceTypes),
    policyRules = tostring(p.policyRules),
    objectType = tostring(p.objectType)
""".strip())

ASR_ITEM_QUERY = _q(T_ASR_ITEM, """
    id, name, resourceGroup, subscriptionId, location,
    friendlyName = tostring(p.friendlyName),
    protectedItemType = tostring(p.protectedItemType),
    protectionState = tostring(p.protectionState),
    protectionStateDescription = tostring(p.protectionStateDescription),
    replicationHealth = tostring(p.replicationHealth),
    failoverHealth = tostring(p.failoverHealth),
    testFailoverState = tostring(p.testFailoverState),
    lastSuccessfulTestFailoverTime = tostring(p.lastSuccessfulTestFailoverTime),
    lastSuccessfulFailoverTime = tostring(p.lastSuccessfulFailoverTime),
    primaryFabricFriendlyName = tostring(p.primaryFabricFriendlyName),
    recoveryFabricFriendlyName = tostring(p.recoveryFabricFriendlyName),
    policyFriendlyName = tostring(p.policyFriendlyName),
    activeLocation = tostring(p.activeLocation),
    rpoInSeconds = tolong(p.providerSpecificDetails.rpoInSeconds),
    lastRpoCalculatedTime = tostring(p.providerSpecificDetails.lastRpoCalculatedTime),
    recoveryAzureVMName = tostring(p.providerSpecificDetails.recoveryAzureVMName),
    healthErrors = tostring(p.healthErrors)
""".strip())

ASR_PLAN_QUERY = _q(T_ASR_PLAN, """
    id, name, resourceGroup, subscriptionId,
    friendlyName = tostring(p.friendlyName),
    primaryFabricFriendlyName = tostring(p.primaryFabricFriendlyName),
    recoveryFabricFriendlyName = tostring(p.recoveryFabricFriendlyName),
    lastPlannedFailoverTime = tostring(p.lastPlannedFailoverTime),
    lastTestFailoverTime = tostring(p.lastTestFailoverTime),
    currentScenarioName = tostring(p.currentScenario.scenarioName),
    currentScenarioStatus = tostring(p.currentScenarioStatus),
    replicationProviders = tostring(p.replicationProviders),
    groups = tostring(p.groups)
""".strip())

VAULT_QUERY = """
resources
| where type in~ ('microsoft.recoveryservices/vaults', 'microsoft.dataprotection/backupvaults')
| extend p = parse_json(tostring(properties))
| project id, name, type, location, resourceGroup, subscriptionId,
    tags = tostring(tags),
    skuName = tostring(parse_json(tostring(sku)).name),
    identityType = tostring(parse_json(tostring(identity)).type),
    provisioningState = tostring(p.provisioningState),
    publicNetworkAccess = tostring(p.publicNetworkAccess),
    securitySettings = tostring(p.securitySettings),
    storageSettings = tostring(p.storageSettings),
    redundancySettings = tostring(p.redundancySettings),
    featureSettings = tostring(p.featureSettings),
    monitoringSettings = tostring(p.monitoringSettings),
    encryption = tostring(p.encryption),
    privateEndpointConnections = tostring(p.privateEndpointConnections),
    privateEndpointStateForBackup = tostring(p.privateEndpointStateForBackup)
""".strip()


# --------------------------------------------------------------------------- helpers
def _json(value: Any) -> Any:
    """Rows project nested objects as JSON text so the projection stays stable; re-parse here."""
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text or text in ("null", "None"):
        return None
    import json as _stdjson

    try:
        return _stdjson.loads(text)
    except (ValueError, TypeError):
        return None


def _vault_kind(vault_id: str) -> str:
    return "backup" if service.is_backup_vault(vault_id) else "recovery_services"


# Datasource ids for sub-resources address a child of the real ARM resource (a storage
# account's blob service, a file service, and so on). Orphan detection must resolve back to
# the parent, or every blob backup would be reported as protecting a deleted resource.
_CHILD_MARKERS = ("/blobservices/", "/fileservices/", "/tableservices/", "/queueservices/", "/databases/")


def _parent_ids(datasource_id: str) -> list[str]:
    out = [datasource_id]
    low = datasource_id.lower()
    for marker in _CHILD_MARKERS:
        index = low.find(marker)
        if index > 0:
            out.append(datasource_id[:index])
    return out


def _is_orphaned(datasource_id: str, live_resource_ids: set[str] | None) -> bool:
    """``False`` whenever the check cannot run — an unverifiable datasource must never be
    reported as deleted."""
    if live_resource_ids is None or not datasource_id:
        return False
    return not any(candidate in live_resource_ids for candidate in _parent_ids(datasource_id))


def _status_bucket(status: str) -> str:
    low = str(status or "").strip().lower()
    if low in RUNNING_STATES:
        return "running"
    if low in FAILED_STATES or "fail" in low:
        return "failed"
    if low in SUCCESS_STATES:
        return "succeeded"
    return "unknown"


def _duration_seconds(value: Any) -> float | None:
    """Parse an ISO-8601 duration (``PT1H2M3S``) or ``hh:mm:ss.fff`` into seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.upper().startswith("P"):
        import re as _re

        match = _re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", text.upper())
        if not match:
            return None
        days, hours, minutes, seconds = (float(g or 0) for g in match.groups())
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    parts = text.split(":")
    if len(parts) == 3:
        try:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- shaping
def shape_vault(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a Recovery Services vault or Backup vault row into one schema."""
    vault_id = str(row.get("id") or "")
    kind = _vault_kind(vault_id)
    security = _json(row.get("securitySettings")) or {}
    storage = _json(row.get("storageSettings")) or []
    redundancy = _json(row.get("redundancySettings")) or {}
    features = _json(row.get("featureSettings")) or {}
    monitoring = _json(row.get("monitoringSettings")) or {}
    encryption = _json(row.get("encryption")) or {}
    endpoints = _json(row.get("privateEndpointConnections")) or []

    soft_delete = (security.get("softDeleteSettings") or {}) if isinstance(security, dict) else {}
    immutability = (security.get("immutabilitySettings") or {}) if isinstance(security, dict) else {}

    # Backup vaults express redundancy per datastore; Recovery Services vaults use a single
    # standard-tier setting. Normalise both onto one lowercase token.
    redundancy_token = ""
    if kind == "backup":
        for entry in storage if isinstance(storage, list) else []:
            if isinstance(entry, dict) and str(entry.get("datastoreType") or "").lower() == "vaultstore":
                redundancy_token = str(entry.get("type") or "")
                break
        if not redundancy_token and isinstance(storage, list) and storage:
            redundancy_token = str((storage[0] or {}).get("type") or "")
    else:
        redundancy_token = str(redundancy.get("standardTierStorageRedundancy") or "")

    crr = str(redundancy.get("crossRegionRestore") or "")
    if kind == "backup":
        crr = str(((features.get("crossRegionRestoreSettings") or {}) or {}).get("state") or "")
    csr = str(((features.get("crossSubscriptionRestoreSettings") or {}) or {}).get("state") or "")

    monitor_alerts = ""
    azure_monitor = (monitoring.get("azureMonitorAlertSettings") or {}) if isinstance(monitoring, dict) else {}
    if azure_monitor:
        monitor_alerts = str(azure_monitor.get("alertsForAllJobFailures") or azure_monitor.get("alertsForAllFailoverIssues") or "")

    # Recovery Services vaults report multi-user authorisation on the vault resource itself;
    # Backup vaults only expose it through the Resource Guard association (filled by
    # enrich_vaults). A missing value stays None so posture reports "unknown", not "absent".
    mua_raw = str(security.get("multiUserAuthorization") or "") if isinstance(security, dict) else ""
    mua_enabled: bool | None = None
    if mua_raw:
        mua_enabled = mua_raw.strip().lower() == "enabled"

    return {
        "id": vault_id,
        "name": str(row.get("name") or ""),
        "kind": kind,
        "type": str(row.get("type") or "").lower(),
        "location": str(row.get("location") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "subscription_id": str(row.get("subscriptionId") or ""),
        "sku": str(row.get("skuName") or ""),
        "identity_type": str(row.get("identityType") or ""),
        "provisioning_state": str(row.get("provisioningState") or ""),
        "public_network_access": str(row.get("publicNetworkAccess") or ""),
        "soft_delete_state": str(soft_delete.get("state") or soft_delete.get("softDeleteState") or ""),
        "soft_delete_retention_days": soft_delete.get("retentionDurationInDays") or soft_delete.get("softDeleteRetentionPeriodInDays"),
        "immutability_state": str(immutability.get("state") or ""),
        "redundancy": redundancy_token,
        "cross_region_restore": crr,
        "cross_subscription_restore": csr,
        "monitor_alerts": monitor_alerts,
        "cmk": bool((encryption or {}).get("keyVaultProperties")),
        "private_endpoints": len(endpoints if isinstance(endpoints, list) else []),
        "private_endpoint_state_for_backup": str(row.get("privateEndpointStateForBackup") or ""),
        "tags": _json(row.get("tags")) or {},
        # Filled in by enrich_vaults (ARM reads that Resource Graph does not surface).
        "mua_enabled": mua_enabled,
        "mua_resource_guard_id": "",
        "diagnostics_workspaces": [],
        "diagnostics_enabled": None,
        "enrichment_error": "",
    }


def shape_rsv_item(row: dict[str, Any]) -> dict[str, Any]:
    item_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(item_id)
    state = str(row.get("protectionState") or "")
    last_status = str(row.get("lastBackupStatus") or "")
    rp = str(row.get("lastRecoveryPoint") or "")
    return {
        "id": item_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "vault_kind": "recovery_services",
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "location": str(row.get("location") or ""),
        "friendly_name": str(row.get("friendlyName") or row.get("name") or ""),
        "datasource_id": service.canonical_id(row.get("datasourceId") or ""),
        "datasource_type": str(row.get("workloadType") or row.get("protectedItemType") or ""),
        "backup_management_type": str(row.get("backupManagementType") or ""),
        "protection_state": state,
        "protection_status": str(row.get("protectionStatus") or ""),
        "health_status": str(row.get("healthStatus") or ""),
        "last_backup_status": last_status,
        "last_backup_time": str(row.get("lastBackupTime") or ""),
        "latest_recovery_point": rp,
        "recovery_point_age_hours": service.age_hours(rp),
        "policy_id": service.canonical_id(row.get("policyId") or ""),
        "policy_name": str(row.get("policyName") or ""),
        "archive_enabled": str(row.get("isArchiveEnabled") or "").lower() == "true",
        "last_error_code": str(row.get("lastBackupErrorCode") or ""),
        "last_error_message": service.safe_error(row.get("lastBackupErrorMessage")),
        "protection_stopped": "stopped" in state.lower() or "paused" in state.lower(),
        "retain_data_only": state.lower() in ("protectionstopped", "backupsstopped", "protectionpaused"),
    }


def shape_dp_instance(row: dict[str, Any]) -> dict[str, Any]:
    item_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(item_id)
    state = str(row.get("protectionState") or "")
    return {
        "id": item_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "vault_kind": "backup",
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "location": str(row.get("location") or ""),
        "friendly_name": str(row.get("friendlyName") or row.get("datasourceName") or row.get("name") or ""),
        "datasource_id": service.canonical_id(row.get("datasourceId") or ""),
        "datasource_type": str(row.get("datasourceType") or ""),
        "backup_management_type": "DataProtection",
        "protection_state": state,
        "protection_status": str(row.get("protectionStatus") or ""),
        "health_status": "",
        "last_backup_status": "",
        "last_backup_time": "",
        # Backup vaults do not surface a latest recovery point in Resource Graph; it is
        # derived from the most recent successful job instead (see build_estate).
        "latest_recovery_point": "",
        "recovery_point_age_hours": None,
        "policy_id": service.canonical_id(row.get("policyId") or ""),
        "policy_name": str(row.get("policyName") or ""),
        "archive_enabled": False,
        "last_error_code": str(row.get("protectionErrorCode") or ""),
        "last_error_message": service.safe_error(row.get("protectionErrorMessage")),
        "protection_stopped": "stopped" in state.lower() or "paused" in state.lower(),
        "retain_data_only": state.lower() in ("protectionstopped", "backupsstopped"),
    }


def shape_job(row: dict[str, Any], *, kind: str) -> dict[str, Any]:
    job_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(job_id)
    status = str(row.get("status") or "")
    start = str(row.get("startTime") or "")
    end = str(row.get("endTime") or "")
    duration = _duration_seconds(row.get("duration"))
    if duration is None and start and end:
        start_dt, end_dt = service.parse_iso(start), service.parse_iso(end)
        if start_dt and end_dt:
            duration = max(0.0, (end_dt - start_dt).total_seconds())
    error_code = str(row.get("errorCode") or row.get("extendedErrorCode") or "")
    return {
        "id": job_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "vault_kind": kind,
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "operation": str(row.get("operation") or row.get("jobType") or ""),
        "status": status,
        "status_bucket": _status_bucket(status),
        "start_time": start,
        "end_time": end,
        "duration_seconds": duration,
        "entity_name": str(row.get("entityFriendlyName") or ""),
        "datasource_id": service.canonical_id(row.get("datasourceId") or ""),
        "datasource_type": str(row.get("datasourceType") or row.get("backupManagementType") or ""),
        "backup_management_type": str(row.get("backupManagementType") or ""),
        "error_code": error_code,
        "error_message": service.safe_error(row.get("errorMessage")),
        "age_hours": service.age_hours(start),
    }


def _rsv_retention_days(retention: Any) -> int | None:
    """Longest retention expressed by a Recovery Services retention policy, in days."""
    policy = _json(retention) or {}
    if not isinstance(policy, dict):
        return None
    best = 0
    daily = (policy.get("dailySchedule") or {}).get("retentionDuration") or {}
    weekly = (policy.get("weeklySchedule") or {}).get("retentionDuration") or {}
    monthly = (policy.get("monthlySchedule") or {}).get("retentionDuration") or {}
    yearly = (policy.get("yearlySchedule") or {}).get("retentionDuration") or {}
    simple = policy.get("retentionDuration") or {}
    units = {"days": 1, "weeks": 7, "months": 30, "years": 365}
    for duration in (daily, weekly, monthly, yearly, simple):
        if not isinstance(duration, dict):
            continue
        try:
            count = int(duration.get("count") or 0)
        except (TypeError, ValueError):
            continue
        factor = units.get(str(duration.get("durationType") or "").lower(), 0)
        best = max(best, count * factor)
    return best or None


def _dp_retention_days(rules: Any) -> int | None:
    """Longest ISO-8601 retention across Backup vault policy lifecycle rules, in days."""
    parsed = _json(rules) or []
    if not isinstance(parsed, list):
        return None
    import re as _re

    best = 0
    for rule in parsed:
        if not isinstance(rule, dict):
            continue
        for lifecycle in (rule.get("lifecycles") or []):
            duration = str(((lifecycle or {}).get("deleteAfter") or {}).get("duration") or "")
            match = _re.match(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$", duration.upper())
            if not match:
                continue
            years, months, weeks, days = (int(g or 0) for g in match.groups())
            best = max(best, years * 365 + months * 30 + weeks * 7 + days)
    return best or None


def _rsv_schedule_summary(schedule: Any) -> str:
    policy = _json(schedule) or {}
    if not isinstance(policy, dict):
        return ""
    run_freq = str(policy.get("scheduleRunFrequency") or "")
    times = policy.get("scheduleRunTimes") or []
    hourly = policy.get("hourlySchedule") or {}
    if hourly:
        interval = hourly.get("interval")
        return f"Hourly every {interval}h" if interval else "Hourly"
    first = str(times[0]) if isinstance(times, list) and times else ""
    clock = first[11:16] if len(first) >= 16 else ""
    days = policy.get("scheduleRunDays") or []
    if run_freq.lower() == "weekly" and isinstance(days, list) and days:
        return f"Weekly {', '.join(str(d)[:3] for d in days)} {clock}".strip()
    return f"{run_freq or 'Scheduled'} {clock}".strip()


def shape_rsv_policy(row: dict[str, Any]) -> dict[str, Any]:
    policy_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(policy_id)
    return {
        "id": service.canonical_id(policy_id),
        "arm_id": policy_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "vault_kind": "recovery_services",
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "backup_management_type": str(row.get("backupManagementType") or ""),
        "policy_type": str(row.get("policyType") or ""),
        "workload_type": str(row.get("workLoadType") or ""),
        "protected_items_count": int(row.get("protectedItemsCount") or 0),
        "time_zone": str(row.get("timeZone") or ""),
        "instant_rp_days": row.get("instantRpRetentionRangeInDays"),
        "schedule_summary": _rsv_schedule_summary(row.get("schedulePolicy")),
        "retention_days": _rsv_retention_days(row.get("retentionPolicy")),
        "schedule_raw": _json(row.get("schedulePolicy")),
        "retention_raw": _json(row.get("retentionPolicy")),
        "datasource_types": [],
    }


def shape_dp_policy(row: dict[str, Any]) -> dict[str, Any]:
    policy_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(policy_id)
    types = _json(row.get("datasourceTypes")) or []
    rules = _json(row.get("policyRules")) or []
    schedule = ""
    for rule in rules if isinstance(rules, list) else []:
        repeating = ((rule or {}).get("trigger") or {}).get("schedule") or {}
        expressions = repeating.get("repeatingTimeIntervals") or []
        if expressions:
            schedule = str(expressions[0])
            break
    return {
        "id": service.canonical_id(policy_id),
        "arm_id": policy_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "vault_kind": "backup",
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "backup_management_type": "DataProtection",
        "policy_type": str(row.get("objectType") or ""),
        "workload_type": ", ".join(str(t) for t in types) if isinstance(types, list) else "",
        "protected_items_count": 0,
        "time_zone": "",
        "instant_rp_days": None,
        "schedule_summary": schedule,
        "retention_days": _dp_retention_days(row.get("policyRules")),
        "schedule_raw": None,
        "retention_raw": rules,
        "datasource_types": [str(t) for t in types] if isinstance(types, list) else [],
    }


def shape_replication(row: dict[str, Any]) -> dict[str, Any]:
    item_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(item_id)
    errors = _json(row.get("healthErrors")) or []
    health = str(row.get("replicationHealth") or "")
    rpo = row.get("rpoInSeconds")
    try:
        rpo_seconds = int(rpo) if rpo not in (None, "") else None
    except (TypeError, ValueError):
        rpo_seconds = None
    last_test = str(row.get("lastSuccessfulTestFailoverTime") or "")
    return {
        "id": item_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "subscription_id": str(row.get("subscriptionId") or ""),
        "resource_group": str(row.get("resourceGroup") or ""),
        "friendly_name": str(row.get("friendlyName") or row.get("name") or ""),
        "protected_item_type": str(row.get("protectedItemType") or ""),
        "protection_state": str(row.get("protectionState") or ""),
        "protection_state_description": str(row.get("protectionStateDescription") or ""),
        "replication_health": health,
        "healthy": health.lower() in ("normal", "healthy"),
        "failover_health": str(row.get("failoverHealth") or ""),
        "test_failover_state": str(row.get("testFailoverState") or ""),
        "last_test_failover": last_test,
        "last_test_failover_age_days": service.age_days(last_test),
        "last_failover": str(row.get("lastSuccessfulFailoverTime") or ""),
        "primary_region": str(row.get("primaryFabricFriendlyName") or ""),
        "recovery_region": str(row.get("recoveryFabricFriendlyName") or ""),
        "policy_name": str(row.get("policyFriendlyName") or ""),
        "active_location": str(row.get("activeLocation") or ""),
        "rpo_seconds": rpo_seconds,
        "rpo_calculated_at": str(row.get("lastRpoCalculatedTime") or ""),
        "recovery_vm_name": str(row.get("recoveryAzureVMName") or ""),
        "health_error_count": len(errors) if isinstance(errors, list) else 0,
        "health_errors": [
            {
                "code": str((e or {}).get("errorCode") or ""),
                "summary": str((e or {}).get("errorMessage") or (e or {}).get("summaryMessage") or "")[:400],
                "level": str((e or {}).get("errorLevel") or ""),
            }
            for e in (errors if isinstance(errors, list) else [])[:10]
        ],
    }


def shape_recovery_plan(row: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(row.get("id") or "")
    vault_id = service.vault_from_child_id(plan_id)
    groups = _json(row.get("groups")) or []
    protected = 0
    for group in groups if isinstance(groups, list) else []:
        protected += len(((group or {}).get("replicationProtectedItems") or []))
    last_test = str(row.get("lastTestFailoverTime") or "")
    return {
        "id": plan_id,
        "name": str(row.get("name") or ""),
        "vault_id": vault_id,
        "vault_name": service.name_from_id(vault_id),
        "subscription_id": str(row.get("subscriptionId") or ""),
        "friendly_name": str(row.get("friendlyName") or row.get("name") or ""),
        "primary_region": str(row.get("primaryFabricFriendlyName") or ""),
        "recovery_region": str(row.get("recoveryFabricFriendlyName") or ""),
        "last_test_failover": last_test,
        "last_test_failover_age_days": service.age_days(last_test),
        "last_planned_failover": str(row.get("lastPlannedFailoverTime") or ""),
        "current_scenario": str(row.get("currentScenarioName") or ""),
        "current_scenario_status": str(row.get("currentScenarioStatus") or ""),
        "protected_item_count": protected,
        "group_count": len(groups) if isinstance(groups, list) else 0,
    }


# --------------------------------------------------------------------------- assembly
def build_estate(
    *,
    vaults: list[dict[str, Any]],
    rsv_items: list[dict[str, Any]],
    dp_instances: list[dict[str, Any]],
    rsv_jobs: list[dict[str, Any]],
    dp_jobs: list[dict[str, Any]],
    rsv_policies: list[dict[str, Any]],
    dp_policies: list[dict[str, Any]],
    replication: list[dict[str, Any]],
    recovery_plans: list[dict[str, Any]],
    live_resource_ids: set[str] | None = None,
    errors: dict[str, str] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure assembly of the unified estate from already-shaped rows.

    ``live_resource_ids`` (lowercased ARM ids that still exist) drives orphan detection: a
    protected item whose datasource is gone is still consuming vault storage and billing."""
    vault_index = {service.canonical_id(v["id"]): v for v in vaults}
    instances = [*rsv_items, *dp_instances]
    jobs = sorted([*rsv_jobs, *dp_jobs], key=lambda j: j.get("start_time") or "", reverse=True)

    # Backup vault instances have no recovery-point field in Resource Graph, so derive the
    # latest point from the newest successful backup job for that datasource.
    latest_by_datasource: dict[str, str] = {}
    for job in jobs:
        if job.get("status_bucket") != "succeeded":
            continue
        if "backup" not in str(job.get("operation") or "").lower():
            continue
        key = job.get("datasource_id") or ""
        stamp = job.get("end_time") or job.get("start_time") or ""
        if key and stamp and stamp > latest_by_datasource.get(key, ""):
            latest_by_datasource[key] = stamp

    for instance in instances:
        vault = vault_index.get(service.canonical_id(instance.get("vault_id") or ""))
        instance["vault_location"] = (vault or {}).get("location", "")
        instance["vault_redundancy"] = (vault or {}).get("redundancy", "")
        instance["offsite"] = bool(
            (vault or {}).get("redundancy", "").lower().startswith("geo")
        )
        if not instance.get("latest_recovery_point"):
            derived = latest_by_datasource.get(instance.get("datasource_id") or "")
            if derived:
                instance["latest_recovery_point"] = derived
                instance["recovery_point_age_hours"] = service.age_hours(derived)
                instance["recovery_point_source"] = "job"
        instance.setdefault("recovery_point_source", "protected_item")
        instance["orphaned"] = _is_orphaned(instance.get("datasource_id") or "", live_resource_ids)

    policies = [*rsv_policies, *dp_policies]
    policy_usage: dict[str, int] = {}
    for instance in instances:
        pid = instance.get("policy_id") or ""
        if pid:
            policy_usage[pid] = policy_usage.get(pid, 0) + 1
    for policy in policies:
        policy["in_use_count"] = policy_usage.get(policy["id"], policy.get("protected_items_count", 0) or 0)
        vault = vault_index.get(service.canonical_id(policy.get("vault_id") or ""))
        policy["vault_location"] = (vault or {}).get("location", "")

    instance_count_by_vault: dict[str, int] = {}
    for instance in instances:
        key = service.canonical_id(instance.get("vault_id") or "")
        instance_count_by_vault[key] = instance_count_by_vault.get(key, 0) + 1
    policy_count_by_vault: dict[str, int] = {}
    for policy in policies:
        key = service.canonical_id(policy.get("vault_id") or "")
        policy_count_by_vault[key] = policy_count_by_vault.get(key, 0) + 1
    replication_by_vault: dict[str, int] = {}
    for item in replication:
        key = service.canonical_id(item.get("vault_id") or "")
        replication_by_vault[key] = replication_by_vault.get(key, 0) + 1
    for vault in vaults:
        key = service.canonical_id(vault["id"])
        vault["instance_count"] = instance_count_by_vault.get(key, 0)
        vault["policy_count"] = policy_count_by_vault.get(key, 0)
        vault["replicated_item_count"] = replication_by_vault.get(key, 0)
        vault["empty"] = vault["instance_count"] == 0 and vault["replicated_item_count"] == 0

    return {
        "generated_at": service.now_iso(),
        "scope": dict(scope or {}),
        "vaults": sorted(vaults, key=lambda v: (v.get("subscription_id", ""), v.get("name", "").lower())),
        "instances": sorted(instances, key=lambda i: (i.get("friendly_name") or "").lower()),
        "policies": sorted(policies, key=lambda p: (p.get("vault_name", ""), p.get("name", "").lower())),
        "jobs": jobs,
        "replication": sorted(replication, key=lambda r: (r.get("friendly_name") or "").lower()),
        "recovery_plans": sorted(recovery_plans, key=lambda p: (p.get("friendly_name") or "").lower()),
        "errors": dict(errors or {}),
        "job_window_days": ARG_JOB_WINDOW_DAYS,
    }


# --------------------------------------------------------------------------- collection
async def _collect_uncached(
    connection: dict[str, Any],
    *,
    subscriptions: set[str],
    scope: dict[str, Any],
    detect_orphans: bool = True,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    sources = {
        "vaults": (VAULT_QUERY, shape_vault, None),
        "rsv_items": (RSV_ITEM_QUERY, shape_rsv_item, None),
        "dp_instances": (DP_INSTANCE_QUERY, shape_dp_instance, None),
        "rsv_jobs": (RSV_JOB_QUERY, shape_job, "recovery_services"),
        "dp_jobs": (DP_JOB_QUERY, shape_job, "backup"),
        "rsv_policies": (RSV_POLICY_QUERY, shape_rsv_policy, None),
        "dp_policies": (DP_POLICY_QUERY, shape_dp_policy, None),
        "replication": (ASR_ITEM_QUERY, shape_replication, None),
        "recovery_plans": (ASR_PLAN_QUERY, shape_recovery_plan, None),
    }
    names = list(sources)

    async def tracked(name: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        """Run one source query, reporting its row count the moment it lands."""
        rows, metadata, error = await service.arg_safe_detailed(
            connection, sources[name][0], subscriptions,
        )
        if progress:
            label = SOURCE_LABELS.get(name, name)
            await progress(
                "query",
                f"{label}: {error}" if error else (
                    f"Received {len(rows):,} {label} row(s)"
                    + (f" of {metadata.get('source_total'):,}" if isinstance(metadata.get("source_total"), int) else "")
                    + (" (partial)." if metadata.get("partial") else ".")
                ),
            )
        return rows, metadata, error

    if progress:
        await progress("query", f"Querying {len(names)} Resource Graph sources across "
                                f"{len(subscriptions):,} subscription(s)…")
    results = await asyncio.gather(*(tracked(name) for name in names))

    shaped: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    source_details: dict[str, dict[str, Any]] = {}
    for name, (rows, metadata, error) in zip(names, results):
        _query, shaper, kind = sources[name]
        source_details[name] = metadata
        if error:
            errors[name] = error
            shaped[name] = []
            continue
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(shaper(row, kind=kind) if kind else shaper(row))
            except (TypeError, ValueError, KeyError, AttributeError) as exc:  # noqa: BLE001
                log.debug("backup_manager: dropped malformed %s row: %s", name, exc)
        deduped: dict[str, dict[str, Any]] = {}
        for item in out:
            identity = service.canonical_id(item.get("id") or "") or service.canonical_hash(item)
            deduped.setdefault(identity, item)
        shaped[name] = list(deduped.values())
        if metadata.get("partial"):
            warnings[name] = (
                f"Retained {metadata.get('source_count', len(rows))} of "
                f"{metadata.get('source_total', 'an unknown total')} row(s); "
                f"{metadata.get('failed_batches', 0)} subscription batch(es) failed."
            )

    live_ids: set[str] | None = None
    if detect_orphans:
        datasources = {
            i.get("datasource_id") for i in [*shaped["rsv_items"], *shaped["dp_instances"]] if i.get("datasource_id")
        }
        if progress:
            await progress("orphans", f"Checking whether {len(datasources):,} protected datasource(s) still exist…")
        live_ids = await _live_resource_ids(connection, subscriptions, datasources)
        if progress:
            await progress(
                "orphans",
                "Orphan detection unavailable — the resource sweep failed, so no item is reported orphaned."
                if live_ids is None else f"Resolved {len(live_ids):,} live resource id(s).",
            )

    estate = build_estate(
        vaults=shaped["vaults"],
        rsv_items=shaped["rsv_items"],
        dp_instances=shaped["dp_instances"],
        rsv_jobs=shaped["rsv_jobs"],
        dp_jobs=shaped["dp_jobs"],
        rsv_policies=shaped["rsv_policies"],
        dp_policies=shaped["dp_policies"],
        replication=shaped["replication"],
        recovery_plans=shaped["recovery_plans"],
        live_resource_ids=live_ids,
        errors=errors,
        scope=scope,
    )
    estate["warnings"] = warnings
    estate["source_details"] = source_details
    return estate


async def _live_resource_ids(
    connection: dict[str, Any], subscriptions: set[str], datasource_ids: set[str],
) -> set[str] | None:
    """Which protected datasources still exist. ``None`` means the check could not run, in
    which case orphan detection stays off rather than reporting false positives."""
    if not datasource_ids:
        return set()
    rows, error = await service.arg_safe(
        connection, "resources | project id = tolower(id)", subscriptions, max_rows=20000,
    )
    if error:
        return None
    return {str(r.get("id") or "") for r in rows}


async def collect_estate(
    connection: dict[str, Any],
    *,
    tenant_id: str = "",
    workload_id: str | None = None,
    subscription_id: str | None = None,
    management_group_id: str | None = None,
    detect_orphans: bool = True,
    force: bool = False,
    progress: ProgressFn | None = None,
    resolved_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cached, scope-resolved estate sweep."""
    if progress:
        await progress("scope", "Resolving the selected scope to subscriptions…")
    resolved = dict(resolved_scope or await service.resolve_scope(
        connection, workload_id=workload_id, subscription_id=subscription_id,
        management_group_id=management_group_id,
    ))
    subscriptions = set(resolved["subscriptions"])
    if progress:
        await progress("scope", f"Scope resolved to {len(subscriptions):,} subscription(s).")
    scope = {
        **resolved,
        "workload_id": workload_id or "",
        "subscription_id": subscription_id or "",
        "management_group_id": management_group_id or "",
        "connection_id": str(connection.get("id") or ""),
    }
    key = inventory_cache.scope_key(
        "estate", connection, tenant_id=tenant_id, workload_id=workload_id,
        subscription_id=subscription_id, management_group_id=management_group_id,
        dimensions=(detect_orphans,),
    )
    if force:
        await inventory_cache.invalidate(kinds={"estate"}, tenant_id=tenant_id or "default")

    async def load() -> dict[str, Any]:
        return await _collect_uncached(
            connection, subscriptions=subscriptions, scope=scope,
            detect_orphans=detect_orphans, progress=progress,
        )

    return await inventory_cache.get_or_create(key, load)


# --------------------------------------------------------------------------- ARM enrichment
async def enrich_vaults(
    connection: dict[str, Any], vaults: list[dict[str, Any]], *, limit: int = 6,
    progress: ProgressFn | None = None,
) -> list[dict[str, Any]]:
    """Fill in the vault facts Resource Graph does not expose: soft-delete/redundancy from the
    Recovery Services backup config endpoints, Resource Guard (MUA) association, and whether
    vault diagnostics ship Backup Reports to Log Analytics.

    Bounded fan-out; a failure on one vault degrades that vault only."""
    if not vaults:
        return vaults
    token = await service.token_for(connection)

    async def enrich(vault: dict[str, Any]) -> None:
        vault_id = vault["id"]
        try:
            if vault["kind"] == "recovery_services":
                config, _s, config_error = await service.arm_get_with(
                    token, f"{vault_id}/backupconfig/vaultconfig", service.RSV_VAULT_CONFIG_API,
                )
                if config:
                    props = service.as_dict(config.get("properties"))
                    state = str(props.get("softDeleteFeatureState") or "")
                    enhanced = str(props.get("enhancedSecurityState") or "")
                    vault["soft_delete_state"] = state or vault.get("soft_delete_state") or ""
                    vault["soft_delete_enhanced"] = enhanced.lower() == "enabled"
                    retention = props.get("softDeleteRetentionPeriodInDays")
                    if retention:
                        vault["soft_delete_retention_days"] = retention
                    vault["resource_guard_operations"] = [
                        str(x) for x in service.as_list(props.get("resourceGuardOperationRequests"))
                    ]
                elif config_error:
                    vault["enrichment_error"] = config_error

                storage, _s2, _e2 = await service.arm_get_with(
                    token, f"{vault_id}/backupstorageconfig/vaultstorageconfig", service.RSV_STORAGE_CONFIG_API,
                )
                if storage:
                    props = service.as_dict(storage.get("properties"))
                    vault["redundancy"] = str(props.get("storageModelType") or props.get("storageType") or vault.get("redundancy") or "")
                    vault["cross_region_restore"] = "Enabled" if props.get("crossRegionRestoreFlag") else "Disabled"
                    vault["redundancy_locked"] = str(props.get("storageTypeState") or "").lower() == "locked"

                proxies, _s3, _e3 = await service.arm_get_with(
                    token, f"{vault_id}/backupResourceGuardProxies", service.RSV_GUARD_PROXY_API,
                )
                guards = service.as_list((proxies or {}).get("value"))
                if guards:
                    vault["mua_enabled"] = True
                    vault["mua_resource_guard_id"] = str(
                        service.as_dict(service.as_dict(guards[0]).get("properties")).get("resourceGuardResourceId") or ""
                    )
                elif vault.get("mua_enabled") is None:
                    vault["mua_enabled"] = False

                # The alertsConfiguration sub-resource is not reliably queryable across API
                # versions; the vault resource itself is authoritative, so only fill a gap.
                if not vault.get("monitor_alerts"):
                    body, _s4, _e4 = await service.arm_get_with(token, vault_id, service.RSV_API)
                    monitoring = service.as_dict(service.as_dict((body or {}).get("properties")).get("monitoringSettings"))
                    azure_monitor = service.as_dict(monitoring.get("azureMonitorAlertSettings"))
                    vault["monitor_alerts"] = str(azure_monitor.get("alertsForAllJobFailures") or "")
            else:
                # Backup vaults carry every security setting on the vault resource itself, so
                # only the Resource Guard association needs a second read.
                body, _s, _e = await service.arm_get_with(token, vault_id, service.DP_API)
                if body:
                    props = service.as_dict(body.get("properties"))
                    security = service.as_dict(props.get("securitySettings"))
                    soft = service.as_dict(security.get("softDeleteSettings"))
                    vault["soft_delete_state"] = str(soft.get("state") or vault.get("soft_delete_state") or "")
                    if soft.get("retentionDurationInDays"):
                        vault["soft_delete_retention_days"] = soft.get("retentionDurationInDays")
                    vault["immutability_state"] = str(
                        service.as_dict(security.get("immutabilitySettings")).get("state") or vault.get("immutability_state") or ""
                    )
                    guard = service.as_dict(props.get("resourceGuardOperationRequests"))
                    resource_guard = service.as_dict(security.get("resourceGuardSettings") if isinstance(security, dict) else {})
                    vault["mua_enabled"] = bool(guard) or bool(resource_guard.get("resourceGuardResourceId"))
                    vault["mua_resource_guard_id"] = str(resource_guard.get("resourceGuardResourceId") or "")
                    monitoring = service.as_dict(props.get("monitoringSettings"))
                    azure_monitor = service.as_dict(monitoring.get("azureMonitorAlertSettings"))
                    vault["monitor_alerts"] = str(azure_monitor.get("alertsForAllJobFailures") or vault.get("monitor_alerts") or "")

            diagnostics, _s5, _e5 = await service.arm_get_with(
                token, f"{vault_id}/providers/microsoft.insights/diagnosticSettings", service.DIAG_API,
            )
            workspaces: list[str] = []
            report_categories = False
            for setting in service.as_list((diagnostics or {}).get("value")):
                props = service.as_dict(service.as_dict(setting).get("properties"))
                workspace = str(props.get("workspaceId") or "")
                if workspace:
                    workspaces.append(workspace)
                for group in service.as_list(props.get("logs")):
                    entry = service.as_dict(group)
                    category = str(entry.get("category") or entry.get("categoryGroup") or "")
                    if entry.get("enabled") and category:
                        report_categories = True
            vault["diagnostics_workspaces"] = sorted(set(workspaces))
            vault["diagnostics_enabled"] = bool(workspaces and report_categories)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:  # noqa: BLE001
            vault["enrichment_error"] = service.safe_error(str(exc))

    done = 0
    total = len(vaults)

    async def enrich_reporting(vault: dict[str, Any]) -> None:
        nonlocal done
        await enrich(vault)
        done += 1
        if progress:
            await progress("vaults", f"Read vault configuration {done}/{total} — {vault.get('name', '')}.")

    if progress:
        await progress("vaults", f"Reading soft delete, redundancy, MUA and diagnostics for {total} vault(s)…")
    await service.bounded_gather([lambda v=v: enrich_reporting(v) for v in vaults], limit=limit)
    return vaults
