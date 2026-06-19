import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type AssessmentRunSummary, type RadarRailItem, type ReservationItem } from "../api";
import { usePersistedState } from "../utils/persistedState";
import { TrendChart } from "./TrendChart";
import { PdfGeneratingOverlay } from "./PdfGeneratingOverlay";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  openai_eu: "OpenAI (EU)",
  azure_openai: "Azure OpenAI",
  github: "GitHub Models",
  github_copilot: "GitHub Copilot",
  chatgpt: "ChatGPT (Codex)",
  claude: "Claude (Anthropic)",
  gemini: "Google Gemini",
  grok: "Grok (xAI)",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  ollama: "Ollama (local)",
  lmstudio: "LM Studio (local)",
};
// Providers that authenticate without an API key (OAuth or keyless local servers).
const KEYLESS_OK = new Set(["chatgpt", "github_copilot", "ollama", "lmstudio"]);

const AUTH_METHOD_LABELS: Record<string, string> = {
  service_principal: "Service principal",
  service_principal_secret: "Service principal (secret)",
  service_principal_cert: "Service principal (certificate)",
  client_secret: "Service principal (secret)",
  certificate: "Service principal (certificate)",
  azure_cli: "Azure CLI sign-in",
  cli: "Azure CLI sign-in",
  az_cli: "Azure CLI sign-in",
  az_cli_token: "Azure CLI sign-in",
  managed_identity: "Managed identity",
  access_token: "Access token",
  device_code: "Device-code sign-in",
};

const CONNECTOR_ICONS: Record<string, string> = {
  teams: "\uD83D\uDCAC", slack: "\uD83D\uDCAC", outlook: "\uD83D\uDCE7", email: "\uD83D\uDCE7", smtp: "\uD83D\uDCE7",
  jira: "\uD83E\uDEB2", grafana: "\uD83D\uDCC8", webhook: "\uD83D\uDD17", servicenow: "\uD83E\uDDFE", pagerduty: "\uD83D\uDCDF",
};

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// Turn an unknown snake_case / kebab-case id into a readable Title Case label, so
// providers/auth-methods added on the backend still render nicely before their
// friendly name is added to the maps above (instead of showing the raw id).
function humanize(id: string): string {
  return id.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).trim();
}
function providerLabel(id: string): string {
  return PROVIDER_LABELS[id] ?? humanize(id);
}
function authLabel(method: string): string {
  return AUTH_METHOD_LABELS[method] ?? humanize(method);
}

// Map a free-form status string to a tri-state health indicator.
function statusOk(status?: string): boolean | undefined {
  if (!status) return undefined;
  const s = status.toLowerCase();
  if (["connected", "ok", "ready", "valid", "success", "active", "healthy"].includes(s)) return true;
  if (["error", "failed", "invalid", "expired", "unauthorized", "disconnected"].includes(s)) return false;
  return undefined;
}

