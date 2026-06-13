"""Built-in AMBA (Azure Monitor Baseline Alerts) reference seed.

A curated resource-type → recommended-alerts map, derived from the public Azure Monitor
Baseline Alerts reference (https://azure.github.io/azure-monitor-baseline-alerts/). This
is the FALLBACK/seed only — admins edit a persisted copy in the JSON registry
(reference.py) which versions changes and can be reset back to this seed.

Each alert entry:
    key                 stable id within the type (used to synthesize finding check_ids)
    name                human label
    amba_category       availability | performance | security
    signal              "metric" | "log"
    metric              metric name (for metric alerts) — '' for log/query alerts
    operator            GreaterThan | LessThan | GreaterOrLessThan | Equals
    threshold           recommended numeric threshold (None = "exists/enabled" only)
    unit                display unit for the threshold (%, count, ms, …)
    window              evaluation/aggregation window (ISO8601-ish label, e.g. PT5M)
    severity            critical | error | warning | info  (drives finding severity)
    requires_action_group  True when a wired action group is part of "good"
    why                 short "why this matters" copy shown in the UI drawer
"""
from __future__ import annotations

from typing import Any

# Bumped whenever the seed below changes so the registry can offer "reset to builtin vN".
BUILTIN_SEED_VERSION = 2


def _a(
    key: str,
    name: str,
    amba_category: str,
    *,
    signal: str = "metric",
    metric: str = "",
    operator: str = "GreaterThan",
    threshold: float | None = None,
    unit: str = "",
    window: str = "PT5M",
    severity: str = "warning",
    requires_action_group: bool = True,
    why: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "amba_category": amba_category,
        "signal": signal,
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
        "unit": unit,
        "window": window,
        "severity": severity,
        "requires_action_group": requires_action_group,
        "why": why,
    }


