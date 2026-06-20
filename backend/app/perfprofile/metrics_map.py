"""Aggregation + ceiling semantics for AMBA metrics, used by the profiler.

The AMBA reference gives us, per resource type, the alert metrics + thresholds. To turn a
metric into a "% of threshold / % of ceiling" reading we also need:
  - which Azure Monitor aggregation to query (Average/Maximum/Total)
  - the absolute ceiling for the metric where one exists (e.g. 100 for a percentage),
    so we can also express headroom toward the hard limit (not just the alert threshold)

This is a small curated overlay keyed by (arm_type, metric). When an entry is missing we
fall back to sensible defaults (Average aggregation; ceiling=100 for %-unit metrics)."""
from __future__ import annotations

from typing import Any

# (arm_type_lower, metric) -> {aggregation, ceiling, higher_is_worse}
# ceiling None => no fixed hard ceiling (the AMBA threshold is the only reference line).
_OVERLAY: dict[tuple[str, str], dict[str, Any]] = {
    ("microsoft.compute/virtualmachines", "Percentage CPU"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.compute/virtualmachines", "Available Memory Bytes"): {"aggregation": "Average", "ceiling": None, "higher_is_worse": False},
    # Managed disk saturation: synthetic % series the collector derives from the Composite
    # read+write counters divided by the disk's provisioned IOPS / MB-per-second.
    ("microsoft.compute/disks", "Disk IOPS saturation"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.compute/disks", "Disk throughput saturation"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.compute/virtualmachines", "OS Disk IOPS Consumed Percentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.compute/virtualmachinescalesets", "Percentage CPU"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.compute/virtualmachinescalesets", "Available Memory Bytes"): {"aggregation": "Average", "ceiling": None, "higher_is_worse": False},
    ("microsoft.web/sites", "Http5xx"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.web/sites", "HttpResponseTime"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.web/sites", "CpuTime"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.web/sites", "HealthCheckStatus"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.web/serverfarms", "CpuPercentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.web/serverfarms", "MemoryPercentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "dtu_consumption_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "storage_percent"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.sql/servers/databases", "deadlock"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.storage/storageaccounts", "Availability"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.storage/storageaccounts", "SuccessE2ELatency"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.storage/storageaccounts", "SuccessServerLatency"): {"aggregation": "Average", "ceiling": None},
    # Transactions is a request COUNT (split by ResponseType via a dimension filter) — sum the
    # buckets so a single 403/503 in any interval registers as a breach.
    ("microsoft.storage/storageaccounts", "Transactions"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.containerservice/managedclusters", "node_cpu_usage_percentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.containerservice/managedclusters", "node_memory_working_set_percentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.keyvault/vaults", "Availability"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.keyvault/vaults", "ServiceApiLatency"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.keyvault/vaults", "SaturationShoebox"): {"aggregation": "Average", "ceiling": 100},
    # ServiceApiResult is a request COUNT (split by status code via a dimension filter) —
    # sum the buckets so a single 401/403/429 in any interval registers as a breach.
    ("microsoft.keyvault/vaults", "ServiceApiResult"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.cache/redis", "serverLoad"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.cache/redis", "usedmemorypercentage"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.documentdb/databaseaccounts", "ServiceAvailability"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.network/applicationgateways", "UnhealthyHostCount"): {"aggregation": "Maximum", "ceiling": None},
    ("microsoft.network/loadbalancers", "VipAvailability"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.network/loadbalancers", "DipAvailability"): {"aggregation": "Average", "ceiling": 100, "higher_is_worse": False},
    ("microsoft.servicebus/namespaces", "ThrottledRequests"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.servicebus/namespaces", "ServerErrors"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.eventhub/namespaces", "ThrottledRequests"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.dbforpostgresql/flexibleservers", "cpu_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.dbforpostgresql/flexibleservers", "memory_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.dbforpostgresql/flexibleservers", "storage_percent"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.dbformysql/flexibleservers", "cpu_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.dbformysql/flexibleservers", "memory_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.cognitiveservices/accounts", "Latency"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.search/searchservices", "SearchLatency"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.search/searchservices", "ThrottledSearchQueriesPercentage"): {"aggregation": "Average", "ceiling": 100},
    # --- seed v6 additions -------------------------------------------------------------
    # VM outbound network (Total counter, like Network In Total).
    ("microsoft.compute/virtualmachines", "Network Out Total"): {"aggregation": "Total", "ceiling": None},
    # SQL vCore/serverless saturation %s (the DTU metric is absent on vCore). connection_failed
    # is a COUNT so it must sum.
    ("microsoft.sql/servers/databases", "cpu_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "log_write_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "workers_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "sessions_percent"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.sql/servers/databases", "connection_failed"): {"aggregation": "Total", "ceiling": None},
    # Storage used capacity gauge.
    ("microsoft.storage/storageaccounts", "UsedCapacity"): {"aggregation": "Average", "ceiling": None},
    # Cosmos: NormalizedRUConsumption is the MAX across partitions; TotalRequests is a COUNT
    # split by StatusCode (429); ServerSideLatency is server processing time.
    ("microsoft.documentdb/databaseaccounts", "NormalizedRUConsumption"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.documentdb/databaseaccounts", "TotalRequests"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.documentdb/databaseaccounts", "ServerSideLatency"): {"aggregation": "Average", "ceiling": None},
    ("microsoft.documentdb/databaseaccounts", "TotalRequestUnits"): {"aggregation": "Total", "ceiling": None},
    # Container Apps: Requests is a COUNT split by statusCodeCategory (5xx); CPU/Mem are %s;
    # Replicas/RestartCount are peak gauges.
    ("microsoft.app/containerapps", "Requests"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.app/containerapps", "CpuPercentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.app/containerapps", "MemoryPercentage"): {"aggregation": "Average", "ceiling": 100},
    ("microsoft.app/containerapps", "RestartCount"): {"aggregation": "Maximum", "ceiling": None},
    ("microsoft.app/containerapps", "Replicas"): {"aggregation": "Maximum", "ceiling": None},
    ("microsoft.app/containerapps", "UsageNanoCores"): {"aggregation": "Average", "ceiling": None},
    # Service Bus: backlog gauges report PEAK (Maximum); UserErrors is a COUNT.
    ("microsoft.servicebus/namespaces", "ActiveMessages"): {"aggregation": "Maximum", "ceiling": None},
    ("microsoft.servicebus/namespaces", "DeadletteredMessages"): {"aggregation": "Maximum", "ceiling": None},
    ("microsoft.servicebus/namespaces", "UserErrors"): {"aggregation": "Total", "ceiling": None},
    # Event Hubs: error/throughput COUNTs.
    ("microsoft.eventhub/namespaces", "ServerErrors"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.eventhub/namespaces", "QuotaExceededErrors"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.eventhub/namespaces", "IncomingMessages"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.eventhub/namespaces", "OutgoingMessages"): {"aggregation": "Total", "ceiling": None},
    # App Configuration: throttle COUNT, quota %, latency.
    ("microsoft.appconfiguration/configurationstores", "ThrottledHttpRequestCount"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.appconfiguration/configurationstores", "RequestQuotaUsage"): {"aggregation": "Maximum", "ceiling": 100},
    ("microsoft.appconfiguration/configurationstores", "HttpIncomingRequestDuration"): {"aggregation": "Average", "ceiling": None},
    # Logic Apps + Data Factory run COUNTs must sum across the window.
    ("microsoft.logic/workflows", "RunsFailed"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.logic/workflows", "RunsThrottled"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.datafactory/factories", "PipelineFailedRuns"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.datafactory/factories", "TriggerFailedRuns"): {"aggregation": "Total", "ceiling": None},
    ("microsoft.datafactory/factories", "ActivityFailedRuns"): {"aggregation": "Total", "ceiling": None},
}

_DEFAULT = {"aggregation": "Average", "ceiling": None, "higher_is_worse": True}


def metric_semantics(arm_type: str, metric: str, unit: str) -> dict[str, Any]:
    """Return {aggregation, ceiling, higher_is_worse} for an AMBA metric. Falls back to
    Average aggregation; ceiling=100 for %-unit metrics; higher_is_worse=True."""
    key = (str(arm_type).lower(), metric)
    overlay = _OVERLAY.get(key)
    if overlay:
        out = dict(_DEFAULT)
        out.update(overlay)
        return out
    out = dict(_DEFAULT)
    if (unit or "").strip() == "%":
        out["ceiling"] = 100
    return out