export function DashboardPanel() {
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const isAdmin = meQ.data?.role === "admin";

  // Setup status sources. Admin-only endpoints are gated by `enabled: isAdmin` so
  // non-admins never trigger a 403 — they fall back to the all-users `activeLlm` /
  // `azureConnections` summaries instead.
  const llmQ = useQuery({ queryKey: ["llmConfig"], queryFn: api.llmConfig, enabled: isAdmin, retry: false });
  const activeLlmQ = useQuery({ queryKey: ["activeLlm"], queryFn: api.activeLlm, retry: false });
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections, retry: false });
  const adminConnQ = useQuery({ queryKey: ["adminConnections"], queryFn: api.adminConnections, enabled: isAdmin, retry: false });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors, enabled: isAdmin, retry: false });
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, retry: false });
  const archQ = useQuery({ queryKey: ["architectures"], queryFn: api.architectures, retry: false });
  const runsQ = useQuery({ queryKey: ["assessmentRuns"], queryFn: () => api.assessmentRuns(), retry: false });
  const policyQ = useQuery({ queryKey: ["policySnapshots"], queryFn: api.policySnapshots, enabled: isAdmin, retry: false });
  const agentsQ = useQuery({ queryKey: ["customAgents"], queryFn: api.customAgents, enabled: isAdmin, retry: false });
  // New signals driving the KPI strip, posture & risks panel, and the activity feed.
  // Each query is non-blocking — a failure hides the affected tile/section instead of
  // breaking the dashboard. Defer expensive admin-only queries with a 5-minute
  // staleTime so they don't refetch on every dashboard mount/navigation.
  const unreadQ = useQuery({
    queryKey: ["notificationsUnread"],
    queryFn: api.notificationsUnread,
    retry: false,
    staleTime: 60_000,
  });
  const reservationsQ = useQuery({
    queryKey: ["reservationsOverview"],
    queryFn: () => api.reservationsOverview(false),
    enabled: isAdmin,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const radarQ = useQuery({
    queryKey: ["radarOverviewDashboard"],
    queryFn: () => api.radarOverview({}),
    enabled: isAdmin,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const recentInvQ = useQuery({
    queryKey: ["recentDeepInvestigations"],
    queryFn: () => api.deepInvestigations(5),
    retry: false,
    staleTime: 2 * 60_000,
  });

  // --- Primary workload (drives the per-scope coverage lenses below) -------------
  // Coverage / telemetry / backup / performance are per-scope, so the dashboard reads
  // them for ONE persisted "primary workload" (defaults to the first discovered one).
  const [primaryWorkloadId, setPrimaryWorkloadId] = usePersistedState<string>("azsup.dashboard.primaryWorkload", "");

  // --- Posture signals that aren't in the base dashboard yet ---------------------
  // All non-blocking + cache-friendly: a failure hides the affected tile, never breaks
  // the page. Coverage trends are cheap (they read the trend store, not a fresh scan).
  const covParams = primaryWorkloadId ? { workload_id: primaryWorkloadId } : undefined;
  const ambaTrendQ = useQuery({
    queryKey: ["dashAmbaTrend", primaryWorkloadId],
    queryFn: () => api.coverageTrend("amba", covParams!),
    enabled: isAdmin && !!primaryWorkloadId,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const telemetryTrendQ = useQuery({
    queryKey: ["dashTelemetryTrend", primaryWorkloadId],
    queryFn: () => api.coverageTrend("telemetry", covParams!),
    enabled: isAdmin && !!primaryWorkloadId,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const backupTrendQ = useQuery({
    queryKey: ["dashBackupTrend", primaryWorkloadId],
    queryFn: () => api.coverageTrend("backupdr", covParams!),
    enabled: isAdmin && !!primaryWorkloadId,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const perfTrendQ = useQuery({
    queryKey: ["dashPerfTrend", primaryWorkloadId],
    queryFn: () => api.coverageTrend("performance", covParams!),
    enabled: isAdmin && !!primaryWorkloadId,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const perfRunsQ = useQuery({
    queryKey: ["dashPerfRuns", primaryWorkloadId],
    queryFn: () => api.perfRuns(covParams!),
    enabled: isAdmin && !!primaryWorkloadId,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const identityQ = useQuery({
    queryKey: ["dashIdentity"],
    queryFn: () => api.identityOverview(30),
    enabled: isAdmin,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const rbacQ = useQuery({
    queryKey: ["dashRbac"],
    queryFn: () => api.rbacOverview(),
    enabled: isAdmin,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const optimizationQ = useQuery({
    queryKey: ["dashOptimization"],
    queryFn: () => api.inventoryOptimization(),
    enabled: isAdmin,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
  });
  const tasksQ = useQuery({
    queryKey: ["dashScheduledTasks"],
    queryFn: api.scheduledTasks,
    enabled: isAdmin,
    retry: false,
    staleTime: 60_000,
  });

  const activeProvider = llmQ.data?.active_provider ?? "";
  const activeCfg = activeProvider ? llmQ.data?.providers?.[activeProvider] : undefined;
  // Effective active provider/model — works for non-admins via the resolved /llm/active.
  const effectiveProvider = activeProvider || activeLlmQ.data?.provider || "";
  const effectiveModel = activeCfg?.model || activeLlmQ.data?.model || "";

  const aiReady: boolean | undefined = isAdmin
    ? (llmQ.isSuccess ? (activeCfg ? activeCfg.has_key || KEYLESS_OK.has(activeProvider) : false) : undefined)
    : (activeLlmQ.isSuccess ? !!activeLlmQ.data?.provider : undefined);

  // All configured providers (have a key, are keyless, or are the active one).
  const configuredProviders = useMemo(() => {
    const entries = Object.entries(llmQ.data?.providers ?? {});
    return entries
      .filter(([id, c]) => c.has_key || KEYLESS_OK.has(id) || id === activeProvider)
      .sort((a, b) => {
        if (a[0] === activeProvider) return -1;
        if (b[0] === activeProvider) return 1;
        return providerLabel(a[0]).localeCompare(providerLabel(b[0]));
      });
  }, [llmQ.data, activeProvider]);

  const tenantOptions = connQ.data?.connections ?? [];
  // Prefer the richer admin shape (auth method, default sub, status detail) when available.
  const conns = useMemo(() => {
    const rich = adminConnQ.data?.connections;
    if (rich && rich.length) {
      return rich.map((c) => ({
        id: c.id, name: c.display_name, tenant_id: c.tenant_id, status: c.status,
        is_default: c.is_default, read_only: c.read_only, disabled: c.disabled,
        auth_method: c.auth_method as string | undefined, default_subscription: c.default_subscription as string | undefined,
        status_detail: c.status_detail as string | undefined,
      }));
    }
    return tenantOptions.map((c) => ({
      id: c.id, name: c.display_name, tenant_id: c.tenant_id, status: c.status,
      is_default: c.is_default, read_only: c.read_only, disabled: false,
      auth_method: undefined as string | undefined, default_subscription: undefined as string | undefined,
      status_detail: undefined as string | undefined,
    }));
  }, [adminConnQ.data, tenantOptions]);

  const connectors = connectorsQ.data?.connectors ?? [];
  const connectorTypes = connectorsQ.data?.types ?? [];
  const connectorTypeLabel = (t: string) => connectorTypes.find((m) => m.id === t)?.label ?? t;

  const azureReady = conns.length > 0;
  const workloads = wlQ.data?.workloads ?? [];
  const architectures = archQ.data?.architectures ?? [];
  const runs = runsQ.data?.runs ?? [];
  const policySnapshots = policyQ.data?.snapshots ?? [];
  const agents = agentsQ.data?.agents ?? [];

  type Step = { id: string; title: string; desc: string; done: boolean | undefined; detail?: string; bullets?: string[]; cta: string; to: string; adminOnly?: boolean };
  const steps: Step[] = useMemo(() => [
    {
      id: "ai", title: "Connect an AI provider",
      desc: "The agent needs a large language model (OpenAI, Claude, Azure OpenAI, GitHub Copilot, a local model, …) to think.",
      done: aiReady,
      detail: aiReady && effectiveProvider
        ? (isAdmin
            ? `${plural(configuredProviders.length, "provider")} configured · Default: ${providerLabel(effectiveProvider)}${effectiveModel ? ` · ${effectiveModel}` : ""}`
            : `Default: ${providerLabel(effectiveProvider)}${effectiveModel ? ` · ${effectiveModel}` : ""}`)
        : undefined,
      bullets: [
        "🌐 A dozen+ providers — OpenAI, Azure OpenAI, Claude, Gemini, Grok, Mistral, GitHub Copilot & more",
        "🔄 Switch the default provider and model anytime, with per-chat overrides",
        "🔑 Authenticate by API key, OAuth sign-in (ChatGPT, GitHub Copilot), or keyless local servers",
        "🖥️ Keep data private with local models via Ollama or LM Studio",
        "💸 Free-only model filtering (e.g. OpenRouter) to keep costs in check",
        "📊 Built-in usage and token accounting per model",
      ],
      cta: isAdmin ? "Configure AI" : "Ask an admin", to: "/admin/providers", adminOnly: true,
    },
    {
      id: "azure", title: "Connect your Azure tenant",
      desc: "Add a connection (service principal or Azure CLI sign-in) so the agent can read your Azure resources.",
      done: azureReady,
      detail: azureReady ? `${plural(conns.length, "connection")}: ${conns.slice(0, 3).map((c) => c.name).join(", ")}${conns.length > 3 ? "…" : ""}` : undefined,
      bullets: [
        "🏢 Connect multiple tenants and subscriptions, each kept isolated",
        "🔐 Authenticate with a service principal (secret or certificate) or Azure CLI sign-in",
        "🛡️ Read-only by default — opt in to writes and auto-execute when you're ready",
        "🔎 Auto-discover the subscriptions and management groups a connection can see",
        "✅ Validate Entra ID permissions before you depend on a connection",
        "⭐ Set a default connection used across the whole app",
      ],
      cta: isAdmin ? "Add connection" : "Ask an admin", to: "/admin/tenants", adminOnly: true,
    },
    {
      id: "deep", title: "Run a Deep Investigation",
      desc: "For thorny problems, switch a chat to Deep mode: the agent forms multiple hypotheses and validates each one with evidence from your live Azure data — a structured root-cause analysis, not a single-shot answer.",
      done: aiReady,
      detail: aiReady ? "Available in any chat — pick “Deep investigation” from the thinking-level menu" : undefined,
      bullets: [
        "🧠 Multi-hypothesis reasoning — the agent branches into several root-cause theories and tests each independently",
        "🔬 Every hypothesis is validated with real evidence pulled live from your Azure estate, not guesses",
        "🌳 Watch the investigation tree build in real time, with color-coded confirmed / refuted / inconclusive status pills",
        "🧩 Scope it to a Workload so the analysis stays focused on the resources that matter",
        "📑 Ends with a structured conclusion: root cause, supporting evidence, and prioritized remediation steps",
        "🛰️ Ideal for intermittent outages, connectivity mysteries, permission failures, and cost or performance anomalies",
        "💾 The full hypothesis tree is saved with the chat — reopen the side panel anytime to review the reasoning",
        "🔁 Re-run after a change to confirm the fix actually resolved the issue, with a fresh evidence trail",
      ],
      cta: "Start a Deep Investigation", to: "/chat",
    },
    {
      id: "workload", title: "Let AI discover your Azure Workloads",
      desc: "Group everything that makes up a solution — subscriptions, resource groups, individual resources, and so on — into a single application. Use ✨ Autopilot to let AI discover them for you.",
      done: workloads.length > 0,
      detail: workloads.length ? `${plural(workloads.length, "Azure Workload")} defined` : undefined,
      bullets: [
        "✨ Autopilot uses AI to discover and propose workloads for you",
        "🧱 Mix any scope — management groups, subscriptions, resource groups, or individual resources",
        "🏷️ Tag and describe each workload and see a live resource-type breakdown",
        "🔄 Refresh from live Azure to stay current as your estate changes",
        "🧠 AI explains its reasoning and confidence for every discovered workload",
        "🎯 Becomes the foundation for assessments and architecture diagrams",
      ],
      cta: "Go to Workloads", to: "/workloads",
    },
    {
      id: "assess", title: "Assess against the Well-Architected Framework",
      desc: "Score a workload against the Azure Well-Architected pillars (security, reliability, cost, ops, performance).",
      done: runs.length > 0,
      detail: runs.length ? `${plural(runs.length, "assessment")} run` : undefined,
      bullets: [
        "🛡️ Scores all five Well-Architected pillars — security, reliability, cost, operations, performance",
        "📜 Findings mapped to CIS, NIST and ISO control frameworks",
        "🤖 AI-graded against your real Azure data, with an overall score out of 100",
        "🔎 Prioritized findings with severity and concrete remediation steps",
        "📊 Rich report with charts and toggles to drill into each pillar",
        "⏰ Run on a schedule or across many workloads at once",
      ],
      cta: "Go to Assessments", to: "/assessments",
    },
    {
      id: "policy", title: "Govern your estate with Azure Policy",
      desc: "Explore live policy assignments, scan compliance, and simulate a guardrail before you enforce it — every action is read-only.",
      done: isAdmin ? (policyQ.isSuccess ? policySnapshots.length > 0 : undefined) : undefined,
      detail: policySnapshots.length
        ? `${plural(policySnapshots.length, "posture snapshot")} captured${
            policySnapshots[0]?.summary?.compliance?.available
              ? ` · last scan: ${policySnapshots[0].summary.compliance.total_non_compliant_resources} non-compliant resources`
              : ""
          }`
        : undefined,
      bullets: [
        "\uD83D\uDD0E Live, read\u2011only inventory of every policy assignment, definition and exemption \u2014 AI summarizes your governance posture at a glance",
        "\uD83E\uDDED Effective\u2011policy resolver shows exactly what governs any scope (inherited included), with AI explaining each policy in plain English",
        "\uD83D\uDCCA Scan compliance for a real\u2011time posture dashboard, then let AI narrate the drift between snapshots over time",
        "\uD83E\uDE7A AI advisors rank promote\u2011to\u2011deny wins, remediation gaps, stale exemptions and policy conflicts by impact",
        "\uD83D\uDCD0 AI coverage\u2011gap analysis against WAF, MCSB & CIS baselines \u2014 proposes the exact built\u2011ins to close every gap",
        "\uD83D\uDEE1\uFE0F Bring failing Well\u2011Architected assessment findings straight into the AI Safe\u2011Rollout Planner \u2014 AI matches each to the right built\u2011in policy, then safely rolls it out so you see the impact before enforcing",
        "\uD83D\uDD2C Analyze impact before you apply \u2014 AI simulates the change against live resources, counts exactly what's affected, and gives a go/no\u2011go verdict",
        "\uD83D\uDEA6 Roll out safely in stages (audit \u2192 deny) with copy\u2011ready policy JSON + `az` commands you run yourself \u2014 the planner never touches Azure",
      ],
      cta: isAdmin ? "Go to Azure Policy" : "Ask an admin", to: "/policy", adminOnly: true,
    },
    {
      id: "arch", title: "Living architecture, built by AI agents",
      desc: "Turn an Azure Workload into a living architecture diagram — built by AI from your real resources, then yours to refine, review, and keep in sync.",
      done: architectures.length > 0,
      detail: architectures.length ? `${plural(architectures.length, "architecture")} mapped` : undefined,
      bullets: [
        "🤖 AI reverse-engineers a diagram from a live Workload — automatically grouped into tiers and connected",
        "🎨 Drag-and-drop editor with a full Azure service palette to design or fine-tune by hand",
        "🩺 Overlay a Well-Architected assessment onto the diagram — failing controls light up the exact resources, with findings explained right where they live",
        "✨ One-click AI Enhance applies Well-Architected best practices and fills in missing pieces",
        "📐 Smart auto-layout (Tidy) with multiple arrangement options to untangle any diagram",
        "🔍 Drift detection compares the diagram against what's actually deployed in Azure",
        "🚦 Lifecycle states (Draft → In Review → Ready) and grouping into solution categories",
      ],
      cta: "Go to Architectures", to: "/architectures",
    },
  ], [aiReady, azureReady, isAdmin, conns, workloads.length, runs.length, architectures.length, effectiveProvider, effectiveModel, configuredProviders.length, policyQ.isSuccess, policySnapshots]);

  const known = steps.filter((s) => s.done !== undefined);
  const doneCount = known.filter((s) => s.done).length;
  const pct = known.length ? Math.round((doneCount / known.length) * 100) : 0;
  const allDone = known.length > 0 && doneCount === known.length;

  // Per-step bullet expansion. Completed steps keep their bullet list collapsed by
  // default so the checklist is scannable; pending steps expand to surface the value.
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  // When the user has finished onboarding the entire setup guide collapses to a banner
  // the user can still expand.
  const [setupGuideOpen, setSetupGuideOpen] = useState<boolean>(false);

  // Renders the rich step cards (status, title, badges, expandable bullets, detail, CTA).
  // `forceExpanded` opens every card's bullet list by default — used when the user
  // explicitly opens the guide from the completed-state banner. The per-card
  // "Learn more / Hide details" toggle still wins once clicked.
  const renderSetupCards = (forceExpanded: boolean) => (
    <div className="space-y-2">
      {steps.map((s, i) => {
        const blocked = s.adminOnly && !isAdmin;
        const expanded = expandedSteps[s.id] ?? (forceExpanded || s.done !== true);
        return (
          <div key={s.id} className={`flex items-start gap-3 rounded-xl border bg-white p-4 shadow-sm ${s.done ? "border-green-200" : ""}`}>
            <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
              s.done === true ? "bg-green-500 text-white" : s.done === false ? "bg-gray-200 text-gray-500" : "bg-amber-100 text-amber-600"
            }`}>
              {s.done === true ? "✓" : s.done === undefined ? "?" : i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-gray-800">{s.title}</span>
                {s.done === true && <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700">done</span>}
                {s.done === undefined && <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">admin-managed</span>}
                {s.bullets && s.bullets.length > 0 && (
                  <button
                    onClick={() => setExpandedSteps((m) => ({ ...m, [s.id]: !expanded }))}
                    className="ml-auto rounded-md border px-1.5 py-0.5 text-[10px] text-gray-500 hover:bg-gray-50"
                  >
                    {expanded ? "Hide details" : "Learn more"}
                  </button>
                )}
              </div>
              <p className="mt-0.5 text-xs text-gray-500">{s.desc}</p>
              {expanded && s.bullets && (
                <ul className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
                  {s.bullets.map((b, bi) => (
                    <li key={bi} className="text-[11px] leading-snug text-gray-600">{b}</li>
                  ))}
                </ul>
              )}
              {s.detail && (
                <p className="mt-1 inline-flex items-center gap-1 rounded-md bg-gray-50 px-2 py-0.5 text-[11px] text-gray-600">
                  <span className="text-green-500">●</span>{s.detail}
                </p>
              )}
            </div>
            {blocked ? (
              <span className="shrink-0 self-center rounded-lg border px-3 py-1.5 text-xs text-gray-400">Ask an admin</span>
            ) : (
              <Link to={s.to} className={`shrink-0 self-center rounded-lg px-3 py-1.5 text-xs font-medium ${s.done ? "border text-gray-600 hover:bg-gray-50" : "bg-brand text-white hover:bg-brand/90"}`}>
                {s.done ? "Review" : s.cta} →
              </Link>
            )}
          </div>
        );
      })}
    </div>
  );

  // -------- Derived KPIs / posture signals --------------------------------------
  const reservations = reservationsQ.data?.items ?? [];
  const radarRail = radarQ.data?.rail ?? [];
  const unreadCount = unreadQ.data?.count ?? 0;
  const recentInvestigations = recentInvQ.data?.investigations ?? [];

  const completedRuns = useMemo(
    () => runs.filter((r) => typeof r.overall_score === "number"),
    [runs],
  );
  const avgScore = useMemo(() => {
    if (!completedRuns.length) return null;
    const total = completedRuns.reduce((s, r) => s + (r.overall_score ?? 0), 0);
    return Math.round(total / completedRuns.length);
  }, [completedRuns]);
  const lowestRun: AssessmentRunSummary | null = useMemo(() => {
    if (!completedRuns.length) return null;
    return [...completedRuns].sort((a, b) => (a.overall_score ?? 0) - (b.overall_score ?? 0))[0];
  }, [completedRuns]);

  const retirementsSoon: RadarRailItem[] = useMemo(
    () => radarRail.filter((r) => (r.days_until ?? 9999) <= 90 && r.severity !== "grey")
      .sort((a, b) => (a.days_until ?? 9999) - (b.days_until ?? 9999))
      .slice(0, 5),
    [radarRail],
  );
  const reservationsExpiringSoon: ReservationItem[] = useMemo(
    () => reservations
      .filter((r) => r.bucket === "expiring_soon")
      .sort((a, b) => (a.days_until ?? 9999) - (b.days_until ?? 9999))
      .slice(0, 5),
    [reservations],
  );

  type ActivityEntry = { kind: string; icon: string; title: string; detail: string; at: string; to: string };
  const recentActivity: ActivityEntry[] = useMemo(() => {
    const entries: ActivityEntry[] = [];
    for (const r of runs.slice(0, 5)) {
      const when = r.ended_at || r.started_at;
      if (!when) continue;
      entries.push({
        kind: "assessment", icon: "🛡️",
        title: `Assessment · ${r.workload_name}`,
        detail: r.overall_score != null ? `${r.overall_score}/100 · ${r.severity}` : (r.status || "running"),
        at: when,
        to: `/assessments/${r.id}`,
      });
    }
    for (const inv of recentInvestigations.slice(0, 5)) {
      entries.push({
        kind: "investigation", icon: "🔎",
        title: `Deep investigation · ${inv.title}`,
        detail: inv.root_cause || inv.summary || "completed",
        at: inv.created_at,
        to: `/chat/${inv.chat_id}`,
      });
    }
    if (isAdmin) {
      for (const snap of policySnapshots.slice(0, 3)) {
        const when = (snap as unknown as { generated_at?: string; created_at?: string }).generated_at
          || (snap as unknown as { created_at?: string }).created_at
          || "";
        if (!when) continue;
        entries.push({
          kind: "policy", icon: "📐",
          title: "Policy posture snapshot",
          detail: snap.summary?.compliance?.available
            ? `${snap.summary.compliance.total_non_compliant_resources ?? 0} non-compliant`
            : "snapshot captured",
          at: when,
          to: "/policy",
        });
      }
    }
    return entries
      .filter((e) => !!e.at)
      .sort((a, b) => (new Date(b.at).getTime() - new Date(a.at).getTime()))
      .slice(0, 6);
  }, [runs, recentInvestigations, policySnapshots, isAdmin]);

  const userName = (meQ.data?.display_name && meQ.data.display_name.trim())
    || (meQ.data?.email ? meQ.data.email.split("@")[0] : "")
    || "there";
  const defaultConn = conns.find((c) => c.is_default) || conns[0];

  // -------- Primary workload bootstrap + coverage signals ------------------------
  useEffect(() => {
    if (!primaryWorkloadId && workloads.length > 0) {
      setPrimaryWorkloadId(workloads[0].id);
    }
  }, [primaryWorkloadId, workloads, setPrimaryWorkloadId]);

  const primaryWorkloadName = workloads.find((w) => w.id === primaryWorkloadId)?.name ?? "";

  type Lens = { key: string; label: string; icon: string; to: string; current: number | null; delta: number | null; points: { at: string; pct: number | null }[]; loading: boolean; hasData: boolean };
  const lenses: Lens[] = useMemo(() => {
    const mk = (key: string, label: string, icon: string, to: string, q: typeof ambaTrendQ): Lens => ({
      key, label, icon, to,
      current: q.data?.current ?? null,
      delta: q.data?.delta ?? null,
      points: q.data?.points ?? [],
      loading: q.isLoading,
      hasData: (q.data?.points?.length ?? 0) > 0 || q.data?.current != null,
    });
    return [
      mk("amba", "Monitoring", "📈", "/coverage", ambaTrendQ),
      mk("telemetry", "Telemetry", "🛰️", "/telemetry", telemetryTrendQ),
      mk("backupdr", "Backup & DR", "💾", "/backupdr", backupTrendQ),
      mk("performance", "Performance", "⚡", "/performance", perfTrendQ),
    ];
  }, [ambaTrendQ, telemetryTrendQ, backupTrendQ, perfTrendQ]);

  const topBottleneck = useMemo(() => {
    const latest = (perfRunsQ.data?.runs ?? []).find((r) => r.top_bottleneck);
    return latest?.top_bottleneck ?? null;
  }, [perfRunsQ.data]);

  // -------- Estate Health Score (blended 0-100) ---------------------------------
  // Weighted blend of what we can read cheaply: assessment avg, the three coverage
  // lenses, minus penalties for due retirements and expiring identity credentials.
  const identityKpis = identityQ.data?.kpis;
  const estateHealth = useMemo(() => {
    const parts: { v: number; w: number }[] = [];
    if (avgScore != null) parts.push({ v: avgScore, w: 2 });
    for (const l of lenses) {
      if (l.key !== "performance" && l.current != null) parts.push({ v: l.current, w: 1 });
    }
    if (!parts.length) return null;
    let base = parts.reduce((s, p) => s + p.v * p.w, 0) / parts.reduce((s, p) => s + p.w, 0);
    // Risk penalties (bounded) so live threats visibly drag the score.
    base -= Math.min(15, retirementsSoon.length * 3);
    const idGaps = (identityKpis?.expiring_secrets ?? 0) + (identityKpis?.expiring_certs ?? 0) + (identityKpis?.keyvault_expiring ?? 0);
    base -= Math.min(10, idGaps * 2);
    return Math.max(0, Math.min(100, Math.round(base)));
  }, [avgScore, lenses, retirementsSoon.length, identityKpis]);

  // -------- "This week" summary --------------------------------------------------
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const assessmentsThisWeek = useMemo(
    () => runs.filter((r) => { const t = new Date(r.ended_at || r.started_at || "").getTime(); return Number.isFinite(t) && t >= weekAgo; }).length,
    [runs, weekAgo],
  );
  const investigationsThisWeek = useMemo(
    () => recentInvestigations.filter((i) => { const t = new Date(i.created_at).getTime(); return Number.isFinite(t) && t >= weekAgo; }).length,
    [recentInvestigations, weekAgo],
  );

  // -------- Attention triage (what needs me today) ------------------------------
  type Attention = { id: string; icon: string; sev: "red" | "amber"; text: string; to: string; action: string };
  const attention: Attention[] = useMemo(() => {
    const out: Attention[] = [];
    for (const r of runs) {
      if ((r.status || "").toLowerCase() === "failed") {
        out.push({ id: `run-${r.id}`, icon: "🛡️", sev: "amber", text: `Assessment failed · ${r.workload_name}`, to: `/assessments/${r.id}`, action: "Open" });
      }
    }
    const idSecrets = (identityKpis?.expiring_secrets ?? 0) + (identityKpis?.expiring_certs ?? 0);
    if (idSecrets > 0) {
      out.push({ id: "id-secrets", icon: "🔑", sev: idSecrets >= 3 ? "red" : "amber", text: `${plural(idSecrets, "credential")} expiring within 30 days`, to: "/identity", action: "Review" });
    }
    if ((identityKpis?.users_without_mfa ?? 0) > 0) {
      out.push({ id: "id-mfa", icon: "🔐", sev: "red", text: `${plural(identityKpis!.users_without_mfa, "privileged user")} without MFA`, to: "/identity", action: "Review" });
    }
    for (const r of retirementsSoon.slice(0, 3)) {
      if ((r.days_until ?? 999) <= 60) {
        out.push({ id: `ret-${r.id}`, icon: "📡", sev: r.severity === "red" ? "red" : "amber", text: `Retiring in ${r.days_until}d · ${r.title}`, to: "/radar", action: "Plan" });
      }
    }
    for (const r of reservationsExpiringSoon.slice(0, 2)) {
      out.push({ id: `ri-${r.id}`, icon: "🏷️", sev: r.severity === "red" ? "red" : "amber", text: `Reservation expiring in ${r.days_until ?? "—"}d · ${r.display_name}`, to: "/reservations", action: "Review" });
    }
    return out.sort((a, b) => (a.sev === "red" ? -1 : 1) - (b.sev === "red" ? -1 : 1)).slice(0, 6);
  }, [runs, identityKpis, retirementsSoon, reservationsExpiringSoon]);

  // -------- Upcoming scheduled automations --------------------------------------
  const upcomingTasks = useMemo(() => {
    const tasks = tasksQ.data?.tasks ?? [];
    return tasks
      .filter((t) => t.status === "active" && t.next_run_at)
      .sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())
      .slice(0, 4);
  }, [tasksQ.data]);

  // -------- FinOps signal --------------------------------------------------------
  const optimization = optimizationQ.data;

  // -------- Dashboard refresh + layout customization ----------------------------
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  async function refreshDashboard() {
    setRefreshing(true);
    try {
      await qc.invalidateQueries();
    } finally {
      setRefreshing(false);
    }
  }

  // Combined "Estate Coverage" PDF (Monitoring + Telemetry + Backup & DR) for the primary workload.
  const [estatePdfBusy, setEstatePdfBusy] = useState(false);
  const estatePdfAbortRef = useRef<AbortController | null>(null);
  async function downloadEstatePdf() {
    if (estatePdfBusy || !primaryWorkloadId) return;
    const controller = new AbortController();
    estatePdfAbortRef.current = controller;
    setEstatePdfBusy(true);
    try {
      const blob = await api.estateCoveragePdf({ workload_id: primaryWorkloadId }, controller.signal);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `estate-coverage-${primaryWorkloadName || "report"}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* best-effort download (incl. user cancel); surfaced via the disabled state */
    } finally {
      estatePdfAbortRef.current = null;
      setEstatePdfBusy(false);
    }
  }
  function cancelEstatePdf() {
    estatePdfAbortRef.current?.abort();
  }

  // Section visibility (persisted) — the "Customize" control toggles these off/on.
  const [hiddenSections, setHiddenSections] = usePersistedState<string[]>("azsup.dashboard.hidden", []);
  const [customizing, setCustomizing] = useState(false);
  // "What's set up" config detail is collapsed by default — it's admin-config noise on a
  // daily dashboard. Persist the user's choice.
  const [configOpen, setConfigOpen] = usePersistedState<boolean>("azsup.dashboard.configOpen", false);
  const isHidden = (key: string) => hiddenSections.includes(key);
  const toggleSection = (key: string) =>
    setHiddenSections(isHidden(key) ? hiddenSections.filter((k) => k !== key) : [...hiddenSections, key]);

  const anyPostureLoading = ambaTrendQ.isLoading || identityQ.isLoading || rbacQ.isLoading;
  const coreLoading = meQ.isLoading || wlQ.isLoading || runsQ.isLoading || archQ.isLoading;

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-6xl space-y-6 p-6">
        {/* Hero — personalized greeting + estate health */}
        <div className="rounded-2xl border bg-gradient-to-br from-brand/10 to-violet-50 p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 items-center gap-3">
              <span className="text-3xl">🤖</span>
              <div className="min-w-0">
                <h1 className="truncate text-2xl font-bold text-gray-800">Welcome back, {userName}</h1>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-gray-600">
                  <span className="rounded-full bg-white/70 px-2 py-0.5">{isAdmin ? "Administrator" : "User"}</span>
                  {effectiveProvider && (
                    <span className="rounded-full bg-white/70 px-2 py-0.5">
                      🧠 {providerLabel(effectiveProvider)}{effectiveModel ? ` · ${effectiveModel}` : ""}
                    </span>
                  )}
                  {defaultConn && (
                    <span className="rounded-full bg-white/70 px-2 py-0.5">🏢 {defaultConn.name}</span>
                  )}
                  {!allDone && known.length > 0 && (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700">
                      Setup {doneCount}/{known.length}
                    </span>
                  )}
                </div>
                {/* This-week summary line */}
                <div className="mt-1.5 text-[11px] text-gray-500">
                  <span className="font-medium text-gray-600">This week:</span>{" "}
                  {plural(assessmentsThisWeek, "assessment")} · {plural(investigationsThisWeek, "investigation")}
                  {retirementsSoon.length > 0 && <> · <span className="text-amber-700">{plural(retirementsSoon.length, "retirement")} ≤90d</span></>}
                  {identityKpis && (identityKpis.expiring_secrets + identityKpis.expiring_certs) > 0 && (
                    <> · <span className="text-red-600">{identityKpis.expiring_secrets + identityKpis.expiring_certs} credentials expiring</span></>
                  )}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-4">
              {/* Estate Health ring */}
              {estateHealth != null && (
                <Link to="/assessments" className="flex items-center gap-2 rounded-xl bg-white/70 px-3 py-1.5 hover:bg-white" title="Blended health across assessments, coverage and live risks">
                  <HealthRing value={estateHealth} />
                  <div className="leading-tight">
                    <div className="text-[10px] font-medium uppercase tracking-wide text-gray-500">Estate health</div>
                    <div className="text-[11px] text-gray-500">blended posture</div>
                  </div>
                </Link>
              )}
              {/* Toolbar */}
              <div className="flex flex-col items-end gap-2">
                <div className="flex items-center gap-1.5">
                  <button onClick={() => void refreshDashboard()} disabled={refreshing} title="Refresh all dashboard data" className="rounded-lg border bg-white/70 px-2 py-1.5 text-xs text-gray-600 hover:bg-white disabled:opacity-50">
                    {refreshing || anyPostureLoading ? "⟳ Refreshing…" : "⟳ Refresh"}
                  </button>
                  <button onClick={() => setCustomizing((v) => !v)} title="Show or hide dashboard sections" className={`rounded-lg border px-2 py-1.5 text-xs ${customizing ? "border-brand/40 bg-brand/5 text-brand" : "bg-white/70 text-gray-600 hover:bg-white"}`}>
                    ⚙ Customize
                  </button>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Link to="/chat" className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90">💬 New chat</Link>
                  <Link to="/chat?deep=1" className="rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-xs font-medium text-violet-700 hover:bg-violet-100">🔎 Deep investigation</Link>
                  <Link to="/assessments" className="rounded-lg border px-3 py-1.5 text-xs text-gray-700 hover:bg-white">🛡️ Run assessment</Link>
                  {isAdmin && (
                    <Link to="/monitor" className="rounded-lg border px-3 py-1.5 text-xs text-gray-700 hover:bg-white">📊 Monitor</Link>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Customize panel — toggle section visibility (persisted) */}
          {customizing && (
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border bg-white/80 p-3">
              <span className="text-[11px] font-medium text-gray-500">Sections:</span>
              {SECTION_TOGGLES.map((s) => (
                <button
                  key={s.key}
                  onClick={() => toggleSection(s.key)}
                  className={`rounded-full border px-2.5 py-0.5 text-[11px] ${isHidden(s.key) ? "border-gray-200 bg-gray-50 text-gray-400 line-through" : "border-brand/30 bg-brand/5 text-brand"}`}
                >
                  {s.label}
                </button>
              ))}
              {hiddenSections.length > 0 && (
                <button onClick={() => setHiddenSections([])} className="ml-1 text-[11px] text-gray-500 hover:underline">Reset</button>
              )}
            </div>
          )}
        </div>

        {/* KPI strip — at-a-glance signals */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          {coreLoading ? (
            Array.from({ length: 6 }).map((_, i) => <SkeletonTile key={i} />)
          ) : (
          <>
          <KpiTile to="/workloads" icon="🧩" label="Workloads" value={workloads.length} />
          <KpiTile
            to="/assessments"
            icon="🛡️"
            label="Assessments"
            value={runs.length}
            sub={avgScore != null ? `avg ${avgScore}/100` : undefined}
          />
          <KpiTile to="/architectures" icon="🗺️" label="Architectures" value={architectures.length} />
          <KpiTile
            to="/notifications"
            icon="🔔"
            label="Unread"
            value={unreadCount}
            tone={unreadCount > 0 ? "amber" : undefined}
          />
          {isAdmin && (
            <KpiTile
              to="/radar"
              icon="📡"
              label="Retirements ≤ 90d"
              value={retirementsSoon.length}
              tone={retirementsSoon.length > 0 ? "red" : undefined}
            />
          )}
          {isAdmin && (
            <KpiTile
              to="/reservations"
              icon="🏷️"
              label="RIs ≤ 30d"
              value={reservationsExpiringSoon.length}
              tone={reservationsExpiringSoon.length > 0 ? "red" : undefined}
            />
          )}
          </>
          )}
        </div>

        {/* Attention — what needs me today (failed runs, expiring creds, deadlines) */}
        {isAdmin && !isHidden("attention") && attention.length > 0 && (
          <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3">
            <div className="mb-2 flex items-center gap-2">
              <span className="text-sm font-semibold text-amber-800">⚠ Needs attention</span>
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700">{attention.length}</span>
            </div>
            <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {attention.map((a) => (
                <li key={a.id}>
                  <Link to={a.to} className="flex items-center gap-2 rounded-lg border bg-white px-2.5 py-1.5 hover:border-brand/40">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${a.sev === "red" ? "bg-red-500" : "bg-amber-500"}`} />
                    <span className="text-base">{a.icon}</span>
                    <span className="min-w-0 flex-1 truncate text-[12px] text-gray-700" title={a.text}>{a.text}</span>
                    <span className="shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium text-brand">{a.action} →</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Coverage lenses — per-workload posture (Monitoring / Telemetry / Backup / Performance) */}
        {isAdmin && !isHidden("coverage") && (
          <div>
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Coverage</h2>
              <div className="flex items-center gap-2">
                {primaryWorkloadId && (
                  <button
                    onClick={() => void downloadEstatePdf()}
                    disabled={estatePdfBusy}
                    title="Download a combined Estate Coverage PDF (Monitoring · Telemetry · Backup & DR) for this workload"
                    className="rounded-lg border bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                  >
                    {estatePdfBusy ? "Generating…" : "📄 Estate PDF"}
                  </button>
                )}
                {workloads.length > 0 && (
                  <select
                    value={primaryWorkloadId}
                    onChange={(e) => setPrimaryWorkloadId(e.target.value)}
                    className="max-w-[220px] rounded-lg border bg-white px-2 py-1 text-xs"
                    title="Primary workload for the coverage lenses"
                  >
                    {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                )}
              </div>
            </div>
            {!primaryWorkloadId ? (
              <Empty text="Discover a workload to see its coverage." />
            ) : (
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {lenses.map((l) => (
                  <CoverageLensTile key={l.key} lens={l} scopeName={primaryWorkloadName} />
                ))}
              </div>
            )}
            {topBottleneck && (
              <Link to="/performance" className="mt-2 flex items-center gap-2 rounded-lg border bg-white px-3 py-1.5 text-[12px] text-gray-600 hover:border-brand/40">
                <span>⚡</span>
                <span className="min-w-0 flex-1 truncate">Top bottleneck: <span className="font-medium text-gray-800">{topBottleneck.resource_name}</span> · {topBottleneck.metric_name}</span>
                <span className="shrink-0 font-semibold text-amber-600">{topBottleneck.pct_of_threshold}%</span>
              </Link>
            )}
          </div>
        )}

        {/* Posture & risks — the actual reason an admin opens the dashboard */}
        {!isHidden("posture") && (
        <div>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">Posture &amp; risks</h2>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Card title="Lowest assessment" icon="🛡️" manageTo="/assessments">
              {lowestRun ? (
                <Link to={`/assessments/${lowestRun.id}`} className="block rounded-lg border bg-gray-50 p-3 hover:border-brand/40 hover:bg-white">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium text-gray-800">{lowestRun.workload_name}</span>
                    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold ${scoreTone(lowestRun.overall_score)}`}>
                      {lowestRun.overall_score}/100
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[11px] text-gray-500">{lowestRun.summary || "Open the run to see prioritized findings."}</p>
                </Link>
              ) : (
                <Empty text={runs.length ? "No completed runs with a score yet." : "No assessment has been run yet."} />
              )}
            </Card>

            {isAdmin && (
              <Card title="Retirements due ≤ 90d" icon="📡" manageTo="/radar">
                {radarQ.isError ? (
                  <Empty text="Radar data not available." />
                ) : retirementsSoon.length === 0 ? (
                  <Empty text="Nothing retiring in the next 90 days." />
                ) : (
                  <ul className="space-y-1.5">
                    {retirementsSoon.map((r) => (
                      <li key={r.id} className="flex items-center justify-between gap-2 rounded-md border bg-gray-50 px-2.5 py-1.5">
                        <span className="min-w-0 truncate text-[12px] text-gray-700" title={r.title}>{r.title}</span>
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                          r.severity === "red" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"
                        }`}>{r.days_until ?? "—"}d</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            )}

            {isAdmin && (
              <Card title="Reservations expiring ≤ 30d" icon="🏷️" manageTo="/reservations">
                {reservationsQ.isError ? (
                  <Empty text="Reservation data not available." />
                ) : reservationsExpiringSoon.length === 0 ? (
                  <Empty text="No reservations expire in the next 30 days." />
                ) : (
                  <ul className="space-y-1.5">
                    {reservationsExpiringSoon.map((r) => (
                      <li key={r.id} className="flex items-center justify-between gap-2 rounded-md border bg-gray-50 px-2.5 py-1.5">
                        <span className="min-w-0 truncate text-[12px] text-gray-700" title={r.display_name}>{r.display_name}</span>
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                          r.severity === "red" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"
                        }`}>{r.days_until ?? "—"}d</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            )}

            {/* Identity risk */}
            {isAdmin && (
              <Card title="Identity risk" icon="🔑" manageTo="/identity">
                {identityQ.isError ? (
                  <Empty text="Identity data not available." />
                ) : identityQ.data?.never_loaded ? (
                  <Empty text="Not loaded yet — open Identity and press Refresh." />
                ) : !identityKpis ? (
                  <Empty text="No identity signals." />
                ) : (
                  <div className="grid grid-cols-2 gap-2">
                    <MiniStat to="/identity" label="Secrets ≤30d" value={identityKpis.expiring_secrets} tone={identityKpis.expiring_secrets > 0 ? "red" : "ok"} />
                    <MiniStat to="/identity" label="Certs ≤30d" value={identityKpis.expiring_certs} tone={identityKpis.expiring_certs > 0 ? "red" : "ok"} />
                    <MiniStat to="/identity" label="Ownerless apps" value={identityKpis.ownerless_apps} tone={identityKpis.ownerless_apps > 0 ? "amber" : "ok"} />
                    <MiniStat to="/identity" label="Admins w/o MFA" value={identityKpis.users_without_mfa} tone={identityKpis.users_without_mfa > 0 ? "red" : "ok"} />
                  </div>
                )}
              </Card>
            )}

            {/* Access (RBAC) */}
            {isAdmin && (
              <Card title="Access (RBAC)" icon="🛂" manageTo="/rbac">
                {rbacQ.isError ? (
                  <Empty text="RBAC data not available." />
                ) : rbacQ.data?.never_loaded ? (
                  <Empty text="Not loaded yet — open RBAC and refresh a scope." />
                ) : !rbacQ.data ? (
                  <Empty text="No access data." />
                ) : (
                  <div className="grid grid-cols-2 gap-2">
                    <MiniStat to="/rbac" label="Privileged" value={rbacQ.data.kpis.privileged} tone={rbacQ.data.kpis.privileged > 0 ? "amber" : "ok"} />
                    <MiniStat to="/rbac" label="Principals" value={rbacQ.data.kpis.unique_principals} tone="ok" />
                    <MiniStat to="/rbac" label="Group-derived" value={rbacQ.data.kpis.group_derived} tone="ok" />
                    <MiniStat to="/rbac" label="Eligible (PIM)" value={rbacQ.data.kpis.eligible} tone="ok" />
                  </div>
                )}
              </Card>
            )}

            {/* Cost optimization (FinOps) */}
            {isAdmin && (
              <Card title="Cost optimization" icon="💰" manageTo="/inventory">
                {optimizationQ.isError ? (
                  <Empty text="Optimization data not available." />
                ) : !optimization?.available ? (
                  <Empty text="Open Inventory to scan for savings." />
                ) : optimization.total_count === 0 ? (
                  <Empty text="No optimization opportunities found. 🎉" />
                ) : (
                  <Link to="/inventory" className="block rounded-lg border bg-gray-50 p-3 hover:border-brand/40 hover:bg-white">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-gray-800">{plural(optimization.total_count, "opportunity")}</span>
                      {optimization.total_monthly_cost != null && optimization.cost_available && (
                        <span className="shrink-0 rounded bg-green-100 px-1.5 py-0.5 text-[10px] font-bold text-green-700">
                          ~{formatMoney(optimization.total_monthly_cost, optimization.currency)}/mo
                        </span>
                      )}
                    </div>
                    <p className="mt-1 line-clamp-2 text-[11px] text-gray-500">
                      {optimization.categories.slice(0, 3).map((c) => c.label).join(" · ") || "Idle, orphaned and oversized resources."}
                    </p>
                  </Link>
                )}
              </Card>
            )}
          </div>
        </div>
        )}

        {/* Scheduled automations — what's coming up */}
        {isAdmin && !isHidden("scheduled") && upcomingTasks.length > 0 && (
          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">Scheduled next</h2>
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {upcomingTasks.map((t) => (
                <li key={t.id}>
                  <Link to="/automations" className="flex items-center gap-3 rounded-xl border bg-white px-3 py-2 shadow-sm hover:border-brand/40">
                    <span className="text-base">{t.target_meta?.icon || "⏰"}</span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-gray-800">{t.name}</div>
                      <div className="truncate text-[11px] text-gray-500">{t.schedule_label || t.schedule_kind}{t.target_meta?.label ? ` · ${t.target_meta.label}` : ""}</div>
                    </div>
                    <span className="shrink-0 text-[11px] text-gray-400">{t.next_run_at ? formatNextRun(t.next_run_at) : ""}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Recent activity feed */}
        {!isHidden("activity") && recentActivity.length > 0 && (
          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">Recent activity</h2>
            <ul className="divide-y rounded-xl border bg-white shadow-sm">
              {recentActivity.map((e, i) => (
                <li key={i}>
                  <Link to={e.to} className="flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50">
                    <span className="text-base">{e.icon}</span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-gray-800">{e.title}</div>
                      <div className="truncate text-[11px] text-gray-500">{e.detail}</div>
                    </div>
                    <span className="shrink-0 text-[11px] text-gray-400">{formatRelative(e.at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Setup guide — full when in progress, collapsed banner when complete */}
        {!isHidden("setup") && (allDone ? (
          <div className="rounded-xl border border-green-200 bg-green-50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm text-green-800">
                <span>✅</span>
                <span className="font-medium">All {known.length} setup steps complete.</span>
                {recentInvQ.isSuccess && (
                  <span className="text-green-700">{plural(recentInvestigations.length, "recent investigation")} · {plural(runs.length, "assessment run")}</span>
                )}
              </div>
              <button onClick={() => setSetupGuideOpen((v) => !v)} className="rounded-lg border border-green-300 bg-white px-2.5 py-1 text-xs text-green-700 hover:bg-green-100">
                {setupGuideOpen ? "Hide guide" : "Show guide"}
              </button>
            </div>
            {setupGuideOpen && (
              <div className="mt-3">
                {renderSetupCards(true)}
              </div>
            )}
          </div>
        ) : (
          <div>
            <div className="mb-2 flex items-end justify-between gap-2">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Setup guide</h2>
              <span className="text-xs text-gray-500">{doneCount}/{known.length} done</span>
            </div>
            <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-gray-200">
              <div className={`h-full rounded-full transition-all ${allDone ? "bg-green-500" : "bg-brand"}`} style={{ width: `${pct}%` }} />
            </div>
            {renderSetupCards(false)}
          </div>
        ))}

        {/* What's set up — detailed (collapsed by default; admin-config noise) */}
        {!isHidden("configured") && (
        <div>
          <button onClick={() => setConfigOpen(!configOpen)} className="mb-2 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-gray-500 hover:text-gray-700">
            <span className="text-gray-400">{configOpen ? "▾" : "▸"}</span>
            What's set up
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal text-gray-500">
              {plural(configuredProviders.length || (effectiveProvider ? 1 : 0), "provider")} · {plural(conns.length, "tenant")}{isAdmin ? ` · ${plural(connectors.length, "connector")}` : ""}
            </span>
          </button>
          {configOpen && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* AI providers */}
            <Card title="AI providers" icon="🧠" manageTo={isAdmin ? "/admin/providers" : undefined}>
              {isAdmin ? (
                configuredProviders.length === 0 ? (
                  <Empty text="No AI provider configured yet." />
                ) : (
                  <ul className="space-y-2">
                    {configuredProviders.map(([id, c]) => {
                      const ready = c.has_key || KEYLESS_OK.has(id);
                      const active = id === activeProvider;
                      return (
                        <li key={id} className="rounded-lg border bg-gray-50 p-2.5">
                          <div className="flex items-center gap-2">
                            <StatusDot ok={c.disabled ? false : ready} />
                            <span className="truncate text-sm font-medium text-gray-800">{providerLabel(id)}</span>
                            {active && <Badge tone="brand">active</Badge>}
                            {c.disabled && <Badge tone="gray">disabled</Badge>}
                            {c.free_only && <Badge tone="green">free only</Badge>}
                            {KEYLESS_OK.has(id) && !c.has_key && <Badge tone="gray">no key needed</Badge>}
                          </div>
                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 pl-4 text-[11px] text-gray-500">
                            <span>Model: <span className="font-mono text-gray-700">{c.model || "—"}</span></span>
                            {c.api_version && <span>API: <span className="font-mono">{c.api_version}</span></span>}
                            {c.has_key && c.key_hint && <span>Key: <span className="font-mono">{c.key_hint}</span></span>}
                          </div>
                          {c.base_url && (
                            <div className="mt-0.5 truncate pl-4 text-[11px] text-gray-400">
                              Endpoint: <span className="font-mono">{c.base_url}</span>
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )
              ) : effectiveProvider ? (
                <div className="rounded-lg border bg-gray-50 p-2.5">
                  <div className="flex items-center gap-2">
                    <StatusDot ok={aiReady} />
                  <span className="text-sm font-medium text-gray-800">{providerLabel(effectiveProvider)}</span>
                    <Badge tone="brand">active</Badge>
                  </div>
                  <div className="mt-1 pl-4 text-[11px] text-gray-500">Model: <span className="font-mono text-gray-700">{effectiveModel || "—"}</span></div>
                  <p className="mt-1 pl-4 text-[11px] text-gray-400">Providers are managed by an admin.</p>
                </div>
              ) : (
                <Empty text="Managed by an admin." />
              )}
            </Card>

            {/* Azure tenants */}
            <Card title="Azure tenants" icon="🏢" manageTo={isAdmin ? "/admin/tenants" : undefined}>
              {conns.length === 0 ? (
                <Empty text="No Azure tenant connected." />
              ) : (
                <ul className="space-y-2">
                  {conns.map((c) => (
                    <li key={c.id} className="rounded-lg border bg-gray-50 p-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="text-base">🔑</span>
                          <span className="truncate text-sm font-medium text-gray-800">{c.name}</span>
                          {c.is_default && <Badge tone="brand">default</Badge>}
                          {c.read_only && <Badge tone="gray">read-only</Badge>}
                          {c.disabled && <Badge tone="gray">disabled</Badge>}
                        </div>
                        <StatusDot ok={c.disabled ? false : statusOk(c.status)} />
                      </div>
                      <dl className="mt-1.5 space-y-0.5 pl-6 text-[11px]">
                        <Row label="Tenant ID" value={c.tenant_id} mono />
                        {c.auth_method && <Row label="Auth" value={authLabel(c.auth_method)} />}
                        {c.default_subscription && <Row label="Default sub" value={c.default_subscription} mono />}
                        {(c.status || c.status_detail) && <Row label="Status" value={[c.status, c.status_detail].filter(Boolean).join(" · ")} />}
                      </dl>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            {/* Connectors (admin) */}
            {isAdmin && (
              <Card title="Connectors" icon="🔌" manageTo="/admin/connectors">
                {connectors.length === 0 ? (
                  <Empty text="No connectors configured (Teams, Slack, Jira, Grafana, …)." />
                ) : (
                  <ul className="space-y-2">
                    {connectors.map((c) => (
                      <li key={c.id} className="flex items-center justify-between gap-2 rounded-lg border bg-gray-50 p-2.5">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="text-base">{CONNECTOR_ICONS[c.type] ?? "🔌"}</span>
                          <span className="truncate text-sm font-medium text-gray-800">{c.name}</span>
                          <Badge tone="gray">{connectorTypeLabel(c.type)}</Badge>
                          {c.disabled && <Badge tone="gray">disabled</Badge>}
                        </div>
                        <StatusDot ok={c.disabled ? false : statusOk(c.status)} />
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            )}

            {/* Sub agents quick summary (admin) */}
            {isAdmin && (
              <Card title="Sub agents" icon="✨" manageTo="/automations/agents">
                {agents.length === 0 ? (
                  <Empty text="No specialized agents defined yet." />
                ) : (
                  <div className="flex items-center justify-between gap-3 rounded-lg border bg-gray-50 p-2.5">
                    <span className="text-sm text-gray-700">{plural(agents.length, "specialized agent")} ready to dispatch in deep investigations.</span>
                  </div>
                )}
              </Card>
            )}
          </div>
          )}
        </div>
        )}

        {/* Explore — full onboarding cards while setting up; a compact launcher strip once complete */}
        {!isHidden("explore") && (
        <div>
          {allDone ? (
            <>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">Quick links</h2>
              <div className="flex flex-wrap gap-2">
                <QuickLink to="/chat" icon="💬" label="Chat" />
                <QuickLink to="/workloads" icon="🧩" label="Workloads" />
                <QuickLink to="/assessments" icon="🛡️" label="Assessments" />
                <QuickLink to="/architectures" icon="🗺️" label="Architectures" />
                {isAdmin && <QuickLink to="/policy" icon="📐" label="Policy" />}
                <QuickLink to="/automations" icon="⚙️" label="Automations" />
                {isAdmin && <QuickLink to="/monitor" icon="📊" label="Monitor" />}
              </div>
            </>
          ) : (
            <>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">What can I do here?</h2>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <ExploreCard to="/chat" icon="💬" title="Chat with the agent" desc="Ask anything about your Azure estate. It investigates with live data and explains its reasoning." />
                <ExploreCard to="/workloads" icon="🧩" title="Azure Workloads" desc="Group resources into applications — or let ✨ Autopilot discover them for you." />
                <ExploreCard to="/assessments" icon="🛡️" title="Assessments" desc="Score workloads against the Well-Architected Framework, mapped to CIS / NIST / ISO." />
                <ExploreCard to="/architectures" icon="🗺️" title="Architectures" desc="AI-built, editable diagrams of your apps — with drift detection and best-practice review." />
                {isAdmin && <ExploreCard to="/policy" icon="📐" title="Azure Policy" desc="Explore assignments, scan compliance, and simulate guardrails before you enforce — read-only." />}
                <ExploreCard to="/automations" icon="⚙️" title="Automations" desc="Schedule recurring jobs, build workbooks & playbooks, and create specialized sub agents." />
                {isAdmin && <ExploreCard to="/monitor" icon="📊" title="Monitor" desc="Central dashboard of activity, runs, usage, and audit across the workspace." />}
              </div>
            </>
          )}
        </div>
        )}
      </div>
      <PdfGeneratingOverlay
        open={estatePdfBusy}
        onCancel={cancelEstatePdf}
        title="Generating Estate Coverage PDF"
        message="Compiling Monitoring, Telemetry and Backup & DR coverage for this workload into one report. You can cancel while it is processing."
      />
    </div>
  );
}

function Card({ title, icon, manageTo, children }: { title: string; icon: string; manageTo?: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-800"><span>{icon}</span>{title}</h3>
        {manageTo && <Link to={manageTo} className="text-xs font-medium text-brand hover:underline">Manage →</Link>}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <dt className="w-20 shrink-0 text-gray-400">{label}</dt>
      <dd className={`min-w-0 break-all text-gray-600 ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

function Badge({ tone, children }: { tone: "brand" | "green" | "gray"; children: ReactNode }) {
  const cls =
    tone === "brand" ? "bg-brand/10 text-brand" :
    tone === "green" ? "bg-green-100 text-green-700" :
    "bg-gray-100 text-gray-500";
  return <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium ${cls}`}>{children}</span>;
}

function Empty({ text }: { text: string }) {
  return <div className="rounded-lg border border-dashed bg-gray-50/60 p-3 text-center text-xs text-gray-400">{text}</div>;
}

function SkeletonTile() {
  return (
    <div className="flex items-center gap-2.5 rounded-xl border bg-white p-3 shadow-sm">
      <div className="h-6 w-6 shrink-0 animate-pulse rounded-full bg-gray-100" />
      <div className="min-w-0 flex-1">
        <div className="h-4 w-10 animate-pulse rounded bg-gray-100" />
        <div className="mt-1 h-2.5 w-16 animate-pulse rounded bg-gray-100" />
      </div>
    </div>
  );
}

function StatusDot({ ok }: { ok: boolean | undefined }) {
  const cls = ok === true ? "bg-green-500" : ok === false ? "bg-gray-300" : "bg-amber-400";
  return <span className={`mt-0.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full ${cls}`} title={ok === true ? "Connected" : ok === false ? "Not connected" : "Unknown"} />;
}

function KpiTile({ to, icon, label, value, sub, tone }: { to: string; icon: string; label: string; value: number; sub?: string; tone?: "red" | "amber" }) {
  const valueTone = tone === "red" ? "text-red-700" : tone === "amber" ? "text-amber-700" : "text-gray-800";
  const borderTone = tone === "red" ? "border-red-200" : tone === "amber" ? "border-amber-200" : "";
  return (
    <Link to={to} className={`flex items-center gap-2.5 rounded-xl border bg-white p-3 shadow-sm transition hover:border-brand/40 hover:shadow-md ${borderTone}`}>
      <span className="text-xl">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className={`text-lg font-bold leading-tight ${valueTone}`}>{value}</div>
        <div className="truncate text-[11px] text-gray-500">{label}</div>
        {sub && <div className="truncate text-[10px] text-gray-400">{sub}</div>}
      </div>
    </Link>
  );
}

function scoreTone(score: number | null | undefined): string {
  if (score == null) return "bg-gray-100 text-gray-500";
  if (score >= 80) return "bg-green-100 text-green-700";
  if (score >= 60) return "bg-amber-100 text-amber-700";
  return "bg-red-100 text-red-700";
}

// Hideable dashboard sections, shown in the "Customize" panel.
const SECTION_TOGGLES: { key: string; label: string }[] = [
  { key: "attention", label: "Needs attention" },
  { key: "coverage", label: "Coverage" },
  { key: "posture", label: "Posture & risks" },
  { key: "scheduled", label: "Scheduled next" },
  { key: "activity", label: "Recent activity" },
  { key: "setup", label: "Setup guide" },
  { key: "configured", label: "What's set up" },
  { key: "explore", label: "Explore" },
];

function ringTone(v: number): string {
  if (v >= 80) return "#16a34a";
  if (v >= 60) return "#d97706";
  return "#dc2626";
}

/** Compact SVG donut ring for the blended Estate Health score (0-100). */
function HealthRing({ value }: { value: number }) {
  const r = 18;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.max(0, Math.min(100, value)) / 100);
  return (
    <svg viewBox="0 0 44 44" className="h-12 w-12 -rotate-90">
      <circle cx="22" cy="22" r={r} fill="none" stroke="#e5e7eb" strokeWidth="5" />
      <circle cx="22" cy="22" r={r} fill="none" stroke={ringTone(value)} strokeWidth="5" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={off} />
      <text x="22" y="23" textAnchor="middle" dominantBaseline="middle" className="rotate-90" transform="rotate(90 22 22)" fontSize="13" fontWeight="700" fill={ringTone(value)}>{value}</text>
    </svg>
  );
}

/** Per-workload coverage lens tile: headline %, delta badge, and a mini trend sparkline. */
function CoverageLensTile({ lens, scopeName }: { lens: { key: string; label: string; icon: string; to: string; current: number | null; delta: number | null; points: { at: string; pct: number | null }[]; loading: boolean; hasData: boolean }; scopeName: string }) {
  const pct = lens.current;
  const valueTone = pct == null ? "text-gray-400" : pct >= 80 ? "text-green-600" : pct >= 50 ? "text-amber-600" : "text-red-600";
  return (
    <Link to={lens.to} className="rounded-xl border bg-white p-3 shadow-sm transition hover:border-brand/40 hover:shadow-md" title={scopeName}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[12px] font-medium text-gray-600"><span>{lens.icon}</span>{lens.label}</span>
        {lens.delta != null && lens.delta !== 0 && (
          <span className={`rounded px-1 text-[10px] font-bold ${lens.delta > 0 ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
            {lens.delta > 0 ? "▲" : "▼"} {Math.abs(lens.delta)}
          </span>
        )}
      </div>
      {lens.loading ? (
        <div className="mt-2 h-7 w-16 animate-pulse rounded bg-gray-100" />
      ) : lens.hasData ? (
        <>
          <div className={`mt-1 text-2xl font-bold leading-tight ${valueTone}`}>{pct != null ? `${pct}${lens.key === "performance" ? "" : "%"}` : "—"}</div>
          {lens.points.length > 1 && (
            <div className="mt-1 [&_svg]:!h-7">
              <TrendChart points={lens.points} current={lens.current} delta={lens.delta} unit={lens.key === "performance" ? "" : "%"} />
            </div>
          )}
        </>
      ) : (
        <div className="mt-2 text-[11px] text-gray-400">No scan yet — open to run one.</div>
      )}
    </Link>
  );
}

/** Small labelled stat used inside the Identity / RBAC posture cards. */
function MiniStat({ to, label, value, tone }: { to: string; label: string; value: number; tone: "red" | "amber" | "ok" }) {
  const cls = value === 0 || tone === "ok" ? "text-gray-700" : tone === "red" ? "text-red-700" : "text-amber-700";
  return (
    <Link to={to} className="rounded-lg border bg-gray-50 px-2.5 py-1.5 hover:border-brand/40 hover:bg-white">
      <div className={`text-base font-bold leading-tight ${cls}`}>{value}</div>
      <div className="truncate text-[10px] text-gray-500">{label}</div>
    </Link>
  );
}

function formatMoney(n: number, currency?: string): string {
  const cur = currency || "USD";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(n);
  } catch {
    return `$${Math.round(n)}`;
  }
}

/** Relative "in 3h / tomorrow / in 2d" label for an upcoming scheduled run. */
function formatNextRun(iso: string): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const diff = t - Date.now();
  if (diff <= 0) return "due now";
  const m = Math.round(diff / 60000);
  if (m < 60) return `in ${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `in ${h}h`;
  const d = Math.round(h / 24);
  return d === 1 ? "tomorrow" : `in ${d}d`;
}

function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const diff = Date.now() - t;
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.round(d / 30);
  return `${mo}mo ago`;
}

function ExploreCard({ to, icon, title, desc }: { to: string; icon: string; title: string; desc: string }) {
  return (
    <Link to={to} className="group rounded-xl border bg-white p-4 shadow-sm transition hover:border-brand/40 hover:shadow-md">
      <div className="flex items-center gap-2">
        <span className="text-lg">{icon}</span>
        <span className="text-sm font-semibold text-gray-800 group-hover:text-brand">{title}</span>
      </div>
      <p className="mt-1 text-xs text-gray-500">{desc}</p>
    </Link>
  );
}

// Compact icon+label chip used for the quick-links launcher strip once setup is complete.
function QuickLink({ to, icon, label }: { to: string; icon: string; label: string }) {
  return (
    <Link
      to={to}
      className="group inline-flex items-center gap-1.5 rounded-full border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition hover:border-brand/40 hover:text-brand hover:shadow"
    >
      <span className="text-sm">{icon}</span>
      <span>{label}</span>
    </Link>
  );
}
