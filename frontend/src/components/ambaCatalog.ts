// Known Azure Monitor metric catalog per ARM resource type — a convenience layer for the
// AMBA Reference editor's "add from catalog" + metric autocomplete. Purely client-side;
// any free-text metric is still allowed. Seeded from the built-in AMBA reference plus a
// few common extras, with sensible default unit/operator/window/threshold/category so a
// metric can be added with one click instead of from memory.

export type CatalogMetric = {
  metric: string;
  label: string;
  unit: string;
  operator: "GreaterThan" | "LessThan" | "GreaterOrLessThan" | "Equals";
  window: string;
  threshold: number | null;
  amba_category: "availability" | "performance" | "security";
  why?: string;
};

export const AMBA_OPERATORS = ["GreaterThan", "LessThan", "GreaterOrLessThan", "Equals"] as const;
export const AMBA_OPERATOR_SYMBOL: Record<string, string> = {
  GreaterThan: ">",
  LessThan: "<",
  GreaterOrLessThan: "≷",
  Equals: "=",
};
export const AMBA_SEVERITIES = ["critical", "error", "warning", "info"] as const;
export const AMBA_CATEGORIES = ["availability", "performance", "security"] as const;
export const AMBA_WINDOWS = ["PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D"];
export const AMBA_UNITS = ["%", "count", "ms", "s", "bytes", "RU", "bps", "flag", "ncores"];

export const CATEGORY_COLOR: Record<string, string> = {
  availability: "#dc2626",
  performance: "#2563eb",
  security: "#b91c1c",
};

const M = (
  metric: string,
  label: string,
  unit: string,
  operator: CatalogMetric["operator"],
  amba_category: CatalogMetric["amba_category"],
  threshold: number | null = null,
  window = "PT5M",
  why = "",
): CatalogMetric => ({ metric, label, unit, operator, window, threshold, amba_category, why });

