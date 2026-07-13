// Canonical public documentation registry. Application code must link to the published
// GitHub Pages portal rather than raw Markdown source files in the repository.
export const DOCS_PORTAL_BASE = "https://zmustafa.github.io/AzureSupportAgent";

export function docsUrl(path = "/"): string {
  const normalized = path === "/" ? "/" : `/${path.replace(/^\/+|\/+$/g, "")}/`;
  return `${DOCS_PORTAL_BASE}${normalized}`;
}

export type DocumentationTarget = {
  label: string;
  guide: string;
  howTo?: string;
  additional?: Array<{ label: string; href: string }>;
};

const target = (label: string, guide: string, howTo?: string, additional?: DocumentationTarget["additional"]): DocumentationTarget => ({
  label,
  guide: docsUrl(guide),
  howTo: howTo ? docsUrl(howTo) : undefined,
  additional,
});

// Longest-prefix matching lets detail/tab routes inherit their owning section's guide.
export const DOCUMENTATION_TARGETS: Readonly<Record<string, DocumentationTarget>> = {
  "/workloads/groups": target("Workload groups and overlaps", "/user-guide/workloads/groups-overlaps", "/how-to/core-workloads/workload-detail-groups"),
  "/workloads/overlaps": target("Workload groups and overlaps", "/user-guide/workloads/groups-overlaps", "/how-to/core-workloads/workload-detail-groups"),
  "/workloads": target("Azure Workloads", "/user-guide/workloads", "/how-to/core-workloads"),
  "/mission-control": target("Mission Control", "/user-guide/mission-control", "/how-to/core-workloads/mission-control"),
  "/dashboard": target("Dashboard", "/user-guide/core/dashboard", "/how-to/core-workloads/dashboard-chat"),
  "/chat": target("Chat and Deep Investigation", "/user-guide/core/chat-deep-investigation", "/how-to/core-workloads/dashboard-chat"),
  "/c": target("Chat and Deep Investigation", "/user-guide/core/chat-deep-investigation", "/how-to/core-workloads/dashboard-chat"),
  "/proactive": target("Proactive Support", "/user-guide/core/proactive-monitor-stats", "/how-to/core-workloads/proactive-monitor-stats"),
  "/monitor": target("Monitor", "/user-guide/core/proactive-monitor-stats", "/how-to/core-workloads/proactive-monitor-stats"),
  "/stats": target("Stats", "/user-guide/core/proactive-monitor-stats", "/how-to/core-workloads/proactive-monitor-stats"),

  "/insights": target("AI Insight Packs", "/user-guide/design-ownership/ai-insight-packs", "/how-to/design-assessment/insight-packs"),
  "/architectures/memory": target("Architecture Memory", "/user-guide/design-ownership/architectures", "/how-to/design-assessment/architectures-know-me"),
  "/architectures": target("Architectures", "/user-guide/design-ownership/architectures", "/how-to/design-assessment/architectures-know-me"),
  "/knowme": target("Know-Me", "/user-guide/design-ownership/know-me", "/how-to/design-assessment/architectures-know-me"),
  "/ownership": target("Ownership", "/user-guide/design-ownership/ownership", "/how-to/design-assessment/ownership"),
  "/graph": target("Estate Graph", "/user-guide/design-ownership/estate-graph", "/how-to/design-assessment/estate-graph"),
  "/assessments": target("Assessments", "/user-guide/assessment-performance/assessments", "/how-to/design-assessment/assessments"),
  "/performance": target("Performance Profiler", "/user-guide/assessment-performance/performance-profiler", "/how-to/design-assessment/performance-profiler"),
  "/fmea": target("FMEA", "/user-guide/assessment-performance/fmea", "/how-to/design-assessment/fmea"),

  "/alerts-manager": target("Alerts Manager", "/user-guide/coverage/alerts-manager", "/how-to/coverage/alerts-manager"),
  "/coverage": target("Monitoring Coverage", "/user-guide/coverage/monitoring-coverage", "/how-to/coverage/monitoring-coverage"),
  "/telemetry": target("Telemetry Coverage", "/user-guide/coverage/telemetry-coverage", "/how-to/coverage/telemetry-coverage"),
  "/backupdr": target("Backup & DR Coverage", "/user-guide/coverage/backup-dr-coverage", "/how-to/coverage/backup-dr-coverage"),
  "/capability": target("Connection Capability", "/user-guide/coverage/connection-capability", "/how-to/coverage/connection-capability"),

  "/inventory": target("Inventory", "/user-guide/estate-intelligence/inventory", "/how-to/estate-intelligence/inventory"),
  "/tagintel": target("Tag Intelligence", "/user-guide/estate-intelligence/tag-intelligence", "/how-to/estate-intelligence/tag-intelligence"),
  "/change-explorer": target("Change Explorer", "/user-guide/estate-intelligence/change-explorer", "/how-to/estate-intelligence/change-explorer"),

  "/policy": target("Azure Policy", "/user-guide/governance-identity/azure-policy", "/how-to/governance-identity"),
  "/identity": target("Identity", "/user-guide/governance-identity/identity", "/how-to/governance-identity/identity-reviews"),
  "/rbac": target("RBAC", "/user-guide/governance-identity/rbac", "/how-to/governance-identity/rbac-access-reviews"),

  "/radar": target("Retirement Radar", "/user-guide/lifecycle-investigation/retirement-radar", "/how-to/lifecycle-investigation/retirement-radar"),
  "/reservations": target("Reservations Monitor", "/user-guide/lifecycle-investigation/reservations-monitor", "/how-to/lifecycle-investigation/reservations-monitor"),
  "/quota": target("Quota Monitor", "/user-guide/lifecycle-investigation/quota-monitor", "/how-to/lifecycle-investigation/quota-monitor"),
  "/telemetry-intel": target("Telemetry Intelligence", "/user-guide/lifecycle-investigation/telemetry-intelligence", "/how-to/lifecycle-investigation/telemetry-intelligence"),
  "/evidence": target("Evidence Locker", "/user-guide/lifecycle-investigation/evidence-locker", "/how-to/lifecycle-investigation/evidence-locker"),
  "/cases": target("Case Files", "/user-guide/lifecycle-investigation/case-files", "/how-to/lifecycle-investigation/case-files"),

  "/automations/tasks": target("Scheduled Tasks", "/user-guide/automations/scheduled-tasks", "/how-to/automations-connectors/scheduled-tasks"),
  "/automations/workbooks": target("Workbooks", "/user-guide/automations/workbooks", "/how-to/automations-connectors/workbooks"),
  "/automations/playbooks": target("Playbooks", "/user-guide/automations/playbooks", "/how-to/automations-connectors/playbooks"),
  "/automations/agents": target("Sub Agents", "/user-guide/automations/sub-agents", "/how-to/automations-connectors/sub-agents"),
  "/automations/notifications": target("Notifications", "/user-guide/automations/notifications", "/how-to/automations-connectors/notifications"),
  "/automations/connectors": target("Connectors", "/connectors", "/how-to/automations-connectors"),
  "/automations": target("Automations", "/user-guide/automations", "/how-to/automations-connectors"),
  "/notifications": target("Notifications", "/user-guide/automations/notifications", "/how-to/automations-connectors/notifications"),

  "/admin/providers": target("AI Providers", "/admin/ai-providers", "/how-to/administration/ai-providers"),
  "/admin/tenants": target("Azure Tenants", "/admin/azure-tenants-sandbox-vms", "/how-to/administration/azure-tenants"),
  "/admin/sandboxvms": target("Sandbox VMs", "/admin/azure-tenants-sandbox-vms", "/how-to/administration/sandbox-vms"),
  "/admin/settings": target("General Settings", "/admin/general-settings", "/how-to/administration/general-settings"),
  "/admin/connectors": target("Connectors", "/connectors", "/how-to/administration/connectors"),
  "/admin/access": target("Access Control", "/admin/access-control", "/how-to/administration/access-control"),
  "/admin/users": target("Users", "/admin/access-control", "/how-to/administration/access-control"),
  "/admin/roles": target("Roles", "/admin/access-control", "/how-to/administration/access-control"),
  "/admin/groups": target("Groups", "/admin/access-control", "/how-to/administration/access-control"),
  "/admin/identity": target("Sign-in and SSO", "/admin/access-control", "/how-to/administration/access-control"),
  "/admin/policies": target("Security Policy", "/admin/security-policy-sessions", "/how-to/administration/security-sessions"),
  "/admin/sessions": target("Active Sessions", "/admin/security-policy-sessions", "/how-to/administration/security-sessions"),
  "/admin/prompts": target("System Prompts", "/admin/prompts-scoring", "/how-to/administration/prompts-scoring"),
  "/admin/scoring": target("Assessments and scoring", "/admin/prompts-scoring", "/how-to/administration/prompts-scoring"),
  "/admin/amba": target("AMBA Reference Set", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/ambachanges": target("AMBA Change Requests", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/telemetry": target("Telemetry Reference Set", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/telemetrychanges": target("Telemetry Change Requests", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/backupdr": target("Backup/DR Reference Set", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/backupdrchanges": target("Backup/DR Change Requests", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/radar": target("Retirement Reference Set", "/admin/reference-sets-change-requests", "/how-to/administration/reference-sets"),
  "/admin/usage": target("Usage", "/admin/usage-audit", "/how-to/administration/usage-audit"),
  "/admin/audit": target("Audit Log", "/admin/usage-audit", "/how-to/administration/usage-audit"),
  "/admin/tools": target("Azure MCP Tools", "/admin/mcp-tools", "/how-to/administration/mcp-tools"),
  "/admin/entratools": target("EntraID MCP Tools", "/admin/mcp-tools", "/how-to/administration/mcp-tools"),
  "/admin/backup": target("Backup & Restore", "/admin/backup-demo", "/how-to/administration/backup-demo"),
  "/admin/demodata": target("Demo Data", "/admin/backup-demo", "/how-to/administration/backup-demo"),
  "/admin": target("Administration", "/admin", "/how-to/administration"),
};

export function documentationForPath(pathname: string): DocumentationTarget | undefined {
  const normalized = pathname !== "/" ? pathname.replace(/\/+$/, "") : pathname;
  const key = Object.keys(DOCUMENTATION_TARGETS)
    .filter((prefix) => normalized === prefix || normalized.startsWith(`${prefix}/`))
    .sort((a, b) => b.length - a.length)[0];
  return key ? DOCUMENTATION_TARGETS[key] : undefined;
}

export const CONNECTOR_DOCS: Readonly<Record<string, string>> = {
  teams: docsUrl("/connectors/messaging"), slack: docsUrl("/connectors/messaging"), outlook: docsUrl("/connectors/messaging"), email: docsUrl("/connectors/messaging"),
  jira: docsUrl("/connectors/ticketing-on-call"), servicenow: docsUrl("/connectors/ticketing-on-call"), pagerduty: docsUrl("/connectors/ticketing-on-call"),
  splunk: docsUrl("/connectors/siem-security"), cortex_xsoar: docsUrl("/connectors/siem-security"), sumologic: docsUrl("/connectors/siem-security"), crowdstrike: docsUrl("/connectors/siem-security"), aws_security_hub: docsUrl("/connectors/siem-security"),
  grafana: docsUrl("/connectors/grafana"), webhook: docsUrl("/connectors/webhooks-logic-apps"), azure_logic_apps: docsUrl("/connectors/webhooks-logic-apps"),
  azure_service_bus: docsUrl("/connectors/queues-storage"), amazon_sqs: docsUrl("/connectors/queues-storage"), amazon_s3: docsUrl("/connectors/queues-storage"),
};
