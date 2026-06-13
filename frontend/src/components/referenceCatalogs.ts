// Shared catalogs for the Telemetry and Backup/DR reference editors — convenience layers
// for "add from catalog" (telemetry diagnostic categories) and the fixed Backup/DR check
// set. Purely client-side; the backend re-sanitizes everything on save (telemetry kinds/
// groups are validated, backup/DR drops unknown check keys).

import { CATEGORY_COLOR, KNOWN_ARM_TYPES } from "./ambaCatalog";

export { CATEGORY_COLOR, KNOWN_ARM_TYPES };

// ---------------------------------------------------------------- Telemetry
export const TELEMETRY_KINDS = ["log", "metric"] as const;
export const TELEMETRY_GROUPS = ["audit", "security", "operational", "performance"] as const;
export const TELEMETRY_GROUP_COLOR: Record<string, string> = {
  audit: "#7c3aed",
  security: "#b91c1c",
  operational: "#2563eb",
  performance: "#ca8a04",
};

export type TelemetryCatalogCategory = {
  key: string;
  name: string;
  kind: "log" | "metric";
  group: "audit" | "security" | "operational" | "performance";
  why?: string;
};

const TC = (
  key: string,
  name: string,
  group: TelemetryCatalogCategory["group"],
  kind: TelemetryCatalogCategory["kind"] = "log",
  why = "",
): TelemetryCatalogCategory => ({ key, name, group, kind, why });

const ALL_METRICS = TC("AllMetrics", "All platform metrics", "performance", "metric", "Platform metrics power dashboards, alerts and capacity analysis.");

