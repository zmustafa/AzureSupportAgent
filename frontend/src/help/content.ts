// Centralized in-app help content: command-palette destinations, the glossary (mirrors
// docs/CONCEPTS.md), per-page intros, keyboard shortcuts, and the Trust & Security points.
// Pure data + types — no React — so it can be imported anywhere cheaply.
import { docsUrl } from "./docsRegistry";

export const DOCS_LINKS = {
  index: docsUrl("/"),
  gettingStarted: docsUrl("/getting-started"),
  concepts: docsUrl("/reference/glossary"),
  userGuide: docsUrl("/user-guide"),
  howTo: docsUrl("/how-to"),
  administration: docsUrl("/admin"),
  connectors: docsUrl("/connectors"),
  installation: docsUrl("/getting-started/one-click-install"),
  deployment: docsUrl("/getting-started/manual-deployment"),
  entraSetup: docsUrl("/getting-started/entra-setup"),
  architecture: docsUrl("/ARCHITECTURE"),
  technical: docsUrl("/technical"),
  security: docsUrl("/security"),
  permissions: docsUrl("/reference/permissions"),
  troubleshooting: docsUrl("/reference/troubleshooting"),
} as const;

// ---- Command palette destinations ----------------------------------------------
export type Destination = {
  label: string;
  path: string;
  group: string;
  icon: string;
  keywords?: string;
  adminOnly?: boolean;
};

