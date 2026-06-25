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
  | "overview"
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
export const ADMIN_NAV: { id: AdminSection; label: string; icon: string; group?: string; desc?: string }[] = [
  // Core configuration
  { id: "providers", label: "AI Providers", icon: "🧠", group: "Configuration", desc: "Configure OpenAI, Azure OpenAI, GitHub Copilot, Claude and other model providers." },
  { id: "tenants", label: "Azure Tenants", icon: "🏢", desc: "Manage Azure tenant connections and service-principal / token credentials." },
  { id: "sandboxvms", label: "Sandbox VMs", icon: "🖥️", desc: "Onboard troubleshooting sandbox VMs for in-guest diagnostics (vm_exec)." },
  { id: "connectors", label: "Connectors", icon: "🔌", desc: "Wire up Teams, Slack, email, Jira, ServiceNow and Grafana integrations." },
  { id: "settings", label: "General", icon: "⚙️", desc: "Core application behavior, safety toggles, and runtime tuning." },
  // Security & access
  { id: "access", label: "Access Control", icon: "🔐", group: "Security & access", desc: "Manage users, roles, groups and single sign-on (OIDC / SAML) providers." },
  { id: "policies", label: "Security Policy", icon: "🔒", desc: "Password rules, lockout, session lifetimes and SSO auto-provisioning defaults." },
  { id: "sessions", label: "Active Sessions", icon: "🖥️", desc: "View and revoke active user sessions across the workspace." },
  // Tool preference — reference sets, change-request inboxes, prompts & scoring tuning.
  { id: "prompts", label: "System Prompts", icon: "📝", group: "Tool Preference", desc: "Edit the system prompts that steer the agent and its sub-agents." },
  { id: "scoring", label: "Assessments & Architecture", icon: "📐", desc: "Tune assessment severity weights, score bands and architecture-designer settings." },
  { id: "amba", label: "AMBA Reference Set", icon: "📡", desc: "Curate the recommended Azure Monitor baseline alerts per resource type." },
  { id: "ambachanges", label: "AMBA Change Requests", icon: "📥", desc: "Review and approve pending changes to monitoring coverage." },
  { id: "telemetry", label: "Telemetry Reference Set", icon: "📊", desc: "Curate the recommended diagnostic-settings categories per resource type." },
  { id: "telemetrychanges", label: "Telemetry Change Requests", icon: "📝", desc: "Review and approve pending changes to telemetry coverage." },
  { id: "backupdr", label: "Backup/DR Reference Set", icon: "🔁", desc: "Curate the recommended backup & disaster-recovery protection checks." },
  { id: "backupdrchanges", label: "Backup/DR Change Requests", icon: "💾", desc: "Review and approve pending changes to backup/DR coverage." },
  { id: "radar", label: "Retirement Radar Reference", icon: "📡", desc: "Tune retirement/breaking-change classification rules and the model-lifecycle table." },
  // Observability
  { id: "usage", label: "Usage", icon: "📊", group: "Observability", desc: "Token usage and estimated cost by AI provider and model." },
  { id: "audit", label: "Audit Log", icon: "📋", desc: "Searchable record of administrative and security-relevant actions." },
  // Miscellaneous
  { id: "tools", label: "Azure MCP Tools", icon: "🧰", group: "Miscellaneous", desc: "Review the Azure MCP tools exposed to the agent and the built-in utilities." },
  { id: "entratools", label: "EntraID MCP Tools", icon: "🆔", desc: "Review the Microsoft Graph (Entra ID) tools available to the agent." },
  { id: "backup", label: "Backup & Restore", icon: "💾", desc: "Export and import the whole-tenant configuration (secret-free)." },
  { id: "demodata", label: "Demo Data", icon: "🎬", desc: "Load or remove the synthetic sample tenant for exploring features." },
];

// Every valid /admin/:section id — top-level nav items plus the Access Control sub-tabs.
export const ADMIN_SECTION_IDS = new Set<AdminSection>([
  ...ADMIN_NAV.map((n) => n.id),
  ...SECURITY_NAV.map((n) => n.id),
]);

// ---- Proactive Support ----------------------------------------------------------
// The posture / forensic / design dashboards, grouped under one expandable sidebar menu
// and surfaced on the /proactive landing page. Mirrors ADMIN_NAV: the `group` field marks
// the FIRST item of each cluster so both the sidebar and the landing page can draw the same
// category subheadings. `icon` is the emoji shown on the landing cards; the sidebar maps the
// item `id` to a line-icon component (see PROACTIVE_ICONS in ChatView). `to` is the route.
export type ProactiveItem = { id: string; to: string; label: string; icon: string; group?: string; desc?: string };