// arm_type (lowercase) -> candidate diagnostic categories (the modern Azure category names).
export const TELEMETRY_CATEGORY_CATALOG: Record<string, TelemetryCatalogCategory[]> = {
  "microsoft.keyvault/vaults": [
    TC("AuditEvent", "Audit events", "audit", "log", "Records every access to secrets/keys/certs."),
    TC("AzurePolicyEvaluationDetails", "Policy evaluation", "operational"),
    ALL_METRICS,
  ],
  "microsoft.web/sites": [
    TC("AppServiceHTTPLogs", "HTTP logs", "operational", "log", "Request-level logs for 5xx triage."),
    TC("AppServiceConsoleLogs", "Console logs", "operational"),
    TC("AppServiceAppLogs", "Application logs", "operational"),
    TC("AppServiceAuditLogs", "Audit logs", "audit", "log", "Publishing/FTP access audit."),
    TC("AppServiceIPSecAuditLogs", "IPSec audit logs", "security"),
    ALL_METRICS,
  ],
  "microsoft.web/sites/functions": [
    TC("FunctionAppLogs", "Function logs", "operational", "log", "Per-invocation execution logs."),
    TC("AppServiceHTTPLogs", "HTTP logs", "operational"),
    TC("AppServiceAuditLogs", "Audit logs", "audit"),
    ALL_METRICS,
  ],
  "microsoft.sql/servers/databases": [
    TC("SQLSecurityAuditEvents", "Security audit events", "security", "log", "Database access/auditing trail."),
    TC("Errors", "Errors", "operational"),
    TC("QueryStoreRuntimeStatistics", "Query Store runtime stats", "performance"),
    TC("Timeouts", "Timeouts", "operational"),
    TC("Deadlocks", "Deadlocks", "operational"),
    ALL_METRICS,
  ],
  "microsoft.sql/servers": [
    TC("SQLSecurityAuditEvents", "SQL security audit", "security"),
    TC("DevOpsOperationsAudit", "DevOps operations audit", "audit"),
    ALL_METRICS,
  ],
  "microsoft.storage/storageaccounts": [
    TC("StorageRead", "Read transactions", "operational"),
    TC("StorageWrite", "Write transactions", "operational"),
    TC("StorageDelete", "Delete transactions", "audit", "log", "Delete operations — data-loss investigations."),
    ALL_METRICS,
  ],
  "microsoft.network/networksecuritygroups": [
    TC("NetworkSecurityGroupEvent", "NSG events", "security"),
    TC("NetworkSecurityGroupRuleCounter", "NSG rule counter", "security"),
  ],
  "microsoft.network/frontdoors": [
    TC("FrontdoorAccessLog", "Access log", "operational"),
    TC("FrontdoorWebApplicationFirewallLog", "WAF log", "security"),
    ALL_METRICS,
  ],
  "microsoft.cdn/profiles": [
    TC("FrontDoorAccessLog", "Access log", "operational"),
    TC("FrontDoorWebApplicationFirewallLog", "WAF log", "security"),
    TC("FrontDoorHealthProbeLog", "Health probe log", "operational"),
    ALL_METRICS,
  ],
  "microsoft.containerservice/managedclusters": [
    TC("kube-apiserver", "API server", "operational"),
    TC("kube-audit", "Kubernetes audit", "audit"),
    TC("kube-audit-admin", "Kubernetes audit (admin)", "audit"),
    TC("kube-controller-manager", "Controller manager", "operational"),
    TC("guard", "Guard (AAD)", "security"),
    ALL_METRICS,
  ],
  "microsoft.apimanagement/service": [
    TC("GatewayLogs", "Gateway logs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.network/applicationgateways": [
    TC("ApplicationGatewayAccessLog", "Access log", "operational"),
    TC("ApplicationGatewayPerformanceLog", "Performance log", "performance"),
    TC("ApplicationGatewayFirewallLog", "Firewall (WAF) log", "security"),
    ALL_METRICS,
  ],
  "microsoft.documentdb/databaseaccounts": [
    TC("DataPlaneRequests", "Data-plane requests", "operational"),
    TC("QueryRuntimeStatistics", "Query runtime stats", "performance"),
    TC("ControlPlaneRequests", "Control-plane requests", "audit"),
    ALL_METRICS,
  ],
  "microsoft.cache/redis": [
    TC("ConnectedClientList", "Connected client list", "operational"),
    ALL_METRICS,
  ],
  "microsoft.servicebus/namespaces": [
    TC("OperationalLogs", "Operational logs", "operational"),
    TC("RuntimeAuditLogs", "Runtime audit logs", "audit"),
    TC("VNetAndIPFilteringLogs", "VNet/IP filtering logs", "security"),
    ALL_METRICS,
  ],
  "microsoft.eventhub/namespaces": [
    TC("OperationalLogs", "Operational logs", "operational"),
    TC("RuntimeAuditLogs", "Runtime audit logs", "audit"),
    TC("ArchiveLogs", "Archive (capture) logs", "operational"),
    TC("AutoScaleLogs", "Auto-scale logs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.eventgrid/topics": [
    TC("PublishFailures", "Publish failures", "operational"),
    TC("DeliveryFailures", "Delivery failures", "operational"),
    ALL_METRICS,
  ],
  "microsoft.logic/workflows": [
    TC("WorkflowRuntime", "Workflow runtime", "operational"),
    ALL_METRICS,
  ],
  "microsoft.network/azurefirewalls": [
    TC("AZFWApplicationRule", "Application rule logs", "security"),
    TC("AZFWNetworkRule", "Network rule logs", "security"),
    TC("AZFWDnsQuery", "DNS proxy logs", "security"),
    TC("AZFWThreatIntel", "Threat intel logs", "security"),
    ALL_METRICS,
  ],
  "microsoft.network/publicipaddresses": [
    TC("DDoSProtectionNotifications", "DDoS notifications", "security"),
    TC("DDoSMitigationFlowLogs", "DDoS mitigation flow logs", "security"),
    TC("DDoSMitigationReports", "DDoS mitigation reports", "security"),
    ALL_METRICS,
  ],
  "microsoft.cognitiveservices/accounts": [
    TC("Audit", "Audit", "audit"),
    TC("RequestResponse", "Request/response", "operational"),
    TC("Trace", "Trace", "operational"),
    ALL_METRICS,
  ],
  "microsoft.search/searchservices": [
    TC("OperationLogs", "Operation logs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.machinelearningservices/workspaces": [
    TC("AmlComputeClusterEvent", "Compute cluster events", "operational"),
    TC("AmlComputeJobEvent", "Compute job events", "operational"),
    TC("AmlRunStatusChangedEvent", "Run status changes", "operational"),
    ALL_METRICS,
  ],
  "microsoft.datafactory/factories": [
    TC("PipelineRuns", "Pipeline runs", "operational"),
    TC("TriggerRuns", "Trigger runs", "operational"),
    TC("ActivityRuns", "Activity runs", "operational"),
    TC("SandboxPipelineRuns", "Sandbox pipeline runs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.synapse/workspaces": [
    TC("SQLSecurityAuditEvents", "SQL security audit", "security"),
    TC("GatewayApiRequests", "Gateway API requests", "operational"),
    TC("BuiltinSqlReqsEnded", "Built-in SQL requests", "performance"),
    TC("IntegrationPipelineRuns", "Integration pipeline runs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.dbforpostgresql/flexibleservers": [
    TC("PostgreSQLLogs", "PostgreSQL server logs", "operational"),
    TC("PostgreSQLFlexSessions", "Sessions", "performance"),
    TC("PostgreSQLFlexQueryStoreRuntime", "Query Store runtime", "performance"),
    ALL_METRICS,
  ],
  "microsoft.dbformysql/flexibleservers": [
    TC("MySqlSlowLogs", "Slow query logs", "performance"),
    TC("MySqlAuditLogs", "Audit logs", "audit"),
    ALL_METRICS,
  ],
  "microsoft.network/virtualnetworks": [
    TC("VMProtectionAlerts", "VM protection alerts", "security"),
    ALL_METRICS,
  ],
  "microsoft.network/loadbalancers": [
    TC("LoadBalancerHealthEvent", "Health events", "operational"),
    ALL_METRICS,
  ],
  "microsoft.containerregistry/registries": [
    TC("ContainerRegistryRepositoryEvents", "Repository events", "audit"),
    TC("ContainerRegistryLoginEvents", "Login events", "security"),
    ALL_METRICS,
  ],
  "microsoft.app/containerapps": [
    TC("ContainerAppConsoleLogs", "Console logs", "operational"),
    TC("ContainerAppSystemLogs", "System logs", "operational"),
    ALL_METRICS,
  ],
  "microsoft.operationalinsights/workspaces": [
    TC("Audit", "Audit (LAQueryLogs/access)", "audit"),
    ALL_METRICS,
  ],
};

export function telemetryCatalogFor(armType: string): TelemetryCatalogCategory[] {
  return TELEMETRY_CATEGORY_CATALOG[(armType || "").toLowerCase()] || [];
}

// ---------------------------------------------------------------- Backup/DR
export type BackupDrCheckDef = { key: string; label: string; why: string };

// The fixed Backup/DR check set (mirrors backend CHECK_META). The backend drops any key
// not in this set, so the editor offers exactly these as toggles.
export const BACKUPDR_CHECKS: BackupDrCheckDef[] = [
  { key: "backup_enabled", label: "Backup Enabled", why: "Resource is not protected by any backup — total data-loss exposure on failure." },
  { key: "policy", label: "Policy", why: "No backup policy attached, so schedule/retention are undefined." },
  { key: "retention", label: "Retention", why: "Retention is below the recommended minimum for this workload tier." },
  { key: "last_job", label: "Last Job", why: "No successful backup within the SLA window — recovery point may be stale or missing." },
  { key: "geo_redundancy", label: "Geo-Redundancy", why: "No geo/offsite redundancy — a regional outage loses the only copy." },
  { key: "offsite_region", label: "Backup Region", why: "Backup destination is in the same region as the resource — a regional outage takes both down." },
  { key: "dr_pair", label: "DR Pair", why: "No disaster-recovery replication pair configured." },
  { key: "encryption", label: "Encryption", why: "Encryption is not configured to the required standard (CMK where mandated)." },
  { key: "soft_delete", label: "Soft-Delete", why: "Soft-delete / purge protection is off — backups or secrets can be permanently deleted." },
  { key: "restore_test", label: "Last Restore Test", why: "No recent restore/failover test — recoverability is unproven." },
  { key: "pitr", label: "Point-in-Time Restore", why: "Continuous/point-in-time backup is not enabled — only coarse periodic restore points exist." },
  { key: "persistence", label: "Persistence", why: "Data persistence (RDB/AOF) is off — a restart or failure loses all cached data." },
  { key: "geo_dr_pair", label: "Geo-DR Pairing", why: "No Geo-DR alias/paired namespace — a regional outage loses the messaging entity and its metadata." },
];

export const BACKUPDR_CHECK_BY_KEY: Record<string, BackupDrCheckDef> = Object.fromEntries(
  BACKUPDR_CHECKS.map((c) => [c.key, c]),
);
