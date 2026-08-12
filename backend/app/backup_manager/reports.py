"""Log Analytics "Backup Reports" integration — the long-horizon half of Backup Manager.

Resource Graph only retains a rolling window of backup jobs, so trend, SLA attainment, and
real consumed-storage figures are impossible from ARG alone.  Azure Backup solves this by
emitting the ``AddonAzureBackup*`` and ``CoreAzureBackup`` tables to a Log Analytics workspace
via vault diagnostic settings.

This module queries those tables directly over the Log Analytics REST API (a distinct token
audience from ARM), and — critically — tells the caller *why* data is unavailable when it is:
no workspace configured, vault diagnostics not enabled, or a connection type that cannot mint
a Log Analytics token.  Silent emptiness would be indistinguishable from healthy backups.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.backup_manager import service

log = logging.getLogger("app.backup_manager.reports")

LA_BASE = "https://api.loganalytics.io"
MAX_ROWS = 5000

# Diagnostic log categories a vault must emit for these tables to populate.
REQUIRED_CATEGORIES = (
    "AddonAzureBackupJobs",
    "AddonAzureBackupPolicy",
    "AddonAzureBackupProtectedInstance",
    "AddonAzureBackupStorage",
    "CoreAzureBackup",
)


class ReportsUnavailable(RuntimeError):
    """Raised when backup reporting cannot run, carrying an actionable reason."""

    def __init__(self, reason: str, *, remedy: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.remedy = remedy


def workspace_for(connection: dict[str, Any], estate: dict[str, Any] | None = None) -> str:
    """Preferred workspace: the connection's admin-approved one, else one the vaults already
    ship diagnostics to (so reporting works without extra configuration)."""
    configured = str(connection.get("log_analytics_workspace_id") or "").strip()
    if configured:
        return configured
    for vault in (estate or {}).get("vaults", []) or []:
        for workspace in vault.get("diagnostics_workspaces") or []:
            if workspace:
                return str(workspace)
    return ""


def workspaces_for(connection: dict[str, Any], estate: dict[str, Any] | None = None) -> list[str]:
    """Every distinct in-scope reporting workspace, configured workspace first."""
    values: list[str] = []
    configured = str(connection.get("log_analytics_workspace_id") or "").strip()
    if configured:
        values.append(configured)
    for vault in (estate or {}).get("vaults", []) or []:
        values.extend(str(value).strip() for value in vault.get("diagnostics_workspaces") or [] if value)
    return list(dict.fromkeys(value for value in values if value))


def _workspace_guid(workspace: str) -> str:
    """The API path takes a workspace *customer id*; ARM ids are resolved separately."""
    text = str(workspace or "").strip()
    return service.name_from_id(text) if text.startswith("/subscriptions/") else text


async def resolve_workspace_guid(connection: dict[str, Any], workspace: str) -> tuple[str, str]:
    """Return ``(guid, error)``. An ARM workspace id is resolved to its customerId via ARM."""
    text = str(workspace or "").strip()
    if not text:
        return "", "No Log Analytics workspace is configured."
    if not text.startswith("/subscriptions/"):
        return text, ""
    body, _status, error = await service.arm_get(connection, text, "2022-10-01")
    if error or not body:
        return "", error or "The configured Log Analytics workspace could not be read."
    guid = str(service.as_dict(body.get("properties")).get("customerId") or "")
    return (guid, "") if guid else ("", "The workspace did not report a customer id.")


async def query(
    connection: dict[str, Any], workspace: str, kql: str, *, timespan: str = "P30D",
) -> list[dict[str, Any]]:
    """Run one Log Analytics query and return row dicts. Raises :class:`ReportsUnavailable`."""
    from app.azure.credentials import get_log_analytics_token

    guid, error = await resolve_workspace_guid(connection, workspace)
    if error or not guid:
        raise ReportsUnavailable(
            error or "No Log Analytics workspace is configured.",
            remedy="Set a Log Analytics workspace on the Azure connection, or enable vault diagnostics.",
        )
    token, token_error = await get_log_analytics_token(connection)
    if not token:
        raise ReportsUnavailable(
            service.safe_error(token_error or "Could not acquire a Log Analytics token."),
            remedy="Use a service-principal or managed-identity connection and grant it Log Analytics Reader on the workspace.",
        )
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{LA_BASE}/v1/workspaces/{guid}/query",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": kql, "timespan": timespan},
            )
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise ReportsUnavailable(service.safe_error(f"Log Analytics request error: {exc}")) from exc
    if resp.status_code != 200:
        try:
            detail = (resp.json().get("error") or {}).get("message") or resp.text
        except (ValueError, AttributeError):
            detail = resp.text
        raise ReportsUnavailable(service.safe_error(f"Log Analytics {resp.status_code}: {detail}"))
    try:
        payload = resp.json()
    except (ValueError, AttributeError) as exc:
        raise ReportsUnavailable("Log Analytics returned an unreadable response.") from exc

    tables = service.as_list(payload.get("tables"))
    if not tables:
        return []
    table = service.as_dict(tables[0])
    columns = [str(service.as_dict(c).get("name") or "") for c in service.as_list(table.get("columns"))]
    rows: list[dict[str, Any]] = []
    for raw in service.as_list(table.get("rows"))[:MAX_ROWS]:
        values = raw if isinstance(raw, list) else []
        rows.append({columns[i]: values[i] for i in range(min(len(columns), len(values)))})
    return rows


# --------------------------------------------------------------------------- queries
JOB_TREND_KQL = """
AddonAzureBackupJobs
| where JobOperation == "Backup"
| summarize Total = count(),
            Failed = countif(JobStatus == "Failed"),
            Succeeded = countif(JobStatus == "Completed")
    by bin(TimeGenerated, 1d)