# arm_type (lowercase) -> {display, category, alerts: [...]}
BUILTIN_TYPES: dict[str, dict[str, Any]] = {
    "microsoft.compute/virtualmachines": {
        "display": "Virtual Machine",
        "category": "compute",
        "alerts": [
            _a("vm_cpu", "CPU utilization high", "performance", metric="Percentage CPU",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Sustained high CPU indicates the VM is undersized or runaway processes are degrading the workload."),
            _a("vm_available_memory", "Available memory low", "performance", metric="Available Memory Bytes",
               operator="LessThan", threshold=1073741824, unit="bytes", window="PT5M", severity="warning",
               why="Low available memory causes paging and process kills, hurting reliability."),
            _a("vm_os_disk_iops", "OS disk IOPS consumed high", "performance",
               metric="OS Disk IOPS Consumed Percentage", operator="GreaterThan", threshold=95, unit="%",
               window="PT5M", severity="warning",
               why="Disk IOPS saturation throttles the VM and slows every dependent service."),
            _a("vm_network_in", "Inbound network unusually high", "performance", metric="Network In Total",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT5M", severity="info",
               why="Abnormal inbound traffic can signal an incident or a misbehaving client."),
        ],
    },
    "microsoft.web/sites": {
        "display": "App Service",
        "category": "compute",
        "alerts": [
            _a("appsvc_http5xx", "HTTP 5xx errors", "availability", metric="Http5xx",
               operator="GreaterThan", threshold=10, unit="count", window="PT5M", severity="error",
               why="5xx responses mean users are seeing failures — the single clearest availability signal."),
            _a("appsvc_response_time", "Response time high", "performance", metric="HttpResponseTime",
               operator="GreaterThan", threshold=5, unit="s", window="PT5M", severity="warning",
               why="Slow responses degrade UX and often precede outright failures."),
            _a("appsvc_cpu_time", "CPU time high", "performance", metric="CpuTime",
               operator="GreaterThan", threshold=None, unit="s", window="PT5M", severity="info",
               why="High CPU time signals the plan is undersized for the load."),
            _a("appsvc_health_check", "Health check status failing", "availability", metric="HealthCheckStatus",
               operator="LessThan", threshold=100, unit="%", window="PT5M", severity="error",
               why="Failing instance health checks remove capacity and risk an outage."),
        ],
    },
    "microsoft.web/serverfarms": {
        "display": "App Service Plan",
        "category": "compute",
        "alerts": [
            _a("plan_cpu", "Plan CPU high", "performance", metric="CpuPercentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="A saturated plan throttles every app hosted on it."),
            _a("plan_memory", "Plan memory high", "performance", metric="MemoryPercentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Memory pressure on the plan causes restarts across all hosted apps."),
        ],
    },
    "microsoft.sql/servers/databases": {
        "display": "SQL Database",
        "category": "data",
        "alerts": [
            _a("sql_dtu", "DTU/compute utilization high", "performance", metric="dtu_consumption_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Hitting the DTU/vCore ceiling throttles queries and stalls the application."),
            _a("sql_storage", "Storage utilization high", "availability", metric="storage_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT15M", severity="error",
               why="A full database goes read-only — a hard outage for writes."),
            _a("sql_deadlocks", "Deadlocks detected", "performance", metric="deadlock",
               operator="GreaterThan", threshold=1, unit="count", window="PT15M", severity="warning",
               why="Deadlocks cause failed transactions and user-visible errors."),
        ],
    },
    "microsoft.storage/storageaccounts": {
        "display": "Storage Account",
        "category": "data",
        "alerts": [
            _a("stor_availability", "Availability below 100%", "availability", metric="Availability",
               operator="LessThan", threshold=99, unit="%", window="PT5M", severity="error",
               why="Any availability dip on storage cascades to everything that depends on it."),
            _a("stor_latency", "Success E2E latency high", "performance", metric="SuccessE2ELatency",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="High storage latency slows every read/write across dependent apps."),
            _a("stor_throttle", "Throttling errors", "availability", metric="Transactions",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="Throttling (429s) means the account is over its limits and requests are failing."),
        ],
    },
    "microsoft.containerservice/managedclusters": {
        "display": "AKS Cluster",
        "category": "compute",
        "alerts": [
            _a("aks_node_cpu", "Node CPU high", "performance", metric="node_cpu_usage_percentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="CPU-saturated nodes evict pods and degrade the whole cluster."),
            _a("aks_node_memory", "Node memory working set high", "performance",
               metric="node_memory_working_set_percentage", operator="GreaterThan", threshold=90, unit="%",
               window="PT5M", severity="warning",
               why="Memory pressure triggers OOM kills and pod restarts."),
            _a("aks_not_ready", "Nodes not ready", "availability", metric="kube_node_status_condition",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="NotReady nodes silently shrink capacity and risk an outage."),
            _a("aks_pod_restarts", "Pod restarts elevated", "availability", metric="kube_pod_status_restarts_total",
               operator="GreaterThan", threshold=None, unit="count", window="PT15M", severity="info",
               why="Frequent restarts indicate crash-looping workloads."),
        ],
    },
    "microsoft.keyvault/vaults": {
        "display": "Key Vault",
        "category": "security",
        "alerts": [
            _a("kv_availability", "Vault availability below 100%", "availability", metric="Availability",
               operator="LessThan", threshold=99, unit="%", window="PT5M", severity="error",
               why="If the vault is unavailable, every app that fetches secrets/keys at runtime breaks."),
            _a("kv_latency", "Vault request latency high", "performance", metric="ServiceApiLatency",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="Slow vault calls add latency to app startup and token refresh."),
            _a("kv_saturation", "Vault API saturation high", "performance", metric="SaturationShoebox",
               operator="GreaterThan", threshold=75, unit="%", window="PT5M", severity="warning",
               why="Approaching the vault's transaction limits leads to throttling and failures."),
        ],
    },
    "microsoft.network/frontdoors": {
        "display": "Front Door (classic)",
        "category": "network",
        "alerts": [
            _a("fd_backend_health", "Backend health percentage low", "availability",
               metric="BackendHealthPercentage", operator="LessThan", threshold=100, unit="%", window="PT5M",
               severity="error",
               why="Unhealthy backends behind Front Door cause user-facing errors."),
            _a("fd_latency", "Total latency high", "performance", metric="TotalLatency",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="Edge latency directly impacts every request routed through Front Door."),
        ],
    },
    "microsoft.cdn/profiles": {
        "display": "Front Door (Standard/Premium)",
        "category": "network",
        "alerts": [
            _a("afd_origin_health", "Origin health below 100%", "availability",
               metric="OriginHealthPercentage", operator="LessThan", threshold=100, unit="%", window="PT5M",
               severity="error",
               why="Unhealthy origins behind Front Door produce user-facing 5xx errors."),
            _a("afd_4xx", "4xx error rate elevated", "availability", metric="Percentage4XX",
               operator="GreaterThan", threshold=5, unit="%", window="PT5M", severity="warning",
               why="A spike in 4xx often signals broken routing or auth regressions."),
            _a("afd_5xx", "5xx error rate elevated", "availability", metric="Percentage5XX",
               operator="GreaterThan", threshold=1, unit="%", window="PT5M", severity="error",
               why="5xx at the edge means users are seeing failures right now."),
        ],
    },
    "microsoft.network/loadbalancers": {
        "display": "Load Balancer",
        "category": "network",
        "alerts": [
            _a("lb_health_probe", "Health probe status low", "availability", metric="DipAvailability",
               operator="LessThan", threshold=100, unit="%", window="PT5M", severity="error",
               why="Failing backend probes remove targets and can black-hole traffic."),
            _a("lb_data_path", "Data path availability low", "availability", metric="VipAvailability",
               operator="LessThan", threshold=100, unit="%", window="PT5M", severity="error",
               why="VIP unavailability is a direct front-end outage."),
        ],
    },
    "microsoft.documentdb/databaseaccounts": {
        "display": "Cosmos DB",
        "category": "data",
        "alerts": [
            _a("cosmos_throttle", "Throttled requests (429)", "availability", metric="TotalRequestUnits",
               operator="GreaterThan", threshold=None, unit="RU", window="PT5M", severity="warning",
               why="429s mean the container is over-provisioned RU/s limits and requests fail."),
            _a("cosmos_availability", "Service availability below 100%", "availability",
               metric="ServiceAvailability", operator="LessThan", threshold=100, unit="%", window="PT1H",
               severity="error",
               why="Any Cosmos availability dip directly impacts the application's data tier."),
        ],
    },
    "microsoft.cache/redis": {
        "display": "Redis Cache",
        "category": "data",
        "alerts": [
            _a("redis_server_load", "Server load high", "performance", metric="serverLoad",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="A saturated Redis server slows or drops cache operations for dependent apps."),
            _a("redis_memory", "Used memory percentage high", "performance", metric="usedmemorypercentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Memory pressure triggers eviction and breaks cache-dependent flows."),
        ],
    },
    "microsoft.network/applicationgateways": {
        "display": "Application Gateway",
        "category": "network",
        "alerts": [
            _a("appgw_unhealthy_hosts", "Unhealthy host count", "availability",
               metric="UnhealthyHostCount", operator="GreaterThan", threshold=0, unit="count", window="PT5M",
               severity="error",
               why="Unhealthy backends behind the gateway cause user-facing failures."),
            _a("appgw_failed_requests", "Failed requests elevated", "availability", metric="FailedRequests",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="A rise in failed requests signals backend or capacity problems."),
        ],
    },
    "microsoft.web/staticsites": {
        "display": "Static Web App",
        "category": "web",
        "alerts": [
            _a("swa_5xx", "Function 5xx errors", "availability", metric="Http5xx",
               operator="GreaterThan", threshold=10, unit="count", window="PT5M", severity="error",
               why="Managed-function 5xx errors break the app's API surface."),
        ],
    },
    "microsoft.web/sites/functions": {
        "display": "Function App",
        "category": "compute",
        "alerts": [
            _a("func_5xx", "HTTP 5xx errors", "availability", metric="Http5xx",
               operator="GreaterThan", threshold=10, unit="count", window="PT5M", severity="error",
               why="Function 5xx responses mean invocations are failing."),
            _a("func_errors", "Function execution failures", "availability", metric="FunctionExecutionCount",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="A spike in executions paired with errors signals a failing trigger or dependency."),
            _a("func_response_time", "Response time high", "performance", metric="HttpResponseTime",
               operator="GreaterThan", threshold=5, unit="s", window="PT5M", severity="warning",
               why="Slow function responses degrade the API and may hit host timeouts."),
        ],
    },
    "microsoft.apimanagement/service": {
        "display": "API Management",
        "category": "integration",
        "alerts": [
            _a("apim_capacity", "Capacity high", "performance", metric="Capacity",
               operator="GreaterThan", threshold=80, unit="%", window="PT5M", severity="warning",
               why="High gateway capacity means APIM is near saturation and will start throttling."),
            _a("apim_5xx", "Gateway 5xx errors", "availability", metric="FailedRequests",
               operator="GreaterThan", threshold=10, unit="count", window="PT5M", severity="error",
               why="Gateway 5xx errors break every API consumer behind APIM."),
            _a("apim_backend_duration", "Backend latency high", "performance", metric="BackendDuration",
               operator="GreaterThan", threshold=2000, unit="ms", window="PT5M", severity="warning",
               why="Slow backends behind APIM degrade all routed APIs."),
        ],
    },
    "microsoft.compute/virtualmachinescalesets": {
        "display": "VM Scale Set",
        "category": "compute",
        "alerts": [
            _a("vmss_cpu", "CPU utilization high", "performance", metric="Percentage CPU",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Sustained high CPU across instances means the scale set is undersized."),
            _a("vmss_available_memory", "Available memory low", "performance", metric="Available Memory Bytes",
               operator="LessThan", threshold=1073741824, unit="bytes", window="PT5M", severity="warning",
               why="Low memory causes paging and instance instability."),
        ],
    },
    "microsoft.app/containerapps": {
        "display": "Container App",
        "category": "containers",
        "alerts": [
            _a("aca_replica_restarts", "Replica restart count elevated", "availability", metric="RestartCount",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Frequent replica restarts indicate crash-looping revisions."),
            _a("aca_requests_5xx", "5xx responses elevated", "availability", metric="Requests",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="error",
               why="5xx responses from the app mean users are seeing failures."),
            _a("aca_cpu", "CPU usage high", "performance", metric="UsageNanoCores",
               operator="GreaterThan", threshold=None, unit="ncores", window="PT5M", severity="info",
               why="CPU near the revision limit throttles request handling."),
        ],
    },
    "microsoft.containerregistry/registries": {
        "display": "Container Registry",
        "category": "containers",
        "alerts": [
            _a("acr_throttle", "Throttled pull/push operations", "availability", metric="TotalPullCount",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="Registry throttling stalls image pulls and breaks deployments/scale-out."),
            _a("acr_storage", "Storage used high", "performance", metric="StorageUsed",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT1H", severity="info",
               why="Approaching the registry storage quota blocks new pushes."),
        ],
    },
    "microsoft.sql/servers": {
        "display": "SQL Server",
        "category": "data",
        "alerts": [
            _a("sqlsrv_dtu_pool", "Elastic pool DTU high", "performance", metric="dtu_consumption_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="A saturated elastic pool throttles every database in it."),
        ],
    },
    "microsoft.dbforpostgresql/flexibleservers": {
        "display": "PostgreSQL Flexible Server",
        "category": "data",
        "alerts": [
            _a("pg_cpu", "CPU utilization high", "performance", metric="cpu_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Sustained high CPU throttles queries and stalls the application."),
            _a("pg_memory", "Memory utilization high", "performance", metric="memory_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Memory pressure causes slow queries and connection failures."),
            _a("pg_storage", "Storage utilization high", "availability", metric="storage_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT15M", severity="error",
               why="A full data disk takes the server read-only — a write outage."),
            _a("pg_connections_failed", "Failed connections elevated", "availability", metric="connections_failed",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="A rise in failed connections signals auth, limit, or capacity problems."),
        ],
    },
    "microsoft.dbformysql/flexibleservers": {
        "display": "MySQL Flexible Server",
        "category": "data",
        "alerts": [
            _a("mysql_cpu", "CPU utilization high", "performance", metric="cpu_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Sustained high CPU throttles queries and stalls the application."),
            _a("mysql_memory", "Memory utilization high", "performance", metric="memory_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="Memory pressure causes slow queries and connection failures."),
            _a("mysql_storage", "Storage utilization high", "availability", metric="storage_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT15M", severity="error",
               why="A full data disk takes the server read-only — a write outage."),
            _a("mysql_aborted_connections", "Aborted connections elevated", "availability",
               metric="aborted_connections", operator="GreaterThan", threshold=None, unit="count", window="PT5M",
               severity="warning",
               why="Aborted connections signal client, auth, or capacity issues."),
        ],
    },
    "microsoft.servicebus/namespaces": {
        "display": "Service Bus",
        "category": "integration",
        "alerts": [
            _a("sb_server_errors", "Server errors", "availability", metric="ServerErrors",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="Server errors mean the namespace is failing requests from producers/consumers."),
            _a("sb_throttled", "Throttled requests", "availability", metric="ThrottledRequests",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               why="Throttling means the namespace is over its limits and messages are rejected."),
            _a("sb_deadletter", "Dead-lettered messages", "availability", metric="DeadletteredMessages",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Growing dead-letter queues mean messages can't be processed — data is stuck."),
            _a("sb_active_messages", "Active message backlog high", "performance", metric="ActiveMessages",
               operator="GreaterThan", threshold=None, unit="count", window="PT15M", severity="info",
               why="A growing backlog signals consumers can't keep up with the producers."),
        ],
    },
    "microsoft.eventhub/namespaces": {
        "display": "Event Hubs",
        "category": "integration",
        "alerts": [
            _a("eh_server_errors", "Server errors", "availability", metric="ServerErrors",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="Server errors mean events are being rejected by the namespace."),
            _a("eh_throttled", "Throttled requests", "availability", metric="ThrottledRequests",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               why="Throttling means ingestion is over the provisioned throughput units."),
            _a("eh_capture_backlog", "Capture backlog", "performance", metric="CaptureBacklog",
               operator="GreaterThan", threshold=None, unit="count", window="PT15M", severity="info",
               why="A capture backlog means events aren't landing in storage on time."),
        ],
    },
    "microsoft.eventgrid/topics": {
        "display": "Event Grid",
        "category": "integration",
        "alerts": [
            _a("eg_publish_fail", "Publish failures", "availability", metric="PublishFailCount",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="Publish failures mean producers can't emit events."),
            _a("eg_deadletter", "Dead-lettered events", "availability", metric="DeadLetteredCount",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Dead-lettered events were never delivered to any subscriber."),
            _a("eg_delivery_fail", "Delivery attempt failures", "availability", metric="DeliveryAttemptFailCount",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="Failed deliveries mean subscribers aren't receiving events."),
        ],
    },
    "microsoft.logic/workflows": {
        "display": "Logic App",
        "category": "integration",
        "alerts": [
            _a("logic_runs_failed", "Runs failed", "availability", metric="RunsFailed",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="error",
               why="Failed runs mean the workflow's business process isn't completing."),
            _a("logic_runs_throttled", "Runs throttled", "availability", metric="RunsThrottled",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Throttled runs are delayed or dropped under load."),
            _a("logic_action_latency", "Action latency high", "performance", metric="ActionLatency",
               operator="GreaterThan", threshold=None, unit="ms", window="PT15M", severity="info",
               why="High action latency slows the end-to-end workflow."),
        ],
    },
    "microsoft.network/azurefirewalls": {
        "display": "Azure Firewall",
        "category": "network",
        "alerts": [
            _a("afw_snat_ports", "SNAT port utilization high", "availability", metric="SNATPortUtilization",
               operator="GreaterThan", threshold=80, unit="%", window="PT5M", severity="error",
               why="SNAT port exhaustion silently drops outbound connections across the whole network."),
            _a("afw_throughput", "Throughput high", "performance", metric="Throughput",
               operator="GreaterThan", threshold=None, unit="bps", window="PT5M", severity="info",
               why="Approaching the firewall throughput ceiling adds latency and risks drops."),
            _a("afw_health", "Firewall health degraded", "availability", metric="FirewallHealth",
               operator="LessThan", threshold=100, unit="%", window="PT5M", severity="error",
               why="A degraded firewall can black-hole or bottleneck all routed traffic."),
        ],
    },
    "microsoft.network/privateendpoints": {
        "display": "Private Endpoint",
        "category": "network",
        "alerts": [
            _a("pe_bytes_drop", "Dropped bytes", "availability", metric="PEBytesOut",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT5M", severity="info",
               why="A drop in private-endpoint traffic can indicate a broken private link path."),
        ],
    },
    "microsoft.network/publicipaddresses": {
        "display": "Public IP",
        "category": "network",
        "alerts": [
            _a("pip_ddos_attack", "Under DDoS attack", "availability", metric="IfUnderDDoSAttack",
               operator="GreaterThan", threshold=0, unit="flag", window="PT5M", severity="critical",
               why="An active DDoS attack on the public IP threatens the availability of the front-end."),
            _a("pip_ddos_dropped", "DDoS packets dropped", "availability", metric="DDoSTriggerTCPPacketsInDDoS",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="warning",
               why="Packet drops from DDoS mitigation indicate an attack is in progress."),
        ],
    },
    "microsoft.cognitiveservices/accounts": {
        "display": "Azure AI / OpenAI",
        "category": "ai",
        "alerts": [
            _a("ai_throttle_429", "Throttled (429) calls", "availability", metric="ClientErrors",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="error",
               why="429s mean requests are exceeding the deployment's TPM/RPM quota and failing."),
            _a("ai_latency", "Response latency high", "performance", metric="Latency",
               operator="GreaterThan", threshold=5000, unit="ms", window="PT5M", severity="warning",
               why="High inference latency degrades every dependent experience."),
            _a("ai_server_errors", "Server (5xx) errors", "availability", metric="ServerErrors",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="5xx errors from the AI service break inference calls."),
        ],
    },
    "microsoft.search/searchservices": {
        "display": "AI Search",
        "category": "ai",
        "alerts": [
            _a("search_throttled", "Throttled search queries", "availability", metric="ThrottledSearchQueriesPercentage",
               operator="GreaterThan", threshold=5, unit="%", window="PT5M", severity="warning",
               why="Throttled queries mean the service is over capacity and dropping requests."),
            _a("search_latency", "Search latency high", "performance", metric="SearchLatency",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="Slow search hurts every search-driven experience."),
        ],
    },
    "microsoft.machinelearningservices/workspaces": {
        "display": "ML Workspace",
        "category": "ai",
        "alerts": [
            _a("ml_failed_runs", "Failed pipeline/job runs", "availability", metric="Failed Runs",
               operator="GreaterThan", threshold=0, unit="count", window="PT1H", severity="warning",
               why="Failed runs mean training/scoring jobs aren't completing."),
            _a("ml_quota_utilization", "Quota utilization high", "performance", metric="Quota Utilization Percentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT15M", severity="warning",
               why="Compute quota exhaustion blocks new jobs from starting."),
        ],
    },
    "microsoft.datafactory/factories": {
        "display": "Data Factory",
        "category": "analytics",
        "alerts": [
            _a("adf_pipeline_failed", "Failed pipeline runs", "availability", metric="PipelineFailedRuns",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="error",
               why="Failed pipelines mean data isn't moving — downstream reports/ETL go stale."),
            _a("adf_trigger_failed", "Failed trigger runs", "availability", metric="TriggerFailedRuns",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Failed triggers mean scheduled pipelines never started."),
            _a("adf_activity_failed", "Failed activity runs", "availability", metric="ActivityFailedRuns",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Failed activities point to the specific step breaking a pipeline."),
        ],
    },
    "microsoft.synapse/workspaces": {
        "display": "Synapse Workspace",
        "category": "analytics",
        "alerts": [
            _a("syn_failed_requests", "Failed SQL requests", "availability", metric="BuiltinSqlPoolRequestsEnded",
               operator="GreaterThan", threshold=None, unit="count", window="PT15M", severity="warning",
               why="Failed SQL requests mean queries/loads against the pool are erroring."),
        ],
    },
    "microsoft.operationalinsights/workspaces": {
        "display": "Log Analytics Workspace",
        "category": "monitoring",
        "alerts": [
            _a("law_ingestion_spike", "Ingestion volume spike", "performance", metric="Ingestion Volume",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT1H", severity="info",
               why="An ingestion spike can blow the cost budget or hit the daily cap (losing data)."),
        ],
    },
}


def builtin_reference() -> dict[str, Any]:
    """A fresh copy of the built-in reference document (version 0 = seed)."""
    import copy

    return {
        "version": 0,
        "updated_at": "",
        "updated_by": "",
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "types": copy.deepcopy(BUILTIN_TYPES),
    }