export const PROACTIVE_NAV: ProactiveItem[] = [
  // Design & ownership — how the estate is shaped, who owns it, how it connects.
  { id: "architectures", to: "/architectures", label: "Architectures", icon: "📐", group: "Design & ownership", desc: "Visual application architecture diagrams — hand-drawn or AI-reverse-engineered from a workload." },
  { id: "knowme", to: "/knowme", label: "Know-Me", icon: "📄", desc: "Support-facing workload references transformed from Architecture Memory — triage runbook, known issues, thresholds and a human-completion checklist." },
  { id: "ownership", to: "/ownership", label: "Ownership", icon: "🪪", desc: "Assign accountable owners and teams across subscriptions, workloads and resources." },
  { id: "graph", to: "/graph", label: "Estate Graph", icon: "🕸️", desc: "A workload-aware knowledge graph of the whole tenant with cost, retirement and RBAC overlays." },
  // Assessment & performance — how healthy and well-architected the estate is.
  { id: "assessments", to: "/assessments", label: "Assessments", icon: "✅", group: "Assessment & performance", desc: "Well-Architected and CIS assessments with scored findings, waivers and PDF reports." },
  { id: "performance", to: "/performance", label: "Performance Profiler", icon: "🚀", desc: "Profile workloads against the monitoring baseline and rank bottlenecks on a heatmap." },
  { id: "fmea", to: "/fmea", label: "FMEA", icon: "🧪", desc: "Failure Mode and Effects Analysis — AI-generated, scored risk tables (Severity × Occurrence × Detection → RPN) built from an architecture's Memory." },
  // Coverage — is the estate monitored, logged and protected.
  { id: "coverage", to: "/coverage", label: "Monitoring Coverage", icon: "📡", group: "Coverage", desc: "Measure Azure Monitor baseline alert coverage and close gaps with generated IaC." },
  { id: "telemetry", to: "/telemetry", label: "Telemetry Coverage", icon: "📊", desc: "Measure diagnostic-settings coverage per resource type against the recommended baseline." },
  { id: "backupdr", to: "/backupdr", label: "Backup & DR Coverage", icon: "🔁", desc: "Audit backup and disaster-recovery protection and generate remediation runbooks." },
  // Estate intelligence — what's deployed, how it's tagged, what changed.
  { id: "inventory", to: "/inventory", label: "Inventory", icon: "📋", group: "Estate intelligence", desc: "A unified resource grid with overview, location, cost and optimization lenses." },
  { id: "tagintel", to: "/tagintel", label: "Tag Intelligence", icon: "🏷️", desc: "Tag census, hygiene, coverage, cost allocation, drift and policy generation." },
  { id: "change-explorer", to: "/change-explorer", label: "Change Explorer", icon: "🕑", desc: "Analyze what changed in a workload over a time window, by risk, actor and dependency." },
  // Governance & identity — policy, identity posture and access review.
  { id: "policy", to: "/policy", label: "Azure Policy", icon: "🛡️", group: "Governance & identity", desc: "Policy inventory, compliance, effective policy, rollout planning and drift / IaC." },
  { id: "identity", to: "/identity", label: "Identity", icon: "🆔", desc: "Identity security findings (expiry, MFA, secrets) and app-registration hygiene." },
  { id: "rbac", to: "/rbac", label: "RBAC", icon: "🔑", desc: "Azure RBAC access review — effective access, privileged exposure and scopes." },
  // Lifecycle & investigation — what's expiring, what's wrong, and the evidence trail.
  { id: "radar", to: "/radar", label: "Retirement Radar", icon: "🛰️", group: "Lifecycle & investigation", desc: "Track Azure retirements and breaking changes impacting your estate." },
  { id: "reservations", to: "/reservations", label: "Reservations Monitor", icon: "🎟️", desc: "Track reservation order expiry and surface a weekly renewal digest." },
  { id: "quota", to: "/quota", label: "Quota Monitor", icon: "📊", desc: "Subscription/region quota usage, limits, headroom and risk — before deployments fail." },
  { id: "telemetry-intel", to: "/telemetry-intel", label: "Telemetry Intelligence", icon: "🔬", desc: "AI correlation and triage over Application Insights with KQL translation." },
  { id: "evidence", to: "/evidence", label: "Evidence Locker", icon: "🗄️", desc: "Investigation snapshots, diffs, sharing and export for audit trails." },
];

export const PROACTIVE_PATHS = new Set<string>(PROACTIVE_NAV.map((n) => n.to));

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
  | "assignments"
  | "byperson"
  | "bysubscription"
  | "timeline"
  | "pivot"
  | "governance"
  | "exemptions"
  | "effective"
  | "advisors"
  | "rollout"
  | "ai"
  | "drift"
  | "history";

export const POLICY_NAV: { id: PolicyTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "inventory", label: "Inventory" },
  { id: "assignments", label: "📋 Assignments" },
  { id: "byperson", label: "👤 By person" },
  { id: "bysubscription", label: "🗂️ By subscription" },
  { id: "timeline", label: "📈 Timeline" },
  { id: "pivot", label: "🧮 Pivot builder" },
  { id: "governance", label: "🛡️ Governance" },
  { id: "exemptions", label: "🪪 Exemptions" },
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