export const DESTINATIONS: Destination[] = [
  // Core
  { label: "Dashboard", path: "/dashboard", group: "Core", icon: "🏠", keywords: "home overview" },
  { label: "Chat with the agent", path: "/chat", group: "Core", icon: "💬", keywords: "ask question investigate" },
  { label: "Deep investigation", path: "/chat?deep=1", group: "Core", icon: "🔎", keywords: "war room root cause hypothesis" },
  { label: "Azure Workloads", path: "/workloads", group: "Core", icon: "🧩", keywords: "applications groups autopilot discover" },
  { label: "Architectures", path: "/architectures", group: "Core", icon: "🗺️", keywords: "diagram design drift" },
  { label: "Architecture Memory", path: "/architectures/memory", group: "Core", icon: "🧠", keywords: "revisions notes persistent knowledge" },
  { label: "Inventory", path: "/inventory", group: "Core", icon: "📋", keywords: "resources grid cost map" },
  { label: "Inventory · Cost", path: "/inventory/cost", group: "Core", icon: "💰", keywords: "spend billing" },
  { label: "Inventory · Optimization", path: "/inventory/optimization", group: "Core", icon: "🧹", keywords: "savings cleanup waste" },
  { label: "Inventory · Location map", path: "/inventory/location", group: "Core", icon: "🌍", keywords: "region world map" },
  // Actions
  { label: "New chat", path: "/chat", group: "Actions", icon: "💬", keywords: "start conversation new" },
  { label: "Run an assessment", path: "/assessments", group: "Actions", icon: "🛡️", keywords: "scan well-architected new run", adminOnly: true },
  { label: "Export / Backup (download ZIP)", path: "/admin/backup", group: "Actions", icon: "⬇️", keywords: "export backup download zip restore import save", adminOnly: true },
  { label: "Load demo data", path: "/admin/demodata", group: "Actions", icon: "🎬", keywords: "sample explore seed try", adminOnly: true },
  // Proactive Support (admin)
  { label: "Assessments", path: "/assessments", group: "Proactive Support", icon: "🛡️", keywords: "well-architected score pillars cis nist", adminOnly: true },
  { label: "Monitoring Coverage (AMBA)", path: "/coverage", group: "Proactive Support", icon: "📡", keywords: "amba alerts baseline gaps", adminOnly: true },
  { label: "Telemetry Coverage", path: "/telemetry", group: "Proactive Support", icon: "🛰️", keywords: "diagnostic settings logs workspace", adminOnly: true },
  { label: "Backup & DR Coverage", path: "/backupdr", group: "Proactive Support", icon: "💾", keywords: "backup disaster recovery rto rpo", adminOnly: true },
  { label: "Performance Profiler", path: "/performance", group: "Proactive Support", icon: "⚡", keywords: "heatmap bottleneck metrics", adminOnly: true },
  { label: "Retirement Radar", path: "/radar", group: "Proactive Support", icon: "📡", keywords: "retirement breaking change deprecation", adminOnly: true },
  { label: "Reservations Monitor", path: "/reservations", group: "Proactive Support", icon: "🏷️", keywords: "reserved instances savings plan", adminOnly: true },
  { label: "Identity", path: "/identity", group: "Proactive Support", icon: "🔑", keywords: "entra aad mfa secrets certificates", adminOnly: true },
  { label: "RBAC / Access Review", path: "/rbac", group: "Proactive Support", icon: "🛂", keywords: "roles access who can do what", adminOnly: true },
  { label: "Telemetry Intelligence", path: "/telemetry-intel", group: "Proactive Support", icon: "📈", keywords: "log analytics noise cost", adminOnly: true },
  { label: "Evidence Locker", path: "/evidence", group: "Proactive Support", icon: "🗄️", keywords: "snapshot forensic audit hash", adminOnly: true },
  { label: "Change Explorer", path: "/change-explorer", group: "Proactive Support", icon: "🧭", keywords: "what changed who changed it forensic activity log resource graph actor timeline drift", adminOnly: true },
  { label: "Quota Monitor", path: "/quota", group: "Proactive Support", icon: "📊", keywords: "limits usage capacity vcpu cores throttling region provider headroom", adminOnly: true },
  { label: "Tag Intelligence", path: "/tagintel", group: "Proactive Support", icon: "🏷️", keywords: "tags census hygiene coverage cost allocation drift", adminOnly: true },
  { label: "Azure Policy", path: "/policy", group: "Proactive Support", icon: "📐", keywords: "compliance guardrail rollout", adminOnly: true },
  { label: "Azure Policy · Exemptions", path: "/policy/exemptions", group: "Proactive Support", icon: "🪪", keywords: "policy exemption waiver mitigated exclude guardrail expiry", adminOnly: true },
  { label: "Azure Policy · Timeline", path: "/policy/timeline", group: "Proactive Support", icon: "📈", keywords: "policy assignment history slicer when created", adminOnly: true },
  { label: "Azure Policy · Effective policy", path: "/policy/effective", group: "Proactive Support", icon: "🔬", keywords: "effective resolver inheritance notscopes which policies apply", adminOnly: true },
  // Automations & monitor
  { label: "Automations", path: "/automations", group: "Automations", icon: "⚙️", keywords: "schedule sub agents workbooks playbooks" },
  { label: "Scheduled Tasks", path: "/automations/tasks", group: "Automations", icon: "⏰", keywords: "recurring cron schedule" },
  { label: "Sub Agents", path: "/automations/agents", group: "Automations", icon: "✨", keywords: "specialized agent wizard", adminOnly: true },
  { label: "Workbooks", path: "/automations/workbooks", group: "Automations", icon: "📓", keywords: "az resource graph powershell" },
  { label: "Playbooks", path: "/automations/playbooks", group: "Automations", icon: "🧩", keywords: "chain flow steps" },
  { label: "Notifications", path: "/notifications", group: "Automations", icon: "🔔", keywords: "alerts center teams slack" },
  { label: "Monitor", path: "/monitor", group: "Automations", icon: "📊", keywords: "dashboard usage tokens activity", adminOnly: true },
  // Settings (admin)
  { label: "Settings · General", path: "/admin/settings", group: "Settings", icon: "⚙️", keywords: "configuration options", adminOnly: true },
  { label: "Settings · AI Providers", path: "/admin/providers", group: "Settings", icon: "🧠", keywords: "openai claude model key", adminOnly: true },
  { label: "Settings · Azure Tenants", path: "/admin/tenants", group: "Settings", icon: "🏢", keywords: "connection service principal", adminOnly: true },
  { label: "Settings · System Prompts", path: "/admin/prompts", group: "Settings", icon: "📝", keywords: "persona tone instructions", adminOnly: true },
  { label: "Settings · Assessments & Architecture", path: "/admin/scoring", group: "Settings", icon: "📐", keywords: "scoring bands custom controls", adminOnly: true },
  { label: "Settings · AMBA Reference Set", path: "/admin/amba", group: "Settings", icon: "📡", keywords: "baseline alerts reference", adminOnly: true },
  { label: "Settings · Telemetry Reference Set", path: "/admin/telemetry", group: "Settings", icon: "📊", keywords: "diagnostic categories reference", adminOnly: true },
  { label: "Settings · Backup/DR Reference Set", path: "/admin/backupdr", group: "Settings", icon: "🔁", keywords: "protection reference", adminOnly: true },
  { label: "Settings · Retirement Radar Reference", path: "/admin/radar", group: "Settings", icon: "📡", keywords: "retirement reference", adminOnly: true },
  { label: "Settings · Sandbox VMs", path: "/admin/sandboxvms", group: "Settings", icon: "🖥️", keywords: "private endpoint diagnostics jump box", adminOnly: true },
  { label: "Settings · Connectors", path: "/admin/connectors", group: "Settings", icon: "🔌", keywords: "jira servicenow teams slack grafana", adminOnly: true },
  { label: "Settings · Azure MCP Tools", path: "/admin/tools", group: "Settings", icon: "🧰", keywords: "tool catalog read write", adminOnly: true },
  { label: "Settings · EntraID MCP Tools", path: "/admin/entratools", group: "Settings", icon: "🆔", keywords: "graph tools", adminOnly: true },
  { label: "Settings · Access Control", path: "/admin/access", group: "Settings", icon: "🔐", keywords: "users roles groups sso", adminOnly: true },
  { label: "Settings · Security Policy", path: "/admin/policies", group: "Settings", icon: "🔒", keywords: "password lockout session policy", adminOnly: true },
  { label: "Settings · Active Sessions", path: "/admin/sessions", group: "Settings", icon: "🖥️", keywords: "logins devices revoke", adminOnly: true },
  { label: "Settings · Usage", path: "/admin/usage", group: "Settings", icon: "📊", keywords: "tokens cost spend", adminOnly: true },
  { label: "Settings · Audit Log", path: "/admin/audit", group: "Settings", icon: "📋", keywords: "audit history actions", adminOnly: true },
  { label: "Settings · Backup & Restore", path: "/admin/backup", group: "Settings", icon: "💾", keywords: "export import download zip restore save", adminOnly: true },
  { label: "Settings · Demo Data", path: "/admin/demodata", group: "Settings", icon: "🎬", keywords: "sample explore seed", adminOnly: true },
];

