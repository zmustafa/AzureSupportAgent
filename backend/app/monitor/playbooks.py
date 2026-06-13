"""Resource-type monitoring playbooks and dashboard archetypes for Monitor AI authoring.

These are deterministic SRE guardrails. Architecture Memory explains what is special
about a workload; playbooks explain what excellent operators normally monitor for each
Azure resource type. The AI author combines both so generated dashboards avoid random
chart soup and cover golden signals, dependencies, security, cost, and observability
coverage.
"""
from __future__ import annotations

from typing import Any

DASHBOARD_ARCHETYPES: dict[str, dict[str, Any]] = {
    "full_stack": {
        "label": "Full-stack workload observability",
        "goal": "Production health across user experience, app, dependencies, security, cost, and telemetry coverage.",
        "layers": ["health", "user_journey", "golden_signals", "dependencies", "security", "cost", "coverage", "runbook"],
    },
    "sre_live": {
        "label": "SRE live operations",
        "goal": "Fast detection and triage for on-call engineers watching a live workload.",
        "layers": ["health", "golden_signals", "saturation", "errors", "dependencies", "changes", "runbook"],
    },
    "incident": {
        "label": "Incident commander",
        "goal": "What is broken, where, why, what changed, and what to do next.",
        "layers": ["impact", "errors", "latency", "dependencies", "changes", "recent_incidents", "runbook"],
    },
    "security": {
        "label": "Security and identity posture",
        "goal": "Identity, exposure, privileged changes, risky sign-ins, secrets/certificates, and compliance drift.",
        "layers": ["security_risk", "identity", "exposure", "secrets", "role_changes", "findings", "runbook"],
    },
    "cost_capacity": {
        "label": "Cost and capacity",
        "goal": "Spend, anomalies, waste, saturation, and capacity headroom.",
        "layers": ["cost", "capacity", "saturation", "top_resources", "anomalies", "recommendations", "runbook"],
    },
    "executive": {
        "label": "Executive health overview",
        "goal": "Low-noise health, SLA/SLO, open risks, cost trend, and accountable next steps.",
        "layers": ["health", "slo", "risk", "cost", "incidents", "coverage"],
    },
}