// ---- Tag Intelligence -----------------------------------------------------------
// Sub-tabs of the Tag Intelligence screen, driven by the /tagintel/:tab URL so a refresh
// (or a shared link) restores the same view. "census" is the default (bare /tagintel). Each
// tab carries a one-line description shown under the title (PageIntro-style).
export type TagIntelTab = "census" | "hygiene" | "coverage" | "cost" | "drift" | "policy" | "generate" | "remediate";

export const TAGINTEL_NAV: { id: TagIntelTab; label: string; description: string }[] = [
  {
    id: "census",
    label: "🔎 Census",
    description:
      "Every tag key and value across your scope — including untagged and partially tagged resources — plus a plain-English console that answers tag questions and shows the Resource Graph query it ran.",
  },
  {
    id: "hygiene",
    label: "🧹 Hygiene",
    description:
      "Find near-duplicate keys, casing drift and value variants (Prod/PRD/Production), build a canonical tag catalog, and see tag-inferred workload boundaries with a confidence score.",
  },
  {
    id: "coverage",
    label: "✅ Coverage",
    description:
      "Measure required-tag coverage per scope and rank the highest-ROI fixes — including resources missing only one required tag — while honoring exceptions for shared/platform services.",
  },
  {
    id: "cost",
    label: "💰 Cost",
    description:
      "Allocate spend by billing code, workload, owner, environment and business unit; expose unallocatable cost from missing billing tags and split shared resources.",
  },
  {
    id: "drift",
    label: "📈 Drift",
    description:
      "Track how tags change over time — keys added or removed, billing-tag value changes and coverage deltas — between snapshots.",
  },
  {
    id: "policy",
    label: "🛡️ Policy",
    description:
      "Generate audit, append, inherit and deny tag policies (plus an initiative) from your real tag usage, following a safe staged rollout.",
  },
  {
    id: "generate",
    label: "✨ AI Generate",
    description:
      "Describe what you want tagged in plain English; the AI proposes a concrete, grounded change-set (every operation resolved to real resources) that you can review and send straight to Remediate.",
  },
  {
    id: "remediate",
    label: "🔧 Remediate",
    description:
      "Fix tags safely: dry-run, preview the exact diff, then generate PowerShell / CLI / Resource Graph / Bicep with a rollback plan — and least-privilege roles shown inline.",
  },
];

export const TAGINTEL_TAB_IDS = new Set<TagIntelTab>(TAGINTEL_NAV.map((n) => n.id));

// ---- Change Explorer ------------------------------------------------------------
// Sub-tabs of the Azure Workload Change Explorer, driven by the /change-explorer/:tab URL so a
// refresh (or a shared link) restores the same view. "summary" is the default.
export type ChangeExplorerTab =
  | "summary" | "timeline" | "changes" | "risk" | "resources" | "actors" | "diff" | "impact" | "export";

export const CHANGEEXPLORER_NAV: { id: ChangeExplorerTab; label: string }[] = [
  { id: "summary", label: "📊 Summary" },
  { id: "timeline", label: "🕑 Timeline" },
  { id: "changes", label: "📋 All Changes" },
  { id: "risk", label: "⚠️ Risk Insights" },
  { id: "resources", label: "📦 Resources" },
  { id: "actors", label: "👤 Actors" },
  { id: "diff", label: "🔬 Technical Diff" },
  { id: "impact", label: "🔗 Dependency Impact" },
  { id: "export", label: "⬇️ Export / Reports" },
];

export const CHANGEEXPLORER_TAB_IDS = new Set<ChangeExplorerTab>(CHANGEEXPLORER_NAV.map((n) => n.id));

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

// ---- Ownership ------------------------------------------------------------------
// The /ownership section's sub-tabs (URL-driven: /ownership/:tab). "directory" is the
// default (bare /ownership). User-level (not admin-only).
export type OwnershipTab =
  | "directory"
  | "assignments"
  | "coverage"
  | "suggestions"
  | "estate"
  | "attestation";

export const OWNERSHIP_NAV: { id: OwnershipTab; label: string }[] = [
  { id: "directory", label: "👥 Owners & Teams" },
  { id: "assignments", label: "🔗 Assignments" },
  { id: "coverage", label: "🎯 Coverage" },
  { id: "suggestions", label: "💡 Suggestions" },
  { id: "estate", label: "🗺️ My Estate" },
  { id: "attestation", label: "✅ Attestation" },
];

export const OWNERSHIP_TAB_IDS = new Set<OwnershipTab>(OWNERSHIP_NAV.map((n) => n.id));

// ---- Identity -------------------------------------------------------------------
// Sub-tabs of the Identity screen, driven by the /identity/:tab URL so a refresh (or a
// shared link) restores the same view. "overview" is the default (bare /identity).
export type IdentityTab = "overview" | "app-registrations";

export const IDENTITY_NAV: { id: IdentityTab; label: string }[] = [
  { id: "overview", label: "🔍 Security Findings" },
  { id: "app-registrations", label: "📝 App Registrations" },
];

export const IDENTITY_TAB_IDS = new Set<IdentityTab>(IDENTITY_NAV.map((n) => n.id));
