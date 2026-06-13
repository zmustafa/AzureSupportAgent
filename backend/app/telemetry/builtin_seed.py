"""Built-in reference seed: recommended diagnostic-setting categories per resource type.

Derived from Azure Monitor "recommended diagnostic settings" guidance. This is the
FALLBACK/seed only — admins edit a persisted, versioned copy in the JSON registry
(reference.py). Categories use Azure's diagnostic category names (logs) and the
``AllMetrics`` pseudo-category for platform metrics.

Each type entry:
    display             human label
    categories          [{key, name, kind (log|metric), group (audit|security|
                          operational|performance), recommended (bool), why}]
    note                short guidance shown in the UI

A category flagged ``group`` = audit|security is treated as high-importance: when it's in
the recommended set but not enabled, the resource is flagged amber with an explicit
audit/security warning."""
from __future__ import annotations

from typing import Any

BUILTIN_SEED_VERSION = 2


def _c(key: str, name: str, *, kind: str = "log", group: str = "operational", why: str = "") -> dict[str, Any]:
    return {"key": key, "name": name, "kind": kind, "group": group, "recommended": True, "why": why}


_ALL_METRICS = _c("AllMetrics", "All platform metrics", kind="metric", group="performance",
                  why="Platform metrics power dashboards, alerts and capacity analysis.")