// ---- Glossary (mirrors docs/CONCEPTS.md) ---------------------------------------
export type GlossaryTerm = { term: string; short: string; long: string };

export const GLOSSARY: GlossaryTerm[] = [
  { term: "Workload", short: "A named group of Azure resources that make up one app.", long: "Everything is scoped to a Workload — assessments, architectures, and coverage scans all run for a workload. It can mix management groups, subscriptions, resource groups, and individual resources. ✨ Autopilot can discover them with AI." },
  { term: "Architecture", short: "A living, AI-built diagram of a workload.", long: "AI reverse-engineers a diagram from your real resources, grouped into tiers. Refine it by hand, overlay an assessment, run drift detection against live Azure, and save revisions. Architecture Memory persists that knowledge for dashboards and investigations." },
  { term: "Assessment", short: "A Well-Architected score for a workload.", long: "Scores a workload across the five WAF pillars (Security, Reliability, Cost, Operations, Performance) out of 100, with prioritized findings, remediation, and mappings to CIS / NIST / ISO. Findings have a lifecycle and can become tickets." },
  { term: "Deep Investigation (War Room)", short: "Parallel specialist agents find a root cause.", long: "Switch a chat to Deep mode and the agent forms multiple hypotheses and dispatches specialist sub-agents (Networking, Identity, Compute, Storage, Security, Reliability, Cost, Monitoring) that research in parallel, validate with live evidence, and converge on a conclusion. The hypothesis tree is saved with the chat." },
  { term: "Proactive Support", short: "Dashboards that surface risk before you ask.", long: "The umbrella for Assessments, the three Coverage detectors, Identity, RBAC, Retirement Radar, Telemetry Intelligence, Performance Profiler, Reservations Monitor, and the Evidence Locker." },
  { term: "AMBA / Monitoring Coverage", short: "Azure Monitor Baseline Alerts coverage.", long: "Audits which recommended baseline metric alerts are present, missing, or misconfigured per resource type, and generates Bicep / Terraform to close the gaps. Rolls up to Operational Excellence." },
  { term: "Telemetry Coverage", short: "Diagnostic-settings / log coverage.", long: "Audits each resource's diagnostic settings against a reference of recommended log/metric categories, and whether logs ship to an approved Log Analytics workspace (vs. drift). Exports Bicep or an Azure Policy assignment." },
  { term: "Backup & DR Coverage", short: "Backup and disaster-recovery posture.", long: "Audits whether backup is enabled with adequate retention, recent successful jobs, offsite/geo redundancy, a configured & recently-drilled DR pair, and encryption/soft-delete. Exports Bicep + a runbook. Rolls up to Reliability." },
  { term: "Retirement Radar", short: "Azure retirements & breaking changes.", long: "Tracks service retirements and breaking changes mapped to the workloads, owners, and deadlines they affect." },
  { term: "Evidence Locker", short: "Tamper-evident forensic snapshots.", long: "A write-once, SHA-256-stamped snapshot store. Capture a point-in-time bundle (inventory, properties, changes, metrics, findings) scoped to a workload; the hash is re-verified on read, so it's tamper-evident. Coverage scans and investigations can be saved here." },
  { term: "MCP", short: "How the agent talks to tools.", long: "Model Context Protocol. The app ships the official Azure MCP server (~65 Azure tools) and a Microsoft Graph MCP server (~43 Entra ID tools). Tools are classified read vs write." },
  { term: "Connection", short: "An encrypted Azure tenant credential.", long: "A stored, encrypted credential (service principal secret/cert or Azure CLI sign-in) that lets the agent read a tenant. Connect multiple tenants, each isolated, and set a default. Read-only by default." },
  { term: "Sub Agent", short: "A custom specialized agent you define.", long: "Built via an AI-guided wizard with a scoped tool-set and persona; dispatched in deep investigations or run on a schedule." },
  { term: "Workbook / Playbook", short: "Saved operations, optionally chained.", long: "A Workbook is a saved az / Resource Graph / PowerShell operation with AI-summarized output. A Playbook chains workbooks into a multi-step, conditional flow." },
  { term: "Demo data", short: "A synthetic tenant to explore safely.", long: "A complete synthetic tenant (Contoso + Zava workloads with coverage, assessments, identity, …) you can load to try every feature without connecting Azure. Manage it under Settings → Demo Data. It never touches Azure." },
];

