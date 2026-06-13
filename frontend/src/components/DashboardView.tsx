import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";

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

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-5xl space-y-6 p-6">
        {/* Hero */}
        <div className="rounded-2xl border bg-gradient-to-br from-brand/10 to-violet-50 p-6">
          <div className="flex items-start gap-3">
            <span className="text-3xl">🤖</span>
            <div className="min-w-0">
              <h1 className="text-2xl font-bold text-gray-800">Welcome to the Azure Support Agent</h1>
              <p className="mt-1 max-w-3xl text-sm text-gray-600">
                An agentic AI teammate for your Azure estate. It reasons over your live data to run deep,
                multi-hypothesis root-cause investigations, AI-scores workloads against the Well-Architected
                Framework, recommends and safely rolls out Azure Policy guardrails, reverse-engineers
                architecture diagrams from real resources, and surfaces inventory and cost insights — every
                answer AI-generated from your real Azure data, never guessed. Follow the steps below to get
                set up.
              </p>
            </div>
          </div>
          {/* Progress */}
          <div className="mt-4 flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-white/70">
              <div className={`h-full rounded-full transition-all ${allDone ? "bg-green-500" : "bg-brand"}`} style={{ width: `${pct}%` }} />
            </div>
            <span className="shrink-0 text-xs font-medium text-gray-600">{allDone ? "🎉 You're all set!" : `${doneCount}/${known.length} done`}</span>
          </div>
        </div>

        {/* Setup checklist */}
        <div>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">Setup guide</h2>
          <div className="space-y-2">
            {steps.map((s, i) => {
              const blocked = s.adminOnly && !isAdmin;
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
                    </div>
                    <p className="mt-0.5 text-xs text-gray-500">{s.desc}</p>
                    {s.bullets && (
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
        </div>

        {/* What's set up — detailed */}
        <div>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">What's set up</h2>
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

            {/* Stats */}
            <Card title="Your estate at a glance" icon="📊">
              <div className="grid grid-cols-2 gap-3">
                <StatTile to="/workloads" label="Workloads" value={workloads.length} icon="🧩" />
                <StatTile to="/assessments" label="Assessments" value={runs.length} icon="🛡️" />
                <StatTile to="/architectures" label="Architectures" value={architectures.length} icon="🗺️" />
                {isAdmin && <StatTile to="/policy" label="Policy snapshots" value={policySnapshots.length} icon="📐" />}
                {isAdmin && <StatTile to="/automations/agents" label="Sub agents" value={agents.length} icon="✨" />}
              </div>
            </Card>
          </div>
        </div>

        {/* Explore */}
        <div>
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
        </div>
      </div>
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

function StatusDot({ ok }: { ok: boolean | undefined }) {
  const cls = ok === true ? "bg-green-500" : ok === false ? "bg-gray-300" : "bg-amber-400";
  return <span className={`mt-0.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full ${cls}`} title={ok === true ? "Connected" : ok === false ? "Not connected" : "Unknown"} />;
}

function StatTile({ to, label, value, icon }: { to: string; label: string; value: number; icon: string }) {
  return (
    <Link to={to} className="rounded-lg border bg-gray-50 p-3 text-center transition hover:border-brand/40 hover:bg-white">
      <div className="text-xl">{icon}</div>
      <div className="text-xl font-bold text-gray-800">{value}</div>
      <div className="text-[11px] text-gray-500">{label}</div>
    </Link>
  );
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