BUILTIN_TYPES: dict[str, dict[str, Any]] = {
    "microsoft.keyvault/vaults": {
        "display": "Key Vault",
        "note": "AuditEvent is essential — it records every secret/key/cert access.",
        "categories": [
            _c("AuditEvent", "Audit events", group="audit",
               why="Records every access to secrets/keys/certs — the primary forensic trail for a vault."),
            _c("AzurePolicyEvaluationDetails", "Policy evaluation", group="operational",
               why="Shows policy compliance evaluations against the vault."),
            _ALL_METRICS,
        ],
    },
    "microsoft.web/sites": {
        "display": "App Service",
        "note": "HTTP + console + audit logs are the baseline for incident triage.",
        "categories": [
            _c("AppServiceHTTPLogs", "HTTP logs", group="operational",
               why="Request-level logs — the first thing you read during a 5xx incident."),
            _c("AppServiceConsoleLogs", "Console logs", group="operational",
               why="stdout/stderr from the app process."),
            _c("AppServiceAppLogs", "Application logs", group="operational",
               why="Application-emitted logs."),
            _c("AppServiceAuditLogs", "Audit logs", group="audit",
               why="Publishing/FTP access audit — who deployed/changed the app."),
            _c("AppServiceIPSecAuditLogs", "IPSec audit logs", group="security",
               why="Access-restriction (IP) audit events."),
            _ALL_METRICS,
        ],
    },
    "microsoft.sql/servers/databases": {
        "display": "SQL Database",
        "note": "Security audit + errors are mandatory for a data tier.",
        "categories": [
            _c("SQLSecurityAuditEvents", "Security audit events", group="security",
               why="Database access/auditing trail required for compliance and breach investigation."),
            _c("Errors", "Errors", group="operational",
               why="SQL errors surfaced for triage."),
            _c("QueryStoreRuntimeStatistics", "Query Store runtime stats", group="performance",
               why="Query performance regressions."),
            _c("Timeouts", "Timeouts", group="operational",
               why="Command timeouts indicating contention or capacity issues."),
            _c("Deadlocks", "Deadlocks", group="operational",
               why="Deadlock graphs for diagnosing failed transactions."),
            _ALL_METRICS,
        ],
    },
    "microsoft.storage/storageaccounts": {
        "display": "Storage Account",
        "note": "Storage diagnostics live on the data-plane sub-resources (blob/file/…).",
        "categories": [
            _c("StorageRead", "Read transactions", group="operational",
               why="Read access logging for the account's services."),
            _c("StorageWrite", "Write transactions", group="operational",
               why="Write access logging."),
            _c("StorageDelete", "Delete transactions", group="audit",
               why="Delete operations — important for data-loss investigations."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/networksecuritygroups": {
        "display": "Network Security Group",
        "note": "NSG event + rule-counter logs are key for connectivity investigations.",
        "categories": [
            _c("NetworkSecurityGroupEvent", "NSG events", group="security",
               why="Rule evaluation events for allowed/denied flows."),
            _c("NetworkSecurityGroupRuleCounter", "NSG rule counter", group="security",
               why="Per-rule hit counts — shows which rule dropped traffic."),
        ],
    },
    "microsoft.network/frontdoors": {
        "display": "Front Door (classic)",
        "note": "Access + WAF logs are essential for edge incident triage.",
        "categories": [
            _c("FrontdoorAccessLog", "Access log", group="operational",
               why="Edge request logs for diagnosing routing/latency issues."),
            _c("FrontdoorWebApplicationFirewallLog", "WAF log", group="security",
               why="WAF match/block events — security signal at the edge."),
            _ALL_METRICS,
        ],
    },
    "microsoft.cdn/profiles": {
        "display": "Front Door (Standard/Premium)",
        "note": "Access + WAF logs for the edge.",
        "categories": [
            _c("FrontDoorAccessLog", "Access log", group="operational",
               why="Edge request logs."),
            _c("FrontDoorWebApplicationFirewallLog", "WAF log", group="security",
               why="WAF events at the edge."),
            _c("FrontDoorHealthProbeLog", "Health probe log", group="operational",
               why="Origin health-probe results."),
            _ALL_METRICS,
        ],
    },
    "microsoft.containerservice/managedclusters": {
        "display": "AKS Cluster",
        "note": "kube-apiserver + audit + controller logs for cluster forensics.",
        "categories": [
            _c("kube-apiserver", "API server", group="operational",
               why="Control-plane API server logs."),
            _c("kube-audit", "Kubernetes audit", group="audit",
               why="Full audit trail of cluster API actions."),
            _c("kube-audit-admin", "Kubernetes audit (admin)", group="audit",
               why="Audit trail excluding read-only get/list — lower volume."),
            _c("kube-controller-manager", "Controller manager", group="operational",
               why="Controller reconciliation logs."),
            _c("guard", "Guard (AAD)", group="security",
               why="AAD authn/authz webhook decisions."),
            _ALL_METRICS,
        ],
    },
    "microsoft.apimanagement/service": {
        "display": "API Management",
        "note": "Gateway logs for request-level API diagnostics.",
        "categories": [
            _c("GatewayLogs", "Gateway logs", group="operational",
               why="Per-request API gateway logs."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/applicationgateways": {
        "display": "Application Gateway",
        "note": "Access + performance + firewall logs.",
        "categories": [
            _c("ApplicationGatewayAccessLog", "Access log", group="operational",
               why="Per-request access logs through the gateway."),
            _c("ApplicationGatewayPerformanceLog", "Performance log", group="performance",
               why="Throughput/latency performance counters."),
            _c("ApplicationGatewayFirewallLog", "Firewall (WAF) log", group="security",
               why="WAF rule matches/blocks."),
            _ALL_METRICS,
        ],
    },
    "microsoft.documentdb/databaseaccounts": {
        "display": "Cosmos DB",
        "note": "Data-plane + query + control-plane audit logs.",
        "categories": [
            _c("DataPlaneRequests", "Data-plane requests", group="operational",
               why="Per-request data-plane logs."),
            _c("QueryRuntimeStatistics", "Query runtime stats", group="performance",
               why="Query performance diagnostics."),
            _c("ControlPlaneRequests", "Control-plane requests", group="audit",
               why="Account configuration changes — audit trail."),
            _ALL_METRICS,
        ],
    },
    "microsoft.cache/redis": {
        "display": "Redis Cache",
        "note": "Connection logs + platform metrics.",
        "categories": [
            _c("ConnectedClientList", "Connected client list", group="operational",
               why="Client connection diagnostics."),
            _ALL_METRICS,
        ],
    },
    "microsoft.web/sites/functions": {
        "display": "Function App",
        "note": "Function execution + HTTP logs are the baseline for invocation triage.",
        "categories": [
            _c("FunctionAppLogs", "Function logs", group="operational",
               why="Per-invocation execution logs — the first read when a function fails."),
            _c("AppServiceHTTPLogs", "HTTP logs", group="operational",
               why="HTTP-triggered request logs."),
            _c("AppServiceAuditLogs", "Audit logs", group="audit",
               why="Publishing/FTP access audit — who deployed/changed the app."),
            _ALL_METRICS,
        ],
    },
    "microsoft.servicebus/namespaces": {
        "display": "Service Bus",
        "note": "Operational + runtime-audit logs for messaging diagnostics.",
        "categories": [
            _c("OperationalLogs", "Operational logs", group="operational",
               why="Management/operation events on entities (create/delete/update)."),
            _c("RuntimeAuditLogs", "Runtime audit logs", group="audit",
               why="Data-plane auth/connection audit — who sent/received and how."),
            _c("VNetAndIPFilteringLogs", "VNet/IP filtering logs", group="security",
               why="Network-rule allow/deny events for the namespace."),
            _ALL_METRICS,
        ],
    },
    "microsoft.eventhub/namespaces": {
        "display": "Event Hubs",
        "note": "Operational + archive + audit logs for ingestion diagnostics.",
        "categories": [
            _c("OperationalLogs", "Operational logs", group="operational",
               why="Management/operation events on hubs and consumer groups."),
            _c("RuntimeAuditLogs", "Runtime audit logs", group="audit",
               why="Data-plane auth/connection audit for producers/consumers."),
            _c("ArchiveLogs", "Archive (capture) logs", group="operational",
               why="Event capture status — surfaces capture backlogs/failures."),
            _c("AutoScaleLogs", "Auto-scale logs", group="operational",
               why="Throughput-unit auto-inflate events."),
            _ALL_METRICS,
        ],
    },
    "microsoft.eventgrid/topics": {
        "display": "Event Grid",
        "note": "Publish + delivery failure logs are the key reliability signal.",
        "categories": [
            _c("PublishFailures", "Publish failures", group="operational",
               why="Producer-side publish errors."),
            _c("DeliveryFailures", "Delivery failures", group="operational",
               why="Subscriber-side delivery errors — undelivered/dead-lettered events."),
            _ALL_METRICS,
        ],
    },
    "microsoft.logic/workflows": {
        "display": "Logic App",
        "note": "Workflow runtime logs for run/action diagnostics.",
        "categories": [
            _c("WorkflowRuntime", "Workflow runtime", group="operational",
               why="Per-run and per-action execution diagnostics."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/azurefirewalls": {
        "display": "Azure Firewall",
        "note": "Application/network rule + DNS proxy logs are essential for connectivity + security forensics.",
        "categories": [
            _c("AZFWApplicationRule", "Application rule logs", group="security",
               why="L7 application-rule allow/deny decisions."),
            _c("AZFWNetworkRule", "Network rule logs", group="security",
               why="L3/L4 network-rule allow/deny decisions — the core connectivity trail."),
            _c("AZFWDnsQuery", "DNS proxy logs", group="security",
               why="DNS proxy query log — resolves what the firewall saw."),
            _c("AZFWThreatIntel", "Threat intel logs", group="security",
               why="Threat-intelligence match events."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/publicipaddresses": {
        "display": "Public IP",
        "note": "DDoS protection/mitigation logs (requires DDoS Protection).",
        "categories": [
            _c("DDoSProtectionNotifications", "DDoS notifications", group="security",
               why="Notifies when an attack is detected/mitigated on the IP."),
            _c("DDoSMitigationFlowLogs", "DDoS mitigation flow logs", group="security",
               why="Dropped/forwarded flows during an active mitigation."),
            _c("DDoSMitigationReports", "DDoS mitigation reports", group="security",
               why="Post-mitigation attack reports."),
            _ALL_METRICS,
        ],
    },
    "microsoft.cognitiveservices/accounts": {
        "display": "Azure AI / OpenAI",
        "note": "Audit + request/response logs for AI usage and abuse investigation.",
        "categories": [
            _c("Audit", "Audit", group="audit",
               why="Control-plane access/audit for the AI account."),
            _c("RequestResponse", "Request/response", group="operational",
               why="Per-request inference logs — latency, tokens, errors."),
            _c("Trace", "Trace", group="operational",
               why="Detailed processing traces for debugging."),
            _ALL_METRICS,
        ],
    },
    "microsoft.search/searchservices": {
        "display": "AI Search",
        "note": "Operation logs for query/index diagnostics.",
        "categories": [
            _c("OperationLogs", "Operation logs", group="operational",
               why="Query and indexing operation diagnostics."),
            _ALL_METRICS,
        ],
    },
    "microsoft.machinelearningservices/workspaces": {
        "display": "ML Workspace",
        "note": "Compute + job + data-asset logs for ML pipeline forensics.",
        "categories": [
            _c("AmlComputeClusterEvent", "Compute cluster events", group="operational",
               why="Cluster scale/node events."),
            _c("AmlComputeJobEvent", "Compute job events", group="operational",
               why="Training/scoring job lifecycle events."),
            _c("AmlRunStatusChangedEvent", "Run status changes", group="operational",
               why="Run state transitions — surfaces failures."),
            _c("AmlComputeClusterNodeEvent", "Cluster node events", group="operational",
               why="Per-node compute events."),
            _ALL_METRICS,
        ],
    },
    "microsoft.datafactory/factories": {
        "display": "Data Factory",
        "note": "Pipeline/trigger/activity run logs are mandatory for ETL diagnostics.",
        "categories": [
            _c("PipelineRuns", "Pipeline runs", group="operational",
               why="Per-pipeline run status — the top-level ETL signal."),
            _c("TriggerRuns", "Trigger runs", group="operational",
               why="Scheduled/event trigger firing results."),
            _c("ActivityRuns", "Activity runs", group="operational",
               why="Per-activity run detail — pinpoints the failing step."),
            _c("SandboxPipelineRuns", "Sandbox pipeline runs", group="operational",
               why="Debug/sandbox pipeline runs."),
            _ALL_METRICS,
        ],
    },
    "microsoft.synapse/workspaces": {
        "display": "Synapse Workspace",
        "note": "SQL security audit + request logs for the analytics tier.",
        "categories": [
            _c("SQLSecurityAuditEvents", "SQL security audit", group="security",
               why="Auditing trail for SQL pools — required for compliance."),
            _c("GatewayApiRequests", "Gateway API requests", group="operational",
               why="Workspace control-plane API requests."),
            _c("BuiltinSqlReqsEnded", "Built-in SQL requests", group="performance",
               why="Serverless SQL request completion + performance."),
            _c("IntegrationPipelineRuns", "Integration pipeline runs", group="operational",
               why="Synapse pipeline (ADF-style) run status."),
            _ALL_METRICS,
        ],
    },
    "microsoft.dbforpostgresql/flexibleservers": {
        "display": "PostgreSQL Flexible Server",
        "note": "Server + session + query-store logs for DB diagnostics.",
        "categories": [
            _c("PostgreSQLLogs", "PostgreSQL server logs", group="operational",
               why="Server logs — errors, slow queries, connection issues."),
            _c("PostgreSQLFlexSessions", "Sessions", group="performance",
               why="Active-session telemetry for contention analysis."),
            _c("PostgreSQLFlexQueryStoreRuntime", "Query Store runtime", group="performance",
               why="Query performance/runtime statistics."),
            _ALL_METRICS,
        ],
    },
    "microsoft.dbformysql/flexibleservers": {
        "display": "MySQL Flexible Server",
        "note": "Slow-query + audit logs for DB diagnostics and compliance.",
        "categories": [
            _c("MySqlSlowLogs", "Slow query logs", group="performance",
               why="Slow queries — the primary DB performance signal."),
            _c("MySqlAuditLogs", "Audit logs", group="audit",
               why="Connection/query audit trail for compliance."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/virtualnetworks": {
        "display": "Virtual Network",
        "note": "VM-protection alerts (DDoS) at the VNet scope.",
        "categories": [
            _c("VMProtectionAlerts", "VM protection alerts", group="security",
               why="DDoS protection alerts for resources in the VNet."),
            _ALL_METRICS,
        ],
    },
    "microsoft.network/loadbalancers": {
        "display": "Load Balancer",
        "note": "Health + alert events (metrics carry most LB signal).",
        "categories": [
            _c("LoadBalancerHealthEvent", "Health events", group="operational",
               why="Backend health-probe state changes."),
            _ALL_METRICS,
        ],
    },
    "microsoft.containerregistry/registries": {
        "display": "Container Registry",
        "note": "Repository + login audit logs for supply-chain forensics.",
        "categories": [
            _c("ContainerRegistryRepositoryEvents", "Repository events", group="audit",
               why="Push/pull/delete on images — the supply-chain audit trail."),
            _c("ContainerRegistryLoginEvents", "Login events", group="security",
               why="Authentication attempts against the registry."),
            _ALL_METRICS,
        ],
    },
    "microsoft.app/containerapps": {
        "display": "Container App",
        "note": "Console + system logs for revision/runtime diagnostics.",
        "categories": [
            _c("ContainerAppConsoleLogs", "Console logs", group="operational",
               why="stdout/stderr from app containers."),
            _c("ContainerAppSystemLogs", "System logs", group="operational",
               why="Platform/system events — scaling, probes, restarts."),
            _ALL_METRICS,
        ],
    },
    "microsoft.sql/servers": {
        "display": "SQL Server (logical)",
        "note": "Master-DB security audit + DevOps audit.",
        "categories": [
            _c("SQLSecurityAuditEvents", "SQL security audit", group="security",
               why="Server-level (master) audit trail for compliance."),
            _c("DevOpsOperationsAudit", "DevOps operations audit", group="audit",
               why="Audit of operations performed via DevOps/automation principals."),
            _ALL_METRICS,
        ],
    },
    "microsoft.operationalinsights/workspaces": {
        "display": "Log Analytics Workspace",
        "note": "Workspace audit (query + access auditing).",
        "categories": [
            _c("Audit", "Audit (LAQueryLogs/access)", group="audit",
               why="Who queried the workspace and what — access auditing for the log store itself."),
            _ALL_METRICS,
        ],
    },
}


def builtin_reference() -> dict[str, Any]:
    import copy

    return {
        "version": 0,
        "updated_at": "",
        "updated_by": "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "types": copy.deepcopy(BUILTIN_TYPES),
    }