RESOURCE_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "match": ["microsoft.web/sites", "app service", "functionapp", "functions"],
        "label": "App Service / Functions",
        "golden_signals": ["requests", "5xx/errors", "response time", "CPU/memory", "restarts"],
        "metrics": ["Requests", "Http5xx", "AverageResponseTime", "CpuTime", "MemoryWorkingSet"],
        "logs": ["AppRequests", "AppExceptions", "AppTraces", "AzureDiagnostics"],
        "widgets": ["SLO/error-rate gauge", "request rate + latency chart", "5xx by operation", "exceptions table", "public /health synthetic ping", "deployment/change activity"],
    },
    {
        "match": ["microsoft.containerservice/managedclusters", "aks", "kubernetes"],
        "label": "AKS",
        "golden_signals": ["pod restarts", "pending pods", "node pressure", "ingress errors", "CPU/memory saturation"],
        "metrics": ["kube_node_status_condition", "kube_pod_status_ready", "node_cpu_usage_percentage", "node_memory_working_set_percentage"],
        "logs": ["ContainerLog", "KubePodInventory", "KubeEvents", "InsightsMetrics"],
        "widgets": ["node health", "pod restart table", "pending/failed pods", "ingress 5xx", "HPA/capacity saturation"],
    },
    {
        "match": ["microsoft.compute/virtualmachines", "virtual machine", "vm"],
        "label": "Virtual Machines",
        "golden_signals": ["CPU", "memory", "disk queue/IOPS", "network", "guest heartbeat"],
        "metrics": ["Percentage CPU", "Available Memory Bytes", "Disk Read Operations/Sec", "Disk Write Operations/Sec", "Network In Total"],
        "logs": ["Heartbeat", "Perf", "Syslog", "Event"],
        "widgets": ["VM heartbeat", "CPU/memory saturation", "disk bottlenecks", "network throughput", "boot diagnostics/backup coverage"],
    },
    {
        "match": ["microsoft.sql/servers/databases", "sql", "database"],
        "label": "Azure SQL",
        "golden_signals": ["CPU/DTU", "deadlocks", "blocked sessions", "connection failures", "storage"],
        "metrics": ["cpu_percent", "dtu_consumption_percent", "deadlock", "connection_failed", "storage_percent"],
        "logs": ["AzureDiagnostics", "SQLSecurityAuditEvents"],
        "widgets": ["CPU/DTU headroom", "deadlocks/blocks", "failed connections", "storage used", "top query errors"],
    },
    {
        "match": ["microsoft.storage/storageaccounts", "storage"],
        "label": "Storage Account",
        "golden_signals": ["availability", "transactions", "latency", "throttling", "auth failures"],
        "metrics": ["Availability", "Transactions", "SuccessE2ELatency", "Ingress", "Egress"],
        "logs": ["StorageBlobLogs", "StorageQueueLogs", "StorageTableLogs", "StorageFileLogs"],
        "widgets": ["availability", "transaction volume", "latency", "throttling/auth failures", "public access/security posture"],
    },
    {
        "match": ["microsoft.keyvault/vaults", "key vault", "keyvault"],
        "label": "Key Vault",
        "golden_signals": ["availability", "latency", "failed requests", "throttling", "certificate/secret expiry"],
        "metrics": ["ServiceApiHit", "ServiceApiLatency", "ServiceApiResult", "SaturationShoebox"],
        "logs": ["AuditEvent", "AzureDiagnostics"],
        "widgets": ["failed operations", "throttling", "latency", "secret/cert expiry", "access denied events"],
    },
    {
        "match": ["microsoft.network/applicationgateways", "frontdoor", "trafficmanager", "loadbalancers", "application gateway"],
        "label": "Edge / Load Balancing",
        "golden_signals": ["backend health", "5xx", "latency", "throughput", "TLS/WAF"],
        "metrics": ["HealthyHostCount", "UnhealthyHostCount", "FailedRequests", "TotalRequests", "Throughput"],
        "logs": ["AzureDiagnostics", "FrontDoorAccessLog", "FrontDoorHealthProbeLog"],
        "widgets": ["backend health", "edge 5xx", "WAF blocks", "TLS/cert status", "public endpoint ping"],
    },
    {
        "match": ["microsoft.servicebus", "eventhub", "event grid", "queue"],
        "label": "Messaging",
        "golden_signals": ["incoming/outgoing messages", "dead-letter", "server errors", "throttling", "queue depth"],
        "metrics": ["IncomingMessages", "OutgoingMessages", "DeadletteredMessages", "ServerErrors", "ThrottledRequests"],
        "logs": ["AzureDiagnostics"],
        "widgets": ["message flow", "dead-letter queue", "server/throttle errors", "consumer lag / queue depth"],
    },
    {
        "match": ["microsoft.insights/components", "application insights", "app insights"],
        "label": "Application Insights",
        "golden_signals": ["failed requests", "dependency failures", "exceptions", "response time", "availability tests"],
        "metrics": ["requests/failed", "dependencies/failed", "exceptions/count", "requests/duration"],
        "logs": ["AppRequests", "AppDependencies", "AppExceptions", "AppAvailabilityResults"],
        "widgets": ["failed requests", "dependency failure contribution", "exceptions", "availability tests", "slow operations"],
    },
]


def playbooks_for_types(resource_types: list[str]) -> list[dict[str, Any]]:
    """Return playbooks matching the workload's resource types (deduped)."""
    hay = "\n".join(t.lower() for t in resource_types)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pb in RESOURCE_PLAYBOOKS:
        if any(m.lower() in hay for m in pb["match"]):
            if pb["label"] not in seen:
                out.append(pb)
                seen.add(pb["label"])
    return out[:8]


def infer_topology(resource_types: list[str]) -> list[str]:
    """A simple deterministic request-path ordering the AI can refine."""
    checks = [
        ("edge", ["frontdoor", "applicationgateways", "trafficmanager", "loadbalancers"]),
        ("compute/app", ["microsoft.web/sites", "containerservice", "virtualmachines", "functions"]),
        ("messaging", ["servicebus", "eventhub", "eventgrid", "queue"]),
        ("data", ["sql", "storage", "cosmos", "mysql", "postgresql", "redis"]),
        ("secrets/identity", ["keyvault", "managedidentity", "authorization"]),
        ("observability", ["microsoft.insights", "operationalinsights"]),
    ]
    hay = "\n".join(t.lower() for t in resource_types)
    return [label for label, needles in checks if any(n in hay for n in needles)]