// arm_type (lowercase) -> candidate metrics
export const METRIC_CATALOG: Record<string, CatalogMetric[]> = {
  "microsoft.compute/virtualmachines": [
    M("Percentage CPU", "CPU utilization", "%", "GreaterThan", "performance", 90),
    M("Available Memory Bytes", "Available memory", "bytes", "LessThan", "performance", 1073741824),
    M("OS Disk IOPS Consumed Percentage", "OS disk IOPS consumed", "%", "GreaterThan", "performance", 95),
    M("Network In Total", "Inbound network", "bytes", "GreaterThan", "performance", null),
    M("Network Out Total", "Outbound network", "bytes", "GreaterThan", "performance", null),
    M("VmAvailabilityMetric", "VM availability", "count", "LessThan", "availability", 1),
  ],
  "microsoft.compute/virtualmachinescalesets": [
    M("Percentage CPU", "CPU utilization", "%", "GreaterThan", "performance", 90),
    M("Available Memory Bytes", "Available memory", "bytes", "LessThan", "performance", 1073741824),
  ],
  "microsoft.web/sites": [
    M("Http5xx", "HTTP 5xx errors", "count", "GreaterThan", "availability", 10),
    M("HttpResponseTime", "Response time", "s", "GreaterThan", "performance", 5),
    M("CpuTime", "CPU time", "s", "GreaterThan", "performance", null),
    M("HealthCheckStatus", "Health check status", "%", "LessThan", "availability", 100),
    M("Requests", "Requests", "count", "GreaterThan", "performance", null),
    M("MemoryWorkingSet", "Memory working set", "bytes", "GreaterThan", "performance", null),
  ],
  "microsoft.web/serverfarms": [
    M("CpuPercentage", "Plan CPU", "%", "GreaterThan", "performance", 90),
    M("MemoryPercentage", "Plan memory", "%", "GreaterThan", "performance", 90),
  ],
  "microsoft.sql/servers/databases": [
    M("dtu_consumption_percent", "DTU / compute utilization", "%", "GreaterThan", "performance", 90),
    M("storage_percent", "Storage utilization", "%", "GreaterThan", "availability", 90),
    M("deadlock", "Deadlocks", "count", "GreaterThan", "performance", 1, "PT15M"),
    M("cpu_percent", "CPU utilization", "%", "GreaterThan", "performance", 90),
    M("connection_failed", "Failed connections", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.storage/storageaccounts": [
    M("Availability", "Availability", "%", "LessThan", "availability", 99),
    M("SuccessE2ELatency", "Success E2E latency", "ms", "GreaterThan", "performance", 1000),
    M("Transactions", "Transactions / throttling", "count", "GreaterThan", "availability", null),
    M("UsedCapacity", "Used capacity", "bytes", "GreaterThan", "performance", null),
  ],
  "microsoft.containerservice/managedclusters": [
    M("node_cpu_usage_percentage", "Node CPU", "%", "GreaterThan", "performance", 90),
    M("node_memory_working_set_percentage", "Node memory working set", "%", "GreaterThan", "performance", 90),
    M("kube_node_status_condition", "Nodes not ready", "count", "GreaterThan", "availability", 0),
    M("kube_pod_status_restarts_total", "Pod restarts", "count", "GreaterThan", "availability", null, "PT15M"),
  ],
  "microsoft.keyvault/vaults": [
    M("Availability", "Vault availability", "%", "LessThan", "availability", 99),
    M("ServiceApiLatency", "Request latency", "ms", "GreaterThan", "performance", 1000),
    M("SaturationShoebox", "API saturation", "%", "GreaterThan", "performance", 75),
  ],
  "microsoft.cache/redis": [
    M("serverLoad", "Server load", "%", "GreaterThan", "performance", 90, "PT5M"),
    M("usedmemorypercentage", "Used memory percentage", "%", "GreaterThan", "performance", 90),
    M("connectedclients", "Connected clients", "count", "GreaterThan", "performance", null),
    M("errors", "Errors", "count", "GreaterThan", "availability", 0),
  ],
  "microsoft.documentdb/databaseaccounts": [
    M("TotalRequestUnits", "Request units (429 risk)", "RU", "GreaterThan", "availability", null),
    M("ServiceAvailability", "Service availability", "%", "LessThan", "availability", 100, "PT1H"),
    M("ProvisionedThroughput", "Provisioned throughput", "RU", "GreaterThan", "performance", null),
  ],
  "microsoft.network/applicationgateways": [
    M("UnhealthyHostCount", "Unhealthy host count", "count", "GreaterThan", "availability", 0),
    M("FailedRequests", "Failed requests", "count", "GreaterThan", "availability", null),
    M("ResponseStatus", "Response status (5xx)", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.network/loadbalancers": [
    M("DipAvailability", "Health probe status", "%", "LessThan", "availability", 90),
    M("VipAvailability", "Data path availability", "%", "LessThan", "availability", 90),
  ],
  "microsoft.network/azurefirewalls": [
    M("SNATPortUtilization", "SNAT port utilization", "%", "GreaterThan", "availability", 80),
    M("Throughput", "Throughput", "bps", "GreaterThan", "performance", null),
    M("FirewallHealth", "Firewall health", "%", "LessThan", "availability", 100),
  ],
  "microsoft.network/publicipaddresses": [
    M("IfUnderDDoSAttack", "Under DDoS attack", "flag", "GreaterThan", "availability", 0),
    M("DDoSTriggerTCPPacketsInDDoS", "DDoS packets dropped", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.servicebus/namespaces": [
    M("ServerErrors", "Server errors", "count", "GreaterThan", "availability", 0),
    M("ThrottledRequests", "Throttled requests", "count", "GreaterThan", "availability", 0),
    M("DeadletteredMessages", "Dead-lettered messages", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("ActiveMessages", "Active message backlog", "count", "GreaterThan", "performance", null, "PT15M"),
  ],
  "microsoft.eventhub/namespaces": [
    M("ServerErrors", "Server errors", "count", "GreaterThan", "availability", 0),
    M("ThrottledRequests", "Throttled requests", "count", "GreaterThan", "availability", 0),
    M("CaptureBacklog", "Capture backlog", "count", "GreaterThan", "performance", null, "PT15M"),
  ],
  "microsoft.eventgrid/topics": [
    M("PublishFailCount", "Publish failures", "count", "GreaterThan", "availability", 0),
    M("DeadLetteredCount", "Dead-lettered events", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("DeliveryAttemptFailCount", "Delivery attempt failures", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.logic/workflows": [
    M("RunsFailed", "Runs failed", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("RunsThrottled", "Runs throttled", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("ActionLatency", "Action latency", "ms", "GreaterThan", "performance", null, "PT15M"),
  ],
  "microsoft.cognitiveservices/accounts": [
    M("ClientErrors", "Throttled (429) calls", "count", "GreaterThan", "availability", null),
    M("Latency", "Response latency", "ms", "GreaterThan", "performance", 5000),
    M("ServerErrors", "Server (5xx) errors", "count", "GreaterThan", "availability", 0),
    M("TotalTokenCalls", "Token calls", "count", "GreaterThan", "performance", null),
  ],
  "microsoft.search/searchservices": [
    M("ThrottledSearchQueriesPercentage", "Throttled search queries", "%", "GreaterThan", "availability", 5),
    M("SearchLatency", "Search latency", "ms", "GreaterThan", "performance", 1000),
  ],
  "microsoft.machinelearningservices/workspaces": [
    M("Failed Runs", "Failed pipeline/job runs", "count", "GreaterThan", "availability", 0, "PT1H"),
    M("Quota Utilization Percentage", "Quota utilization", "%", "GreaterThan", "performance", 90, "PT15M"),
  ],
  "microsoft.datafactory/factories": [
    M("PipelineFailedRuns", "Failed pipeline runs", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("TriggerFailedRuns", "Failed trigger runs", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("ActivityFailedRuns", "Failed activity runs", "count", "GreaterThan", "availability", 0, "PT15M"),
  ],
  "microsoft.synapse/workspaces": [
    M("BuiltinSqlPoolRequestsEnded", "Failed SQL requests", "count", "GreaterThan", "availability", null, "PT15M"),
  ],
  "microsoft.apimanagement/service": [
    M("Capacity", "Capacity", "%", "GreaterThan", "performance", 80),
    M("FailedRequests", "Gateway 5xx errors", "count", "GreaterThan", "availability", 10),
    M("BackendDuration", "Backend latency", "ms", "GreaterThan", "performance", 2000),
  ],
  "microsoft.app/containerapps": [
    M("RestartCount", "Replica restart count", "count", "GreaterThan", "availability", 0, "PT15M"),
    M("Requests", "5xx responses", "count", "GreaterThan", "availability", null),
    M("UsageNanoCores", "CPU usage", "ncores", "GreaterThan", "performance", null),
  ],
  "microsoft.containerregistry/registries": [
    M("TotalPullCount", "Throttled pulls/pushes", "count", "GreaterThan", "availability", null),
    M("StorageUsed", "Storage used", "bytes", "GreaterThan", "performance", null, "PT1H"),
  ],
  "microsoft.dbforpostgresql/flexibleservers": [
    M("cpu_percent", "CPU utilization", "%", "GreaterThan", "performance", 90),
    M("memory_percent", "Memory utilization", "%", "GreaterThan", "performance", 90),
    M("storage_percent", "Storage utilization", "%", "GreaterThan", "availability", 90),
    M("connections_failed", "Failed connections", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.dbformysql/flexibleservers": [
    M("cpu_percent", "CPU utilization", "%", "GreaterThan", "performance", 90),
    M("memory_percent", "Memory utilization", "%", "GreaterThan", "performance", 90),
    M("storage_percent", "Storage utilization", "%", "GreaterThan", "availability", 90),
    M("aborted_connections", "Aborted connections", "count", "GreaterThan", "availability", null),
  ],
  "microsoft.network/frontdoors": [
    M("BackendHealthPercentage", "Backend health", "%", "LessThan", "availability", 100),
    M("TotalLatency", "Total latency", "ms", "GreaterThan", "performance", 1000),
  ],
  "microsoft.cdn/profiles": [
    M("OriginHealthPercentage", "Origin health", "%", "LessThan", "availability", 100),
    M("Percentage4XX", "4xx error rate", "%", "GreaterThan", "availability", 5),
    M("Percentage5XX", "5xx error rate", "%", "GreaterThan", "availability", 1),
  ],
  "microsoft.operationalinsights/workspaces": [
    M("Ingestion Volume", "Ingestion volume spike", "bytes", "GreaterThan", "performance", null, "PT1H"),
  ],
};

export function catalogFor(armType: string): CatalogMetric[] {
  return METRIC_CATALOG[(armType || "").toLowerCase()] || [];
}

// A flat list of all known arm types in the catalog (for the "add resource type" picker).
export const KNOWN_ARM_TYPES: { type: string; label: string; category: string }[] = [
  { type: "microsoft.compute/virtualmachines", label: "Virtual Machine", category: "compute" },
  { type: "microsoft.compute/virtualmachinescalesets", label: "VM Scale Set", category: "compute" },
  { type: "microsoft.web/sites", label: "App Service", category: "web" },
  { type: "microsoft.web/serverfarms", label: "App Service Plan", category: "compute" },
  { type: "microsoft.web/sites/functions", label: "Function App", category: "compute" },
  { type: "microsoft.sql/servers/databases", label: "SQL Database", category: "data" },
  { type: "microsoft.storage/storageaccounts", label: "Storage Account", category: "storage" },
  { type: "microsoft.containerservice/managedclusters", label: "AKS Cluster", category: "containers" },
  { type: "microsoft.keyvault/vaults", label: "Key Vault", category: "security" },
  { type: "microsoft.cache/redis", label: "Redis Cache", category: "data" },
  { type: "microsoft.documentdb/databaseaccounts", label: "Cosmos DB", category: "data" },
  { type: "microsoft.network/applicationgateways", label: "Application Gateway", category: "network" },
  { type: "microsoft.network/loadbalancers", label: "Load Balancer", category: "network" },
  { type: "microsoft.network/azurefirewalls", label: "Azure Firewall", category: "network" },
  { type: "microsoft.network/publicipaddresses", label: "Public IP", category: "network" },
  { type: "microsoft.network/frontdoors", label: "Front Door (classic)", category: "network" },
  { type: "microsoft.cdn/profiles", label: "Front Door (Std/Premium)", category: "network" },
  { type: "microsoft.servicebus/namespaces", label: "Service Bus", category: "integration" },
  { type: "microsoft.eventhub/namespaces", label: "Event Hubs", category: "integration" },
  { type: "microsoft.eventgrid/topics", label: "Event Grid", category: "integration" },
  { type: "microsoft.logic/workflows", label: "Logic App", category: "integration" },
  { type: "microsoft.apimanagement/service", label: "API Management", category: "integration" },
  { type: "microsoft.app/containerapps", label: "Container App", category: "containers" },
  { type: "microsoft.containerregistry/registries", label: "Container Registry", category: "containers" },
  { type: "microsoft.dbforpostgresql/flexibleservers", label: "PostgreSQL Flexible", category: "data" },
  { type: "microsoft.dbformysql/flexibleservers", label: "MySQL Flexible", category: "data" },
  { type: "microsoft.cognitiveservices/accounts", label: "Azure AI / OpenAI", category: "ai" },
  { type: "microsoft.search/searchservices", label: "AI Search", category: "ai" },
  { type: "microsoft.machinelearningservices/workspaces", label: "ML Workspace", category: "ai" },
  { type: "microsoft.datafactory/factories", label: "Data Factory", category: "analytics" },
  { type: "microsoft.synapse/workspaces", label: "Synapse", category: "analytics" },
  { type: "microsoft.insights/components", label: "App Insights", category: "monitoring" },
  { type: "microsoft.operationalinsights/workspaces", label: "Log Analytics", category: "monitoring" },
];