| order by TimeGenerated asc
""".strip()

STORAGE_KQL = """
AddonAzureBackupStorage
| where isnotempty(StorageConsumedInMBs)
| summarize arg_max(TimeGenerated, StorageConsumedInMBs, StorageType) by BackupItemUniqueId
| project BackupItemUniqueId, StorageType, StorageConsumedInMBs, TimeGenerated
""".strip()

PROTECTED_INSTANCE_KQL = """
AddonAzureBackupProtectedInstance
| summarize arg_max(TimeGenerated, ProtectedInstanceCount, BackupItemUniqueId) by VaultUniqueId
| project VaultUniqueId, ProtectedInstanceCount, TimeGenerated
""".strip()

FAILURE_HISTORY_KQL = """
AddonAzureBackupJobs
| where JobStatus == "Failed"
| summarize Failures = count(), LastSeen = max(TimeGenerated)
    by JobFailureCode, BackupItemUniqueId
| summarize Failures = sum(Failures), Items = dcount(BackupItemUniqueId), LastSeen = max(LastSeen)
    by JobFailureCode
| order by Failures desc
""".strip()

SLA_KQL = """
AddonAzureBackupJobs
| where JobOperation == "Backup"
| summarize Total = count(), Succeeded = countif(JobStatus == "Completed") by BackupItemUniqueId
| extend SuccessRate = todouble(Succeeded) * 100 / todouble(Total)
| order by SuccessRate asc
""".strip()


async def build_report(
    connection: dict[str, Any], estate: dict[str, Any], *, days: int = 30,
) -> dict[str, Any]:
    """Long-horizon backup report. Always returns a shaped response; ``available`` states
    whether real data was retrieved and ``reason``/``remedy`` explain any shortfall."""
    workspaces = workspaces_for(connection, estate)
    workspace = workspaces[0] if workspaces else ""
    timespan = f"P{max(1, min(int(days), 180))}D"
    vaults_with_diagnostics = sum(1 for v in estate.get("vaults", []) if v.get("diagnostics_enabled"))
    total_vaults = len(estate.get("vaults", []) or [])
    base = {
        "available": False,
        "workspace": workspace,
        "workspaces": workspaces,
        "workspaces_total": len(workspaces),
        "workspaces_succeeded": 0,
        "workspaces_failed": 0,
        "partial": False,
        "days": days,
        "vaults_total": total_vaults,
        "vaults_with_diagnostics": vaults_with_diagnostics,
        "required_categories": list(REQUIRED_CATEGORIES),
        "job_trend": [],
        "storage": [],
        "failure_history": [],
        "sla": [],
        "storage_by_item": {},
        "reason": "",
        "remedy": "",
    }
    if not workspace:
        base["reason"] = "No Log Analytics workspace is configured for this connection and no vault ships diagnostics."
        base["remedy"] = "Enable vault diagnostic settings from the Vaults tab, then re-run this report."
        return base

    semaphore = asyncio.Semaphore(3)

    async def collect(current: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, str]:
        async with semaphore:
            try:
                return (
                    current,
                    await query(connection, current, JOB_TREND_KQL, timespan=timespan),
                    await query(connection, current, STORAGE_KQL, timespan=timespan),
                    await query(connection, current, FAILURE_HISTORY_KQL, timespan=timespan),
                    await query(connection, current, SLA_KQL, timespan=timespan),
                    "", "",
                )
            except ReportsUnavailable as exc:
                return current, [], [], [], [], exc.reason, exc.remedy

    results = await asyncio.gather(*(collect(value) for value in workspaces))
    trend: list[dict[str, Any]] = []
    storage: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sla: list[dict[str, Any]] = []
    report_errors: list[str] = []
    remedies: list[str] = []
    for current, current_trend, current_storage, current_failures, current_sla, error, remedy in results:
        if error:
            report_errors.append(f"{service.name_from_id(current)}: {error}")
            if remedy:
                remedies.append(remedy)
            continue
        base["workspaces_succeeded"] += 1
        trend.extend(current_trend)
        storage.extend(current_storage)
        failures.extend(current_failures)
        sla.extend(current_sla)
    base["workspaces_failed"] = len(report_errors)
    base["partial"] = bool(report_errors)
    if not base["workspaces_succeeded"]:
        base["reason"] = "; ".join(report_errors)[:1200]
        base["remedy"] = "; ".join(dict.fromkeys(remedies))[:800] or "Grant the connection Log Analytics Reader on the workspaces."
        return base

    # Opaque BackupItemUniqueId values are retained only when they match an in-scope item;
    # ARM-shaped ids are additionally constrained to the resolved subscription boundary.
    subscriptions = {str(value).lower() for value in estate.get("scope", {}).get("subscriptions") or []}
    names = {str(item.get("friendly_name") or "").lower() for item in estate.get("instances", []) if item.get("friendly_name")}

    def in_scope_item(row: dict[str, Any]) -> bool:
        value = str(row.get("BackupItemUniqueId") or "")
        lower = value.lower()
        if "/subscriptions/" in lower:
            subscription = service.subscription_from_id(value).lower()
            return not subscriptions or subscription in subscriptions
        return any(name and name in lower for name in names)

    storage = [row for row in storage if in_scope_item(row)]
    sla = [row for row in sla if in_scope_item(row)]
    storage = list({str(row.get("BackupItemUniqueId") or ""): row for row in storage}.values())
    sla = list({str(row.get("BackupItemUniqueId") or ""): row for row in sla}.values())
    management_group_scope = estate.get("scope", {}).get("scope_kind") == "management_group"
    if management_group_scope:
        # These two queries are workspace-level aggregates and cannot be proven scope-bound
        # when a workspace is shared. Omit rather than leak unrelated subscriptions.
        trend = []
        failures = []
        base["partial"] = True
        report_errors.append("Workspace-level trend/failure aggregates were omitted because they are not resource-scoped.")

    storage_by_item: dict[str, float] = {}
    for row in storage:
        key = str(row.get("BackupItemUniqueId") or "")
        try:
            storage_by_item[key] = float(row.get("StorageConsumedInMBs") or 0) / 1024.0
        except (TypeError, ValueError):
            continue

    base.update({
        "available": True,
        "reason": "; ".join(report_errors)[:1200],
        "job_trend": [
            {
                "date": str(r.get("TimeGenerated") or "")[:10],
                "total": int(r.get("Total") or 0),
                "failed": int(r.get("Failed") or 0),
                "succeeded": int(r.get("Succeeded") or 0),
            }
            for r in trend
        ],
        "storage": [
            {
                "item": str(r.get("BackupItemUniqueId") or ""),
                "storage_type": str(r.get("StorageType") or ""),
                "consumed_gb": round(float(r.get("StorageConsumedInMBs") or 0) / 1024.0, 2),
            }
            for r in storage[:500]
        ],
        "failure_history": [
            {
                "error_code": str(r.get("JobFailureCode") or "Unknown"),
                "failures": int(r.get("Failures") or 0),
                "items": int(r.get("Items") or 0),
                "last_seen": str(r.get("LastSeen") or ""),
            }
            for r in failures[:100]
        ],
        "sla": [
            {
                "item": str(r.get("BackupItemUniqueId") or ""),
                "total": int(r.get("Total") or 0),
                "succeeded": int(r.get("Succeeded") or 0),
                "success_rate": round(float(r.get("SuccessRate") or 0), 1),
            }
            for r in sla[:500]
        ],
        "storage_by_item": storage_by_item,
        "total_consumed_gb": round(sum(storage_by_item.values()), 1),
    })
    return base


def diagnostic_setting_body(workspace_arm_id: str, *, categories: list[str] | None = None) -> dict[str, Any]:
    """Diagnostic-setting body that enables exactly the Backup Reports categories.

    ``Dedicated`` (resource-specific) destination mode is required: the ``AddonAzureBackup*``
    tables only exist in resource-specific mode. Vaults expose a single ``AllMetrics`` metric
    category — naming an individual metric category is rejected by ARM.
    """
    wanted = [c for c in (categories or REQUIRED_CATEGORIES) if c in REQUIRED_CATEGORIES]
    return {
        "properties": {
            "workspaceId": workspace_arm_id,
            "logAnalyticsDestinationType": "Dedicated",
            "logs": [{"category": category, "enabled": True} for category in wanted],
            "metrics": [{"category": "AllMetrics", "enabled": True}],
        }
    }
