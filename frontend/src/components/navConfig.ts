// Lightweight navigation config (data + types only, no heavy component imports).
// Kept separate so ChatView can render the sidebar nav and parse routes without
// pulling the heavy Admin/Automations/Monitor panels into the main bundle — those
// are lazy-loaded (code-split) on first navigation.

// ---- Settings (admin) -----------------------------------------------------------
export type SecuritySection =
  | "users"
  | "roles"
  | "groups"
  | "identity"
  | "sessions"
  | "policies";

export type AdminSection =
  | "providers"
  | "tenants"
  | "sandboxvms"
  | "connectors"
  | "settings"
  | "prompts"
  | "scoring"
  | "amba"
  | "ambachanges"
  | "telemetry"
  | "telemetrychanges"
  | "backupdr"
  | "backupdrchanges"
  | "radar"
  | "access"
  | "tools"
  | "entratools"
  | "usage"
  | "audit"
  | "backup"
  | "demodata"
  | SecuritySection;

// Full security nav (kept for the Active Sessions / Security Policy items + lookups).
export const SECURITY_NAV: { id: SecuritySection; label: string; icon: string }[] = [
  { id: "users", label: "Users", icon: "👤" },
  { id: "roles", label: "Roles", icon: "🛡️" },
  { id: "groups", label: "Groups", icon: "👥" },
  { id: "identity", label: "Sign-in & SSO", icon: "🔑" },
  { id: "sessions", label: "Active Sessions", icon: "🖥️" },
  { id: "policies", label: "Security Policy", icon: "🔒" },
];

// Sub-tabs grouped under the "Access Control" page.
export const ACCESS_NAV: { id: SecuritySection; label: string; icon: string }[] = [
  { id: "users", label: "Users", icon: "👤" },
  { id: "roles", label: "Roles", icon: "🛡️" },
  { id: "groups", label: "Groups", icon: "👥" },
  { id: "identity", label: "Sign-in & SSO", icon: "🔑" },
];

export const ACCESS_SUB_IDS = new Set<string>(ACCESS_NAV.map((n) => n.id));

// Settings sidebar sub-menu. Users/Roles/Groups/Sign-in & SSO live under Access Control.
// Ordered in logical clusters (the `group` marks the first item of each cluster so the
// UI can draw a subtle divider): core configuration, connections/integrations,
// security & access, then observability.
export const ADMIN_NAV: { id: AdminSection; label: string; icon: string; group?: string }[] = [
  // Core configuration
  { id: "settings", label: "General", icon: "⚙️", group: "Configuration" },
  { id: "providers", label: "AI Providers", icon: "🧠" },
  { id: "prompts", label: "System Prompts", icon: "📝" },
  { id: "scoring", label: "Assessments & Architecture", icon: "📐" },
  { id: "amba", label: "AMBA Reference Set", icon: "📡" },
  { id: "ambachanges", label: "AMBA Change Requests", icon: "📥" },
  { id: "telemetry", label: "Telemetry Reference Set", icon: "📊" },
  { id: "telemetrychanges", label: "Telemetry Change Requests", icon: "📝" },
  { id: "backupdr", label: "Backup/DR Reference Set", icon: "🔁" },
  { id: "backupdrchanges", label: "Backup/DR Change Requests", icon: "💾" },
  { id: "radar", label: "Retirement Radar Reference", icon: "📡" },
  // Connections & integrations
  { id: "tenants", label: "Azure Tenants", icon: "🏢", group: "Connections" },
  { id: "sandboxvms", label: "Sandbox VMs", icon: "🖥️" },
  { id: "connectors", label: "Connectors", icon: "🔌" },
  { id: "tools", label: "Azure MCP Tools", icon: "🧰" },
  { id: "entratools", label: "EntraID MCP Tools", icon: "🆔" },
  // Security & access
  { id: "access", label: "Access Control", icon: "🔐", group: "Security & access" },
  { id: "policies", label: "Security Policy", icon: "🔒" },
  { id: "sessions", label: "Active Sessions", icon: "🖥️" },
  // Observability
  { id: "usage", label: "Usage", icon: "📊", group: "Observability" },
  { id: "audit", label: "Audit Log", icon: "📋" },
  { id: "backup", label: "Backup & Restore", icon: "💾" },
  { id: "demodata", label: "Demo Data", icon: "🎬" },
];

// Every valid /admin/:section id — top-level nav items plus the Access Control sub-tabs.
export const ADMIN_SECTION_IDS = new Set<AdminSection>([
  ...ADMIN_NAV.map((n) => n.id),
  ...SECURITY_NAV.map((n) => n.id),
]);