// ---- Per-page intros (route prefix → copy) -------------------------------------
export type PageIntroCopy = { title: string; blurb: string; learnMoreHref?: string };

export const PAGE_INTROS: Record<string, PageIntroCopy> = {
  "/coverage": { title: "Monitoring Coverage", blurb: "Audit which recommended Azure Monitor baseline alerts (AMBA) are present, missing, or misconfigured for a scope. Pick a workload and run a scan to start.", learnMoreHref: docsUrl("/user-guide/coverage/monitoring-coverage") },
  "/telemetry": { title: "Telemetry Coverage", blurb: "Audit each resource's diagnostic settings against the recommended log/metric categories and whether logs reach an approved workspace.", learnMoreHref: docsUrl("/user-guide/coverage/telemetry-coverage") },
  "/backupdr": { title: "Backup & DR Coverage", blurb: "Audit backup and disaster-recovery posture — protection, retention, recent jobs, offsite copies, and DR drills — against a reference baseline.", learnMoreHref: docsUrl("/user-guide/coverage/backup-dr-coverage") },
  "/performance": { title: "Performance Profiler", blurb: "Find bottlenecks on a resource × metric heatmap — which resources run hottest against their baseline thresholds.", learnMoreHref: docsUrl("/user-guide/assessment-performance/performance-profiler") },
  "/radar": { title: "Retirement Radar", blurb: "Track Azure service retirements and breaking changes, mapped to the workloads, owners, and deadlines they affect.", learnMoreHref: docsUrl("/user-guide/lifecycle-investigation/retirement-radar") },
  "/evidence": { title: "Evidence Locker", blurb: "Capture tamper-evident, hash-stamped point-in-time snapshots for forensics and audit.", learnMoreHref: docsUrl("/user-guide/lifecycle-investigation/evidence-locker") },
  "/assessments": { title: "Assessments", blurb: "Score a workload against the Azure Well-Architected Framework, with findings mapped to CIS / NIST / ISO and a branded PDF export.", learnMoreHref: docsUrl("/user-guide/assessment-performance/assessments") },
};

// ---- Keyboard shortcuts ---------------------------------------------------------
export const SHORTCUTS: { keys: string; action: string }[] = [
  { keys: "Ctrl / ⌘ + K", action: "Open the Command Palette" },
  { keys: "?", action: "Open the Help menu" },
  { keys: "Esc", action: "Close any dialog or overlay" },
];

// ---- Trust & Security points ----------------------------------------------------
export type TrustPoint = { icon: string; title: string; body: string };

export const TRUST_POINTS: TrustPoint[] = [
  { icon: "👁️", title: "Read-only by default", body: "The agent reads your estate out of the box. Anything that would change Azure is write-classified and stays off until you opt in." },
  { icon: "✅", title: "Approval-gated writes", body: "Every write-classified tool call requires explicit, per-action approval before it runs." },
  { icon: "🧾", title: "Full audit log", body: "Every privileged action is recorded with the actor, target, and timestamp." },
  { icon: "🏠", title: "Runs in your tenant", body: "Deployed to your own Azure Container App — your data never leaves your subscription." },
  { icon: "🧠", title: "AI disabled until configured", body: "No data goes to any LLM provider until an admin explicitly configures and enables one." },
  { icon: "👥", title: "RBAC + SSO", body: "Users, roles, and groups with least-privilege defaults, plus OIDC and SAML single sign-on." },
  { icon: "🗝️", title: "Encrypted credentials", body: "Azure connection secrets are encrypted at rest on the mounted Azure Files volume." },
  { icon: "🧩", title: "Multi-tenant isolation", body: "Connect multiple Azure tenants; each connection is kept isolated." },
];
