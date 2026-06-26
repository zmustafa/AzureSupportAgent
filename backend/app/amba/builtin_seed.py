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
BUILTIN_SEED_VERSION = 7


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
    dimension_filter: str = "",
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
        # Optional Azure Monitor metric dimension filter (e.g. "StatusCode eq '403'"),
        # so one metric (ServiceApiResult) can be split into distinct signals.
        "dimension_filter": dimension_filter,
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
            _a("vm_network_out", "Outbound network unusually high", "performance", metric="Network Out Total",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT5M", severity="info",
               why="A spike in outbound traffic can signal data exfiltration, a runaway backup/replication "
                   "job, or a chatty dependency — and on metered egress it drives cost."),
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
            _a("sql_cpu", "CPU utilization high", "performance", metric="cpu_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="cpu_percent is the vCore/serverless compute utilization (the DTU metric does not exist on "
                   "vCore databases). Sustained high CPU throttles queries and stalls the application — the "
                   "primary scale signal for a General Purpose / serverless database."),
            _a("sql_log_write", "Log write utilization high", "performance", metric="log_write_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="log_write_percent is how hard the transaction-log writer is pushed against its rate "
                   "limit. A pegged log throttles every INSERT/UPDATE/DELETE — a classic hidden bottleneck "
                   "during bulk loads, index rebuilds, and write bursts that CPU alone won't reveal."),
            _a("sql_workers", "Worker utilization high", "performance", metric="workers_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="workers_percent is the share of the database's max concurrent workers (requests) in use. "
                   "Near 100% means new queries queue or fail with worker-limit errors — the saturation point "
                   "behind 'the database stopped responding' even when CPU looks moderate."),
            _a("sql_sessions", "Session utilization high", "performance", metric="sessions_percent",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="sessions_percent is open sessions as a share of the database limit. Connection leaks or a "
                   "missing pool cap drive this toward 100%, after which new connections are refused — an "
                   "outage that looks like an app bug, not a database one."),
            _a("sql_connection_failed", "Failed connections elevated", "availability", metric="connection_failed",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               why="A rise in failed connections signals auth failures, hitting the connection/worker limit, "
                   "or firewall/network blocks — callers can't reach the data tier even though it reports "
                   "healthy."),
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
               why="High end-to-end storage latency slows every read/write across dependent apps. "
                   "Compare against server latency below to tell storage-side slowness from client/network."),
            _a("stor_server_latency", "Server-side latency high", "performance", metric="SuccessServerLatency",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="Server latency is the storage-side processing time only. High server latency points at "
                   "the storage tier (large ops, archive/cool reads, onset of throttling); high E2E but normal "
                   "server latency points at the client/network instead."),
            _a("stor_throttle", "Throttled requests (503)", "performance", metric="Transactions",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               dimension_filter="ResponseType eq 'ServerBusyError'",
               why="ServerBusyError (503) means the account is exceeding its per-account or per-partition "
                   "scalability targets and requests are being throttled \u2014 callers should back off, and a hot "
                   "prefix/partition may need to be spread out."),
            _a("stor_auth_failures", "Authorization failures (403)", "security", metric="Transactions",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               dimension_filter="ResponseType eq 'AuthorizationError'",
               why="AuthorizationError (403) means callers are being denied \u2014 usually a firewall/network-ACL "
                   "rule, a missing RBAC data-plane role, or an expired/invalid SAS. The account can read 100% "
                   "'available' while every dependent app fails to read its data, so this is the signal that "
                   "actually catches access outages."),
            _a("stor_authn_failures", "Authentication failures (403)", "security", metric="Transactions",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               dimension_filter="ResponseType eq 'AuthenticationError'",
               why="AuthenticationError (403) is the OTHER half of access failures, distinct from "
                   "AuthorizationError: it's a network-ACL/firewall deny (public access disabled or default "
                   "action Deny), an invalid/expired SAS token, or a wrong account key \u2014 the request is "
                   "rejected before authorization is even evaluated. This is the single most common "
                   "'storage reports 100% available but the app can't reach it' outage, so it must be caught "
                   "separately from RBAC-style AuthorizationError."),
            _a("stor_capacity", "Used capacity growing", "performance", metric="UsedCapacity",
               operator="GreaterThan", threshold=None, unit="bytes", window="PT1H", severity="info",
               why="UsedCapacity is the total bytes stored. A sudden climb can signal a runaway producer, a "
                   "stuck lifecycle/cleanup job, or unbounded log/blob growth — worth watching before it drives "
                   "cost or hits a quota."),
        ],
    },    "microsoft.compute/disks": {
        "display": "Managed Disk",
        "category": "data",
        "alerts": [
            _a("disk_iops_saturation", "Disk IOPS saturation high", "performance",
               metric="Disk IOPS saturation", operator="GreaterThan", threshold=80, unit="%",
               window="PT5M", severity="warning",
               why="Total disk IOPS (read+write) as a percentage of the disk's provisioned IOPS. Sustained "
                   "high IOPS saturation means the disk — not the CPU — is the bottleneck: reads/writes queue "
                   "and every dependent app (DB, app server) slows down. Once burst credits are exhausted the "
                   "disk is capped at its baseline IOPS. Bump the disk tier/size or enable on-demand bursting."),
            _a("disk_throughput_saturation", "Disk throughput saturation high", "performance",
               metric="Disk throughput saturation", operator="GreaterThan", threshold=80, unit="%",
               window="PT5M", severity="warning",
               why="Total disk throughput (read+write MB/s) as a percentage of the disk's provisioned "
                   "bandwidth. High throughput saturation throttles large sequential I/O — backups, ETL, log "
                   "flushes, restores — long before IOPS run out. Move to a higher tier or stripe the load "
                   "across multiple disks."),
        ],
    },    "microsoft.containerservice/managedclusters": {
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
            _a("kv_auth_failures", "Authorization failures (401/403)", "security", metric="ServiceApiResult",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               dimension_filter="StatusCode eq '401' or StatusCode eq '403'",
               why="401/403 results mean callers are being denied — usually a missing access policy / RBAC "
                   "role assignment or an expired identity. Secret and key fetches fail at runtime even though "
                   "the vault reports 100% 'available', so this is the signal that actually catches access outages."),
            _a("kv_throttling", "Throttled requests (429)", "performance", metric="ServiceApiResult",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               dimension_filter="StatusCode eq '429'",
               why="429 responses mean the vault is hitting its per-vault transaction limits; callers should "
                   "cache secrets and back off, and heavy workloads may need to spread load across vaults."),
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
               operator="LessThan", threshold=90, unit="%", window="PT5M", severity="error",
               why="Failing backend probes remove targets and can black-hole traffic."),
            _a("lb_data_path", "Data path availability low", "availability", metric="VipAvailability",
               operator="LessThan", threshold=90, unit="%", window="PT5M", severity="error",
               why="VIP unavailability is a direct front-end outage."),
        ],
    },
    "microsoft.documentdb/databaseaccounts": {
        "display": "Cosmos DB",
        "category": "data",
        "alerts": [
            _a("cosmos_normalized_ru", "Normalized RU consumption high", "performance",
               metric="NormalizedRUConsumption", operator="GreaterThan", threshold=80, unit="%",
               window="PT5M", severity="warning",
               why="NormalizedRUConsumption is the MAX RU utilization across partition key ranges as a percent "
                   "of provisioned (or autoscale-max) RU/s. Sustained values near 100% mean a single partition "
                   "is saturated — requests there get 429-throttled even while the account average looks fine. "
                   "This is the earliest, truest signal of a hot partition or an under-provisioned container. "
                   "Note: this is a provisioned/autoscale metric \u2014 SERVERLESS accounts don't emit it (there is "
                   "no provisioned RU/s to normalize against), so on serverless it reads 'no data' and you "
                   "should watch server-side latency and 429s instead."),
            _a("cosmos_429", "Rate-limited requests (429)", "availability", metric="TotalRequests",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               dimension_filter="StatusCode eq '429'",
               why="A 429 means Cosmos rejected the request for exceeding the provisioned RU/s for that "
                   "partition (or the serverless ceiling). The SDK retries with backoff, but sustained 429s "
                   "surface to the app as added latency and eventually failures — the direct signal that the "
                   "data tier is over its throughput budget."),
            _a("cosmos_server_latency", "Server-side latency high", "performance",
               metric="ServerSideLatency", operator="GreaterThan", threshold=100, unit="ms", window="PT5M",
               severity="warning",
               why="ServerSideLatency is the time Cosmos itself spent servicing the request (excludes network "
                   "and SDK). Rising server latency points at expensive cross-partition queries, missing "
                   "indexes, or large documents — tuning targets that no amount of client-side scaling fixes."),
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
            _a("aca_http_5xx", "5xx server errors", "availability", metric="Requests",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               dimension_filter="statusCodeCategory eq '5xx'",
               why="Requests split to the 5xx status category are the app's own server errors — the single "
                   "clearest signal users are seeing failures right now. Unfiltered request counts can't tell "
                   "a healthy spike from an error spike; this dimension filter does."),
            _a("aca_cpu_pct", "CPU utilization high", "performance", metric="CpuPercentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="CpuPercentage is CPU against the revision's allocated cores. Sustained high CPU throttles "
                   "request handling and inflates latency — the signal that the app needs more cores per "
                   "replica or more replicas."),
            _a("aca_memory_pct", "Memory utilization high", "performance", metric="MemoryPercentage",
               operator="GreaterThan", threshold=90, unit="%", window="PT5M", severity="warning",
               why="MemoryPercentage near 100% precedes OOM kills, which show up as replica restarts and 5xx. "
                   "Catching memory pressure early explains crash-loops that look mysterious from request "
                   "metrics alone."),
            _a("aca_replica_restarts", "Replica restart count elevated", "availability", metric="RestartCount",
               operator="GreaterThan", threshold=0, unit="count", window="PT15M", severity="warning",
               why="Frequent replica restarts indicate crash-looping revisions (bad image, failing readiness "
                   "probe, or OOM)."),
            _a("aca_replicas", "Active replica count", "performance", metric="Replicas",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="info",
               why="Replicas is the current replica count. Pinned at the max scale bound under load means the "
                   "app is capacity-capped (raise maxReplicas or the scale rule); flapping to zero explains "
                   "cold-start latency on the next request."),
            _a("aca_cpu", "CPU usage (nanocores)", "performance", metric="UsageNanoCores",
               operator="GreaterThan", threshold=None, unit="ncores", window="PT5M", severity="info",
               why="Absolute CPU consumption in nanocores — a raw companion to CpuPercentage for sizing."),
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
            _a("sb_user_errors", "User errors elevated", "availability", metric="UserErrors",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="warning",
               why="UserErrors are client-side failures (bad auth, message-too-large, entity-not-found, lock "
                   "lost). Unlike ServerErrors they point at the producer/consumer code or config — a spike "
                   "usually means a recent deploy broke how the app talks to the namespace."),
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
            _a("eh_quota_exceeded", "Quota exceeded errors", "availability", metric="QuotaExceededErrors",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="QuotaExceededErrors mean producers/consumers hit a hard namespace quota (throughput-unit "
                   "ceiling, connection count, or entity size). Events are rejected outright — a sharper signal "
                   "than generic throttling that the tier is undersized for the load."),
            _a("eh_incoming_messages", "Incoming message throughput", "performance", metric="IncomingMessages",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="info",
               why="IncomingMessages is the ingest rate. Read alongside ThrottledRequests it shows whether a "
                   "throttle is driven by real volume (scale up TUs) or a hot partition (fix the partition key)."),
            _a("eh_outgoing_messages", "Outgoing message throughput", "performance", metric="OutgoingMessages",
               operator="GreaterThan", threshold=None, unit="count", window="PT5M", severity="info",
               why="OutgoingMessages is the consumer egress rate. Persistently far below IncomingMessages means "
                   "consumers are falling behind — the lag that eventually breaches retention and loses events."),
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
    "microsoft.appconfiguration/configurationstores": {
        "display": "App Configuration",
        "category": "integration",
        "alerts": [
            _a("appcfg_throttled", "Throttled requests (429)", "availability", metric="ThrottledHttpRequestCount",
               operator="GreaterThan", threshold=0, unit="count", window="PT5M", severity="error",
               why="ThrottledHttpRequestCount is requests rejected with 429 for exceeding the store's request "
                   "quota (the Free tier caps daily requests hard, Standard caps per-second). Throttling means "
                   "apps can't read their configuration/feature flags at runtime — startup stalls and flags go "
                   "stale. The fix is client-side caching/Sentinel polling or moving off Free."),
            _a("appcfg_quota", "Request quota utilization high", "performance", metric="RequestQuotaUsage",
               operator="GreaterThan", threshold=80, unit="%", window="PT5M", severity="warning",
               why="RequestQuotaUsage is how close the store is to its request-quota ceiling. Watching it climb "
                   "toward 100% is the early warning before throttling actually starts — time to add caching or "
                   "upgrade the tier before config reads begin failing."),
            _a("appcfg_latency", "Request latency high", "performance", metric="HttpIncomingRequestDuration",
               operator="GreaterThan", threshold=1000, unit="ms", window="PT5M", severity="warning",
               why="HttpIncomingRequestDuration is server-side request latency. Slow config reads add directly "
                   "to app cold-start and the latency of any flag/refresh check on the hot path."),
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