// ---- Automations ----------------------------------------------------------------
export type AutomationsSection =
  | "overview"
  | "tasks"
  | "agents"
  | "connectors"
  | "workbooks"
  | "playbooks"
  | "notifications";

export const AUTOMATIONS_NAV: {
  id: Exclude<AutomationsSection, "overview">;
  label: string;
  icon: string;
  description: string;
}[] = [
  {
    id: "tasks",
    label: "Scheduled Tasks",
    icon: "⏰",
    description: "Recurring agent workflows that run on a schedule.",
  },
  {
    id: "workbooks",
    label: "Workbooks",
    icon: "📓",
    description: "Saved az / Resource Graph / PowerShell operations with AI-summarized output.",
  },
  {
    id: "playbooks",
    label: "Playbooks",
    icon: "🧩",
    description: "Chain workbooks into multi-step, conditional flows.",
  },
  {
    id: "notifications",
    label: "Notifications",
    icon: "🔔",
    description: "Route events to Teams, Slack, email and the in-app center.",
  },
];

// ---- Azure Policy ---------------------------------------------------------------
// Sub-tabs of the governance toolkit, driven by the /policy/:tab URL so a refresh (or a
// shared link) restores the same view.
export type PolicyTab =
  | "overview"
  | "inventory"
  | "effective"
  | "advisors"
  | "rollout"
  | "ai"
  | "drift"
  | "history";

export const POLICY_NAV: { id: PolicyTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "inventory", label: "Inventory" },
  { id: "effective", label: "Effective policy" },
  { id: "advisors", label: "Advisors" },
  { id: "rollout", label: "🚦 Rollout Planner" },
  { id: "ai", label: "AI tools" },
  { id: "drift", label: "Drift & IaC" },
  { id: "history", label: "History" },
];

export const POLICY_TAB_IDS = new Set<PolicyTab>(POLICY_NAV.map((n) => n.id));

// ---- Inventory ------------------------------------------------------------------
// Sub-tabs of the Inventory screen, driven by the /inventory/:tab URL so a refresh (or a
// shared link) restores the same view. "grid" is the default (bare /inventory).
export type InventoryTab = "grid" | "overview" | "location" | "cost" | "optimization" | "changes";

export const INVENTORY_NAV: { id: InventoryTab; label: string }[] = [
  { id: "grid", label: "📋 Grid" },
  { id: "overview", label: "📊 Overview" },
  { id: "location", label: "🌍 Location" },
  { id: "cost", label: "💰 Cost" },
  { id: "optimization", label: "🧹 Optimization" },
  { id: "changes", label: "🕑 Changes" },
];

export const INVENTORY_TAB_IDS = new Set<InventoryTab>(INVENTORY_NAV.map((n) => n.id));

// ---- RBAC / Access Review -------------------------------------------------------
// Sub-tabs of the RBAC access-review screen, driven by the /rbac/:tab URL so a refresh (or a
// shared link) restores the same view. "overview" is the default (bare /rbac). The 7 tabs
// collapse the standalone scanner's 25 workbook sheets into task-oriented views.
export type RbacTab =
  | "overview"
  | "effective"
  | "privileged"
  | "scopes"
  | "roles"
  | "insights"
  | "diagnostics";

export const RBAC_NAV: { id: RbacTab; label: string }[] = [
  { id: "overview", label: "📊 Overview" },
  { id: "effective", label: "🧩 Effective Access" },
  { id: "privileged", label: "🛡️ Privileged & Exposure" },
  { id: "scopes", label: "🗂️ Scopes" },
  { id: "roles", label: "🎫 Roles & Principals" },
  { id: "insights", label: "📈 Insights" },
  { id: "diagnostics", label: "🩺 Diagnostics" },
];

export const RBAC_TAB_IDS = new Set<RbacTab>(RBAC_NAV.map((n) => n.id));

// ---- Identity -------------------------------------------------------------------
// Sub-tabs of the Identity screen, driven by the /identity/:tab URL so a refresh (or a
// shared link) restores the same view. "overview" is the default (bare /identity).
export type IdentityTab = "overview" | "app-registrations";

export const IDENTITY_NAV: { id: IdentityTab; label: string }[] = [
  { id: "overview", label: "🔍 Security Findings" },
  { id: "app-registrations", label: "📝 App Registrations" },
];

export const IDENTITY_TAB_IDS = new Set<IdentityTab>(IDENTITY_NAV.map((n) => n.id));
