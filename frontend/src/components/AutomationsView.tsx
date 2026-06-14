import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BrandIcon } from "./BrandIcon";
import {
  api,
  type AppConnector,
  type ConnectorTypeMeta,
  type CustomAgent,
  type AgentAnswer,
  type AgentCategory,
  type AgentWizardQuestion,
  type AgentEnhanceCurrent,
  type AgentEnhanceDraft,
  type ScheduledTask,
  type TaskRunInfo,
} from "../api";
import { formatError, formatRelativeFromNow, formatTimestamp } from "../utils/format";

export type { AutomationsSection } from "./navConfig";
import { AUTOMATIONS_NAV, type AutomationsSection } from "./navConfig";
export { AUTOMATIONS_NAV };
import { WorkbooksSection } from "./WorkbooksView";
import { PlaybooksSection } from "./PlaybooksView";
import { NotificationsSection } from "./NotificationsView";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  github: "GitHub Models",
  github_copilot: "GitHub Copilot",
  ollama: "Ollama",
  chatgpt: "ChatGPT Codex",
  azure_openai: "Azure OpenAI",
  claude: "Claude",
  gemini: "Google Gemini",
  grok: "Grok",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  lmstudio: "LM Studio",
};

// Schedule target types (what a schedule invokes) — mirrors backend TARGET_META.
const TARGET_TYPES = ["agent", "assessment", "workbook", "playbook"] as const;
type TargetType = (typeof TARGET_TYPES)[number];
const TARGET_META: Record<TargetType, { label: string; icon: string; blurb: string }> = {
  agent: { label: "Sub Agent", icon: "🤖", blurb: "Run an AI agent that investigates and delivers a report." },
  assessment: { label: "Assessment", icon: "🛡️", blurb: "Score workloads against Well-Architected pillars." },
  workbook: { label: "Workbook", icon: "📓", blurb: "Run an az / KQL / PowerShell operation and AI-summarize it." },
  playbook: { label: "Playbook", icon: "📋", blurb: "Run a chained sequence of workbooks." },
};
const ASSESSMENT_PILLARS = [
  { id: "security", label: "🛡️ Security" },
  { id: "reliability", label: "🔄 Reliability" },
  { id: "cost", label: "💰 Cost Optimization" },
  { id: "operations", label: "⚙️ Operational Excellence" },
  { id: "performance", label: "⚡ Performance Efficiency" },
];
// Recognised Well-Architected methodologies → the pillar bundle each one runs. Selecting a
// pack is a one-click way to schedule a WARA / WASA / full WAF review (mirrors the Run flow).
const ASSESSMENT_PACKS: { id: string; short: string; label: string; icon: string; pillars: string[] }[] = [
  { id: "waf", short: "WAF", label: "Well-Architected Review", icon: "🏛️", pillars: ["security", "reliability", "cost", "operations", "performance"] },
  { id: "wara", short: "WARA", label: "Reliability Assessment", icon: "🔄", pillars: ["reliability"] },
  { id: "wasa", short: "WASA", label: "Security Assessment", icon: "🛡️", pillars: ["security"] },
];

// Fallback sub-agent category metadata (the live list comes from the API).
const AGENT_CATEGORY_FALLBACK: AgentCategory[] = [
  { id: "networking", label: "Networking", icon: "🌐" },
  { id: "compute", label: "Compute", icon: "⚙️" },
  { id: "data", label: "Data & Storage", icon: "🗄️" },
  { id: "security", label: "Security & Identity", icon: "🔐" },
  { id: "operations", label: "Operations & Monitoring", icon: "📈" },
  { id: "cost", label: "Cost & Governance", icon: "💰" },
  { id: "general", label: "General", icon: "🧩" },
];

// All IANA timezones the browser knows, with UTC first and the local zone surfaced.
const TIMEZONES: string[] = (() => {
  let zones: string[] = [];
  try {
    const sv = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf;
    if (sv) zones = sv("timeZone");
  } catch {
    /* older browsers */
  }
  if (zones.length === 0) {
    zones = [
      "UTC",
      "America/New_York",
      "America/Chicago",
      "America/Denver",
      "America/Los_Angeles",
      "Europe/London",
      "Europe/Berlin",
      "Asia/Kolkata",
      "Asia/Dubai",
      "Asia/Singapore",
      "Asia/Tokyo",
      "Australia/Sydney",
    ];
  }
  const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const ordered = ["UTC", ...(local && local !== "UTC" ? [local] : [])];
  return [...ordered, ...zones.filter((z) => !ordered.includes(z))];
})();

export function AutomationsPanel({ section }: { section: AutomationsSection }) {
  // The Sub Agents section renders a long catalog of agent cards, so give it the app's
  // wide responsive width (the other sections are form-like and read better when narrow).
  const wide = section === "agents";
  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className={`mx-auto space-y-6 p-8 ${wide ? "max-w-5xl xl:max-w-6xl 2xl:max-w-screen-2xl" : "max-w-5xl"}`}>
        {section === "overview" && <OverviewSection />}
        {section === "tasks" && <TasksSection />}
        {section === "agents" && <AgentsSection />}
        {section === "connectors" && <ConnectorsSection />}
        {section === "workbooks" && <WorkbooksSection />}
        {section === "playbooks" && <PlaybooksSection />}
        {section === "notifications" && <NotificationsSection />}
      </div>
    </div>
  );
}

function OverviewSection() {
  const tasksQ = useQuery({ queryKey: ["scheduledTasks"], queryFn: api.scheduledTasks });
  const agentsQ = useQuery({ queryKey: ["customAgents"], queryFn: api.customAgents });
  const connQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });

  const metrics = tasksQ.data?.metrics ?? { active: 0, total: 0, total_runs: 0 };
  const agentCount = agentsQ.data?.agents?.length ?? 0;
  const connectorCount = connQ.data?.connectors?.length ?? 0;
  const counts: Record<string, number> = {
    tasks: metrics.total,
    agents: agentCount,
    connectors: connectorCount,
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-gray-800">Automations</h1>
        <p className="mt-1 text-sm text-gray-500">
          Schedule recurring agent workflows that investigate, act, and notify via your
          connectors.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Active schedules", metrics.active],
          ["Total schedules", metrics.total],
          ["Total runs", metrics.total_runs],
          ["Connectors", connectorCount],
        ].map(([k, v]) => (
          <div key={k as string} className="rounded-lg border bg-white p-4 text-center shadow-sm">
            <div className="text-2xl font-semibold text-gray-800">{v as number}</div>
            <div className="text-xs text-gray-500">{k as string}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {AUTOMATIONS_NAV.map((n) => (
          <Link
            key={n.id}
            to={`/automations/${n.id}`}
            className="group rounded-xl border bg-white p-5 shadow-sm transition hover:border-brand hover:shadow"
          >
            <div className="mb-2 flex items-center gap-2 text-base font-semibold text-gray-800">
              <span className="text-xl">{n.icon}</span>
              {n.label}
            </div>
            <p className="text-sm text-gray-500">{n.description}</p>
            <div className="mt-4 flex items-center justify-between text-xs">
              <span className="text-gray-400">{counts[n.id]} configured</span>
              <span className="font-medium text-brand group-hover:underline">Open →</span>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

function Card({ title, children, action }: { title: React.ReactNode; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-medium">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "ok" ? "bg-green-500" : status === "error" ? "bg-red-500" : "bg-gray-300";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} title={status} />;
}

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

// ===========================================================================
// Connectors
// ===========================================================================
export function ConnectorsSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  // The connector wizard modal: null = closed; {} = add (start at the type picker);
  // { presetType } = add starting at a chosen type's setup step (from the gallery);
  // { initial } = edit an existing connector (start at the setup step).
  const [wizard, setWizard] = useState<{ initial?: EditConnector; presetType?: ConnectorTypeMeta } | null>(null);
  const [msg, setMsg] = useState<{ id: string; ok: boolean; text: string } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const connectors = q.data?.connectors ?? [];
  const types = q.data?.types ?? [];

  async function remove(id: string) {
    if (!confirm("Delete this connector?")) return;
    setBusyId(id);
    try {
      await api.deleteConnector(id);
      qc.invalidateQueries({ queryKey: ["connectors"] });
    } finally {
      setBusyId(null);
    }
  }
  async function test(id: string) {
    setBusyId(id);
    setMsg(null);
    try {
      const r = await api.testConnector(id);
      setMsg({ id, ok: r.ok, text: r.ok ? `✓ ${r.detail || "OK"}` : `✗ ${r.detail || "Failed"}` });
      qc.invalidateQueries({ queryKey: ["connectors"] });
    } catch (e) {
      setMsg({ id, ok: false, text: formatError(e) });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
    <Card
      title="Connectors"
      action={
        <button
          onClick={() => setWizard({})}
          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
        >
          + Add connector
        </button>
      }
    >
      <p className="mb-3 text-xs text-gray-500">
        Give the agent tools for external services — Teams, Slack, Microsoft Outlook, Email,
        Jira, ServiceNow, PagerDuty, Splunk, Cortex XSOAR, Grafana, Webhook, Amazon SQS/S3,
        AWS Security Hub, and Azure Service Bus. Secrets are encrypted at rest and never shown again.
      </p>

      {q.isLoading && <div className="h-16 animate-pulse rounded-lg border bg-gray-100" />}

      <div className="space-y-2">
        {connectors.map((c) => (
          <div key={c.id} className="rounded-lg border bg-white p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <BrandIcon type={c.type} className="h-5 w-5" />
                  <span className="font-medium">{c.name}</span>
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] uppercase text-gray-500">
                    {c.type} · {c.mode}
                  </span>
                  <StatusDot status={c.status} />
                  {c.disabled && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">disabled</span>
                  )}
                </div>
                {c.status_detail && (
                  <div className="mt-0.5 text-[11px] text-gray-400">{c.status_detail}</div>
                )}
                {msg && msg.id === c.id && (
                  <div className={`mt-1 text-[11px] ${msg.ok ? "text-green-600" : "text-red-600"}`}>{msg.text}</div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1.5 text-xs">
                <button onClick={() => void test(c.id)} disabled={busyId === c.id} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50">Test</button>
                <button onClick={() => setWizard({ initial: toEdit(c) })} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Edit</button>
                <button onClick={() => void remove(c.id)} disabled={busyId === c.id} className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50 disabled:opacity-50">Delete</button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Catalog of available connectors — shown as the empty state, and below any
          configured connectors so the full set of integrations is always discoverable. */}
      {!q.isLoading && (
        <div className={connectors.length > 0 ? "mt-5 border-t border-gray-200 pt-5" : ""}>
          <ConnectorGallery
            types={types}
            hasConnectors={connectors.length > 0}
            onPick={(ty) => setWizard({ presetType: ty })}
          />
        </div>
      )}
    </Card>
    {wizard && (
      <ConnectorWizard
        types={types}
        initial={wizard.initial}
        presetType={wizard.presetType}
        onClose={() => setWizard(null)}
        onSaved={() => {
          setWizard(null);
          qc.invalidateQueries({ queryKey: ["connectors"] });
        }}
      />
    )}
    </>
  );
}

type EditConnector = {
  id?: string;
  type: string;
  mode: string;
  name: string;
  disabled?: boolean;
  config: Record<string, string>;
};

function toEdit(c: AppConnector): EditConnector {
  const config: Record<string, string> = {};
  for (const [k, v] of Object.entries(c.config)) {
    if (typeof v === "string") config[k] = v;
  }
  return { id: c.id, type: c.type, mode: c.mode, name: c.name, disabled: c.disabled, config };
}

// Marketing-oriented catalog grouping for the empty-state gallery. Any connector type
// not listed here still shows up under a trailing "More" group.
const CONNECTOR_CATEGORIES: { label: string; blurb: string; ids: string[] }[] = [
  { label: "Messaging & ChatOps", blurb: "Reach your team where they already work.", ids: ["teams", "slack", "outlook", "email"] },
  { label: "Ticketing & ITSM", blurb: "Turn findings into tracked work — automatically.", ids: ["jira", "servicenow", "pagerduty"] },
  { label: "Observability & SIEM", blurb: "Push evidence into the tools your SOC already trusts.", ids: ["splunk", "grafana", "securityhub", "xsoar"] },
  { label: "Queues & Storage", blurb: "Pipe results into your own automation and data lake.", ids: ["servicebus", "sqs", "s3"] },
  { label: "Custom", blurb: "Call any HTTP API the agent doesn't natively support.", ids: ["webhook"] },
];

/** Empty-state showcase: advertises every available connector, grouped by use case.
 *  Clicking a card opens the wizard pre-seeded to that connector's setup step. Also
 *  rendered below configured connectors (``hasConnectors``) as an "add more" catalog. */
function ConnectorGallery({
  types,
  onPick,
  hasConnectors = false,
}: {
  types: ConnectorTypeMeta[];
  onPick: (ty: ConnectorTypeMeta) => void;
  hasConnectors?: boolean;
}) {
  const byId = new Map(types.map((t) => [t.id, t]));
  const used = new Set<string>();
  const groups = CONNECTOR_CATEGORIES.map((cat) => {
    const items = cat.ids
      .map((id) => byId.get(id))
      .filter((x): x is ConnectorTypeMeta => !!x);
    items.forEach((i) => used.add(i.id));
    return { label: cat.label, blurb: cat.blurb, items };
  }).filter((g) => g.items.length > 0);
  const leftovers = types.filter((t) => !used.has(t.id));
  if (leftovers.length > 0) groups.push({ label: "More", blurb: "", items: leftovers });

  return (
    <div className="rounded-xl border border-gray-200 bg-gradient-to-b from-brand/5 to-white p-5">
      <div className="mb-5 text-center">
        <h3 className="text-base font-semibold text-gray-800">
          {hasConnectors ? "Add another connector" : "Connect the agent to your stack"}
        </h3>
        <p className="mx-auto mt-1 max-w-xl text-xs text-gray-500">
          Connectors give the agent tools to act — post alerts, raise tickets, page on-call, and push
          evidence into the systems you already run. Pick one to {hasConnectors ? "add it" : "get started"};
          secrets are encrypted at rest and every connector is admin-gated.
        </p>
      </div>
      <div className="space-y-5">
        {groups.map((g) => (
          <div key={g.label}>
            <div className="mb-2 flex items-baseline gap-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{g.label}</h4>
              {g.blurb && <span className="text-[11px] text-gray-400">{g.blurb}</span>}
            </div>
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {g.items.map((ty) => (
                <button
                  key={ty.id}
                  onClick={() => onPick(ty)}
                  className="group flex flex-col rounded-xl border border-gray-200 bg-white p-3.5 text-left transition hover:border-brand hover:shadow-sm"
                >
                  <div className="mb-1.5 flex items-center gap-2.5">
                    <BrandIcon type={ty.id} className="h-6 w-6" />
                    <span className="font-semibold leading-tight text-gray-800">{ty.label}</span>
                  </div>
                  <span className="text-xs text-gray-500">{ty.description}</span>
                  <span className="mt-2 text-[11px] font-medium text-brand opacity-0 transition group-hover:opacity-100">
                    Set up {ty.label} →
                  </span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Modal wizard for adding/editing a connector: a 3-step flow with a left stepper
 *  (Choose a connector → Set up connector → Review + add). For edits the type is
 *  fixed and the flow starts at the setup step. */
function ConnectorWizard({
  types,
  initial,
  presetType,
  onClose,
  onSaved,
}: {
  types: ConnectorTypeMeta[];
  initial?: EditConnector;
  presetType?: ConnectorTypeMeta;
  onClose: () => void;
  onSaved: () => void;
}) {
  // A gallery pick seeds the form to that type and jumps straight to the setup step,
  // while still letting the user step back to choose a different connector.
  const presetInitial: EditConnector | undefined = presetType
    ? { type: presetType.id, mode: Object.keys(presetType.modes)[0] ?? "", name: "", config: {} }
    : undefined;
  const isEdit = !!initial?.id;
  const [step, setStep] = useState<1 | 2 | 3>(isEdit || presetType ? 2 : 1);
  const [search, setSearch] = useState("");
  const [form, setForm] = useState<EditConnector>(
    initial ?? presetInitial ?? { type: "", mode: "", name: "", config: {} },
  );
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const t = types.find((x) => x.id === form.type);
  const modeKeys = t ? Object.keys(t.modes) : [];
  const fields = t?.modes[form.mode] ?? [];

  // Load Azure connections lazily for connector modes that need them (graph modes).
  const needsAzure = fields.some((f) => f.key === "connection_id");
  const azureQ = useQuery({
    queryKey: ["adminConnections"],
    queryFn: api.adminConnections,
    enabled: needsAzure,
  });
  const azureConns = (azureQ.data?.connections ?? []).map((c) => ({
    id: c.id,
    display_name: c.display_name,
  }));

  const set = (patch: Partial<EditConnector>) => setForm((f) => ({ ...f, ...patch }));
  const setCfg = (key: string, val: string) =>
    setForm((f) => ({ ...f, config: { ...f.config, [key]: val } }));

  function pickType(ty: ConnectorTypeMeta) {
    const mode = Object.keys(ty.modes)[0] ?? "";
    setForm((f) => ({ ...f, type: ty.id, mode, config: {} }));
    setError("");
    setStep(2);
  }

  function gotoReview() {
    if (!form.name.trim()) {
      setError("Give the connector a name.");
      return;
    }
    setError("");
    setStep(3);
  }

  async function save() {
    setSaving(true);
    setError("");
    try {
      await api.upsertConnector({
        id: form.id,
        name: form.name,
        type: form.type,
        mode: form.mode,
        disabled: form.disabled,
        config: form.config,
      });
      onSaved();
    } catch (e) {
      setError(formatError(e));
      setStep(2);
    } finally {
      setSaving(false);
    }
  }

  const q = search.trim().toLowerCase();
  const filteredTypes = q
    ? types.filter(
        (ty) =>
          ty.label.toLowerCase().includes(q) || ty.description.toLowerCase().includes(q),
      )
    : types;

  const STEPS: { n: 1 | 2 | 3; label: string }[] = [
    { n: 1, label: "Choose a connector" },
    { n: 2, label: "Set up connector" },
    { n: 3, label: "Review + add" },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex h-[640px] max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">
            {isEdit ? "Edit connector" : "Add a connector"}
          </h2>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Body: stepper + content */}
        <div className="flex min-h-0 flex-1">
          {/* Left stepper */}
          <div className="hidden w-60 shrink-0 border-r border-gray-200 bg-gray-50/60 p-6 sm:block">
            <ol className="space-y-1">
              {STEPS.map((s, i) => {
                const active = step === s.n;
                const done = step > s.n;
                return (
                  <li key={s.n}>
                    <button
                      onClick={() => {
                        // Allow jumping back to a completed step (not forward).
                        if (s.n < step && !(isEdit && s.n === 1)) setStep(s.n);
                      }}
                      disabled={s.n >= step || (isEdit && s.n === 1)}
                      className={`flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm transition ${
                        s.n < step && !(isEdit && s.n === 1)
                          ? "text-gray-600 hover:bg-gray-100"
                          : "cursor-default"
                      }`}
                    >
                      <span
                        className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                          active
                            ? "bg-brand text-white"
                            : done
                              ? "bg-brand/15 text-brand"
                              : "bg-gray-200 text-gray-500"
                        }`}
                      >
                        {done ? "✓" : s.n}
                      </span>
                      <span className={active ? "font-medium text-gray-900" : ""}>{s.label}</span>
                    </button>
                    {i < STEPS.length - 1 && (
                      <span className="ml-[1.45rem] block h-3 w-px bg-gray-200" />
                    )}
                  </li>
                );
              })}
            </ol>
          </div>

          {/* Right content */}
          <div className="min-w-0 flex-1 overflow-y-auto p-6">
            {/* Step 1 — choose a connector */}
            {step === 1 && (
              <>
                <h3 className="mb-3 text-base font-semibold text-gray-800">Choose a connector</h3>
                <div className="relative mb-4">
                  <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400">
                    <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                      <circle cx="9" cy="9" r="5.5" />
                      <path d="M13.5 13.5L17 17" strokeLinecap="round" />
                    </svg>
                  </span>
                  <input
                    autoFocus
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search"
                    className="w-full rounded-lg border border-gray-300 py-2 pl-9 pr-3 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                  />
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {filteredTypes.map((ty) => (
                    <button
                      key={ty.id}
                      onClick={() => pickType(ty)}
                      className="group flex flex-col rounded-xl border border-gray-200 bg-white p-4 text-left transition hover:border-brand hover:shadow-sm"
                    >
                      <div className="mb-2 flex items-start gap-2.5">
                        <BrandIcon type={ty.id} className="h-6 w-6" />
                        <span className="min-w-0">
                          <span className="block font-semibold leading-tight text-gray-800">
                            {ty.label}
                          </span>
                        </span>
                      </div>
                      <span className="text-xs text-gray-500">{ty.description}</span>
                    </button>
                  ))}
                  {filteredTypes.length === 0 && (
                    <p className="col-span-full py-6 text-center text-sm text-gray-400">
                      No connectors match &ldquo;{search}&rdquo;.
                    </p>
                  )}
                </div>
              </>
            )}

            {/* Step 2 — set up connector */}
            {step === 2 && t && (
              <>
                <h3 className="mb-1 flex items-center gap-2 text-base font-semibold text-gray-800">
                  <BrandIcon type={form.type} className="h-5 w-5" />
                  Set up {t.label}
                </h3>
                <p className="mb-4 text-xs text-gray-500">{t.description}</p>
                <div className="space-y-3">
                  <div>
                    <label className={label}>Name</label>
                    <input
                      className={input}
                      value={form.name}
                      onChange={(e) => set({ name: e.target.value })}
                      placeholder="e.g. Ops Teams"
                      autoComplete="off"
                    />
                  </div>
                  {modeKeys.length > 1 && (
                    <div>
                      <label className={label}>Mode</label>
                      <div className="flex flex-wrap gap-2">
                        {modeKeys.map((m) => (
                          <button
                            key={m}
                            onClick={() => set({ mode: m, config: {} })}
                            className={`rounded-lg border px-3 py-1.5 text-sm capitalize transition ${
                              form.mode === m
                                ? "border-brand bg-brand/5 font-medium text-brand"
                                : "border-gray-200 text-gray-600 hover:bg-gray-50"
                            }`}
                          >
                            {m}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {fields.map((f) => (
                    <div key={f.key}>
                      <label className={label}>
                        {f.label}
                        {f.optional && <span className="text-gray-400"> (optional)</span>}
                      </label>
                      {f.key === "connection_id" ? (
                        <select
                          className={input}
                          value={form.config[f.key] ?? ""}
                          onChange={(e) => setCfg(f.key, e.target.value)}
                        >
                          <option value="">— Select an Azure connection —</option>
                          {azureConns.map((c) => (
                            <option key={c.id} value={c.id}>
                              {c.display_name}
                            </option>
                          ))}
                        </select>
                      ) : f.options && f.options.length > 0 ? (
                        <select
                          className={input}
                          value={form.config[f.key] ?? ""}
                          onChange={(e) => setCfg(f.key, e.target.value)}
                        >
                          <option value="">— Select —</option>
                          {f.options.map((opt) => (
                            <option key={opt} value={opt}>
                              {opt}
                            </option>
                          ))}
                        </select>
                      ) : f.type === "textarea" ? (
                        <textarea
                          rows={3}
                          className={`${input} font-mono text-xs`}
                          value={form.config[f.key] ?? ""}
                          onChange={(e) => setCfg(f.key, e.target.value)}
                          placeholder={f.placeholder}
                          autoComplete="off"
                        />
                      ) : (
                        <input
                          type={f.secret ? "password" : "text"}
                          className={input}
                          value={form.config[f.key] ?? ""}
                          onChange={(e) => setCfg(f.key, e.target.value)}
                          placeholder={form.id && f.secret ? "•••• (leave blank to keep)" : f.placeholder}
                          autoComplete={f.secret ? "new-password" : "off"}
                        />
                      )}
                      {f.help && <p className="mt-0.5 text-[11px] text-gray-400">{f.help}</p>}
                    </div>
                  ))}
                  <label className="flex items-center gap-2 text-sm text-gray-600">
                    <input
                      type="checkbox"
                      checked={!form.disabled}
                      onChange={(e) => set({ disabled: !e.target.checked })}
                    />
                    Enabled
                  </label>
                </div>
              </>
            )}

            {/* Step 3 — review + add */}
            {step === 3 && t && (
              <>
                <h3 className="mb-3 text-base font-semibold text-gray-800">Review + add</h3>
                <div className="rounded-xl border border-gray-200">
                  <div className="flex items-center gap-2.5 border-b border-gray-100 px-4 py-3">
                    <BrandIcon type={form.type} className="h-6 w-6" />
                    <div>
                      <div className="font-semibold text-gray-800">{form.name || "(unnamed)"}</div>
                      <div className="text-xs text-gray-500">
                        {t.label} · {form.mode}
                      </div>
                    </div>
                    <span
                      className={`ml-auto rounded-full px-2 py-0.5 text-[11px] ${
                        form.disabled ? "bg-amber-100 text-amber-700" : "bg-emerald-100 text-emerald-700"
                      }`}
                    >
                      {form.disabled ? "Disabled" : "Enabled"}
                    </span>
                  </div>
                  <dl className="divide-y divide-gray-100">
                    {fields.map((f) => {
                      const raw = form.config[f.key] ?? "";
                      const display = f.secret
                        ? raw
                          ? "••••••••"
                          : form.id
                            ? "(unchanged)"
                            : "—"
                        : f.key === "connection_id"
                          ? azureConns.find((c) => c.id === raw)?.display_name || raw || "—"
                          : raw || "—";
                      return (
                        <div key={f.key} className="flex gap-3 px-4 py-2 text-sm">
                          <dt className="w-40 shrink-0 text-gray-500">{f.label}</dt>
                          <dd className="min-w-0 flex-1 break-all text-gray-800">{display}</dd>
                        </div>
                      );
                    })}
                    {fields.length === 0 && (
                      <div className="px-4 py-3 text-sm text-gray-400">No fields to configure.</div>
                    )}
                  </dl>
                </div>
                <p className="mt-3 text-[11px] text-gray-400">
                  Secrets are encrypted at rest and never shown again after saving.
                </p>
              </>
            )}

            {error && <div className="mt-3 text-xs text-red-600">{error}</div>}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-gray-200 px-6 py-4">
          <button
            onClick={() => {
              if (step === 1 || (isEdit && step === 2)) onClose();
              else setStep((s) => (s - 1) as 1 | 2 | 3);
            }}
            className="rounded-lg border border-gray-300 px-4 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50"
          >
            {step === 1 || (isEdit && step === 2) ? "Cancel" : "Back"}
          </button>
          <div className="flex items-center gap-2">
            {step === 1 && (
              <button
                disabled
                className="cursor-not-allowed rounded-lg bg-gray-200 px-4 py-1.5 text-sm font-medium text-gray-400"
                title="Pick a connector above"
              >
                Next
              </button>
            )}
            {step === 2 && (
              <button
                onClick={gotoReview}
                className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90"
              >
                Next
              </button>
            )}
            {step === 3 && (
              <button
                onClick={() => void save()}
                disabled={saving}
                className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
              >
                {saving ? "Saving…" : isEdit ? "Save changes" : "Add connector"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// Custom Agents
// ===========================================================================
function AgentsSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["customAgents"], queryFn: api.customAgents });
  const [editing, setEditing] = useState<Partial<CustomAgent> | null>(null);
  // When set, the AI wizard is open; on completion it hands a prefilled draft to the
  // standard AgentForm (via setEditing) for final review + save.
  const [wizardOpen, setWizardOpen] = useState(false);
  // When set, the AI ENHANCE wizard is open for this existing agent.
  const [enhancing, setEnhancing] = useState<CustomAgent | null>(null);
  // Bulk selection: ids of agents whose model we want to change together.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // Group the list by category (default) or show a flat list; optional category filter.
  const [grouped, setGrouped] = useState(true);
  const [catFilter, setCatFilter] = useState<string>("all");
  const agents = q.data?.agents ?? [];
  const tools = q.data?.tools ?? [];
  const categories = q.data?.categories ?? AGENT_CATEGORY_FALLBACK;
  const catMeta = (id: string): AgentCategory =>
    categories.find((c) => c.id === id) ?? { id, label: id, icon: "🧩" };
  const catCounts = agents.reduce<Record<string, number>>((acc, a) => {
    const c = a.category ?? "general";
    acc[c] = (acc[c] ?? 0) + 1;
    return acc;
  }, {});
  const filteredAgents = catFilter === "all" ? agents : agents.filter((a) => (a.category ?? "general") === catFilter);

  // Drop any selected ids that no longer exist after a refetch.
  const validSelected = new Set([...selectedIds].filter((id) => agents.some((a) => a.id === id)));
  const allSelected = agents.length > 0 && validSelected.size === agents.length;

  function toggleOne(id: string, on: boolean) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }
  function toggleAll(on: boolean) {
    setSelectedIds(on ? new Set(agents.map((a) => a.id)) : new Set());
  }

  async function remove(id: string) {
    if (!confirm("Delete this sub agent?")) return;
    await api.deleteAgent(id);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    qc.invalidateQueries({ queryKey: ["customAgents"] });
  }

  async function toggleEnabled(a: CustomAgent) {
    await api.setAgentEnabled(a.id, a.name, !(a.enabled ?? true));
    qc.invalidateQueries({ queryKey: ["customAgents"] });
  }

  // Trigger a browser download of a JSON object.
  function downloadJson(filename: string, data: unknown) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function exportOne(a: CustomAgent) {
    const data = await api.exportAgent(a.id);
    const safe = a.name.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
    downloadJson(`agent-${safe || a.id}.json`, data);
  }

  async function exportMany(ids: string[]) {
    const data = await api.exportAgents(ids);
    const stamp = new Date().toISOString().slice(0, 10);
    downloadJson(`agents-${ids.length ? ids.length : "all"}-${stamp}.json`, data);
  }

  // Import agents from a previously exported JSON file (single or bulk shape).
  const importInputRef = useRef<HTMLInputElement>(null);
  const [importMsg, setImportMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function onImportFile(file: File) {
    setImportMsg(null);
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const res = await api.importAgents(data, true);
      setImportMsg({
        ok: true,
        text: `Imported ${res.agents.length} agent${res.agents.length === 1 ? "" : "s"} `
          + `(${res.created} new, ${res.updated} updated).`,
      });
      qc.invalidateQueries({ queryKey: ["customAgents"] });
    } catch (e) {
      setImportMsg({ ok: false, text: formatError(e) });
    }
  }

  function renderAgentCard(a: CustomAgent) {
    const checked = validSelected.has(a.id);
    const cat = catMeta(a.category ?? "general");
    return (
      <div key={a.id} className={`rounded-lg border bg-white p-3 ${checked ? "border-brand/50 ring-1 ring-brand/30" : ""}`}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2.5">
            <input
              type="checkbox"
              className="mt-1 shrink-0"
              checked={checked}
              onChange={(e) => toggleOne(a.id, e.target.checked)}
              aria-label={`Select ${a.name}`}
            />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className={`font-medium ${a.enabled === false ? "text-gray-400" : ""}`}>{a.name}</span>
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500" title={`Category: ${cat.label}`}>{cat.icon} {cat.label}</span>
                {a.enabled === false && (
                  <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">Disabled</span>
                )}
                <span className={`rounded px-1.5 py-0.5 text-[10px] ${a.run_mode === "autonomous" ? "bg-amber-100 text-amber-700" : "bg-gray-100 text-gray-500"}`}>{a.run_mode}</span>
                {a.model && (
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">
                    {(a.provider && PROVIDER_LABELS[a.provider]) || a.provider} · {a.model}
                  </span>
                )}
              </div>
              <div className="mt-0.5 line-clamp-2 text-xs text-gray-500">{a.instructions}</div>
              {a.connector_tools.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {a.connector_tools.map((t) => (
                    <span key={t} className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-mono text-brand">{t}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5 text-xs">
            <button
              type="button"
              role="switch"
              aria-checked={a.enabled !== false}
              title={a.enabled === false ? "Enable agent" : "Disable agent"}
              onClick={() => void toggleEnabled(a)}
              className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${a.enabled !== false ? "bg-green-500" : "bg-gray-300"}`}
            >
              <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${a.enabled !== false ? "translate-x-[18px]" : "translate-x-0.5"}`} />
            </button>
            <Link
              to={`/chat?agent=${encodeURIComponent(a.id)}`}
              title={a.enabled === false ? "Enable the agent to chat with it" : `Start a chat with ${a.name}`}
              aria-disabled={a.enabled === false}
              onClick={(e) => { if (a.enabled === false) e.preventDefault(); }}
              className={`rounded border px-2 py-1 font-medium ${a.enabled === false ? "cursor-not-allowed border-gray-200 text-gray-300" : "border-brand/40 text-brand hover:bg-brand/5"}`}
            >
              💬 Chat
            </Link>
            <button onClick={() => setEnhancing(a)} title="Enhance this agent with AI" className="rounded border border-brand/40 px-2 py-1 font-medium text-brand hover:bg-brand/5">✨ Enhance</button>
            <button onClick={() => setEditing(a)} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Edit</button>
            <button onClick={() => void exportOne(a)} title="Export config as JSON" className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Export</button>
            <button onClick={() => void remove(a.id)} className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50">Delete</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <Card
      title="Sub Agents"
      action={
        !editing && !wizardOpen && !enhancing && (
          <div className="flex items-center gap-2">
            {agents.length > 0 && (
              <button
                onClick={() => void exportMany([])}
                title="Export all agents as JSON"
                className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-50"
              >
                ⬇ Export all
              </button>
            )}
            <button
              onClick={() => importInputRef.current?.click()}
              title="Import agents from a JSON export file"
              className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-50"
            >
              ⬆ Import
            </button>
            <input
              ref={importInputRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onImportFile(f);
                e.target.value = ""; // allow re-importing the same file
              }}
            />
            <button
              onClick={() => setWizardOpen(true)}
              className="flex items-center gap-1.5 rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm font-medium text-brand transition hover:bg-brand/5"
            >
              ✨ Generate with AI
            </button>
            <button onClick={() => setEditing({ name: "", instructions: "", run_mode: "review", connector_tools: [], allow_all_azure: true })} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90">
              + New agent
            </button>
          </div>
        )
      }
    >
      <p className="mb-3 text-xs text-gray-500">
        A sub agent has its own instructions, model, tenant, and a chosen set of tools.
        Scheduled tasks invoke a sub agent.
      </p>
      {importMsg && (
        <div
          className={`mb-3 flex items-start justify-between gap-3 rounded-lg border px-3 py-2 text-xs ${
            importMsg.ok
              ? "border-green-200 bg-green-50 text-green-700"
              : "border-red-200 bg-red-50 text-red-700"
          }`}
        >
          <span>{importMsg.ok ? "✓ " : "✗ "}{importMsg.text}</span>
          <button onClick={() => setImportMsg(null)} className="shrink-0 text-gray-400 hover:text-gray-600">✕</button>
        </div>
      )}
      {wizardOpen && (
        <AgentWizard
          tools={tools}
          onCancel={() => setWizardOpen(false)}
          onDraft={(draft) => {
            setWizardOpen(false);
            setEditing(draft);
          }}
        />
      )}
      {enhancing && (
        <AgentEnhanceWizard
          agent={enhancing}
          tools={tools}
          onCancel={() => setEnhancing(null)}
          onApply={(draft) => {
            // Hand the enhanced draft to the standard form (with the agent id) for a
            // final review + save. Editing an existing id => update in place.
            setEnhancing(null);
            setEditing({ ...enhancing, ...draft });
          }}
        />
      )}
      {editing && (
        <AgentForm
          value={editing}
          tools={tools}
          categories={categories}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["customAgents"] });
          }}
        />
      )}
      {q.isLoading && <div className="h-16 animate-pulse rounded-lg border bg-gray-100" />}
      {!q.isLoading && agents.length === 0 && !editing && !wizardOpen && !enhancing && (
        <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-sm text-gray-500">No sub agents yet.</div>
      )}

      {/* Bulk selection header + action bar */}
      {!editing && !wizardOpen && !enhancing && agents.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={allSelected}
              ref={(el) => {
                if (el) el.indeterminate = validSelected.size > 0 && !allSelected;
              }}
              onChange={(e) => toggleAll(e.target.checked)}
            />
            {validSelected.size > 0 ? `${validSelected.size} selected` : "Select all"}
          </label>
          {validSelected.size > 0 && (
            <>
              <button
                onClick={() => void exportMany([...validSelected])}
                className="rounded-lg border border-gray-200 bg-white px-2.5 py-1 text-xs font-medium text-gray-600 transition hover:bg-gray-50"
              >
                ⬇ Export selected
              </button>
              <BulkModelBar
                count={validSelected.size}
                onClear={() => setSelectedIds(new Set())}
                onApply={async (provider, model) => {
                  const targets = agents.filter((a) => validSelected.has(a.id));
                  await Promise.all(
                    targets.map((a) =>
                      api.upsertAgent({ id: a.id, name: a.name, provider, model }),
                    ),
                  );
                  setSelectedIds(new Set());
                  qc.invalidateQueries({ queryKey: ["customAgents"] });
                }}
              />
            </>
          )}
        </div>
      )}

      {/* Group toggle + category filter */}
      {!editing && !wizardOpen && !enhancing && agents.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-md border text-xs">
            <button onClick={() => setCatFilter("all")} className={`px-2.5 py-1 ${catFilter === "all" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All ({agents.length})</button>
            {categories.filter((c) => catCounts[c.id]).map((c) => (
              <button key={c.id} onClick={() => setCatFilter(c.id)} className={`border-l px-2.5 py-1 ${catFilter === c.id ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
                {c.icon} {c.label} ({catCounts[c.id]})
              </button>
            ))}
          </div>
          <label className="ml-auto flex items-center gap-1.5 text-xs text-gray-500">
            <span>Group by category</span>
            <button type="button" role="switch" aria-checked={grouped} onClick={() => setGrouped((v) => !v)}
              className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${grouped ? "bg-brand" : "bg-gray-300"}`}>
              <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${grouped ? "translate-x-[18px]" : "translate-x-0.5"}`} />
            </button>
          </label>
        </div>
      )}

      {!editing && !wizardOpen && !enhancing && (
        grouped && catFilter === "all" ? (
          <div className="space-y-4">
            {categories.filter((c) => catCounts[c.id]).map((c) => (
              <div key={c.id}>
                <div className="mb-1.5 flex items-center gap-2 border-b pb-1 text-xs font-semibold text-gray-600">
                  <span>{c.icon} {c.label}</span>
                  <span className="rounded-full bg-gray-100 px-1.5 text-[10px] font-normal text-gray-500">{catCounts[c.id]}</span>
                </div>
                <div className="grid grid-cols-1 gap-2 2xl:grid-cols-2">
                  {agents.filter((a) => (a.category ?? "general") === c.id).map((a) => renderAgentCard(a))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-2 2xl:grid-cols-2">{filteredAgents.map((a) => renderAgentCard(a))}</div>
        )
      )}
    </Card>
  );
}

/** Inline bar to bulk-apply a provider/model to the selected custom agents. */
function BulkModelBar({
  count,
  onApply,
  onClear,
}: {
  count: number;
  onApply: (provider: string, model: string) => Promise<void>;
  onClear: () => void;
}) {
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);

  const cfg = useQuery({ queryKey: ["llmConfig"], queryFn: api.llmConfig });
  const providers = cfg.data
    ? Object.entries(cfg.data.providers)
        .filter(([, p]) => !p.disabled)
        .map(([id]) => id)
    : [];
  const models = useQuery({
    queryKey: ["agentModels", provider],
    queryFn: () => api.llmModels(provider),
    enabled: !!provider,
  });
  const modelList = models.data?.models ?? [];

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1.5">
      <span className="text-xs font-medium text-gray-700">Set model for {count}:</span>
      <select
        className="rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700"
        value={provider}
        onChange={(e) => {
          setProvider(e.target.value);
          setModel("");
        }}
      >
        <option value="">Provider…</option>
        {providers.map((p) => (
          <option key={p} value={p}>
            {PROVIDER_LABELS[p] ?? p}
          </option>
        ))}
      </select>
      <select
        className="rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 disabled:opacity-50"
        value={model}
        onChange={(e) => setModel(e.target.value)}
        disabled={!provider}
      >
        <option value="">{provider ? (models.isLoading ? "Loading…" : "Provider default") : "—"}</option>
        {modelList.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <button
        disabled={!provider || busy}
        onClick={async () => {
          setBusy(true);
          try {
            await onApply(provider, model);
            setProvider("");
            setModel("");
          } finally {
            setBusy(false);
          }
        }}
        className="rounded-md bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-50"
      >
        {busy ? "Applying…" : `Apply to ${count}`}
      </button>
      <button
        onClick={onClear}
        className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
      >
        Clear
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI agent designer wizard
// ---------------------------------------------------------------------------
const WIZARD_EXAMPLES = [
  "Investigate performance issues for VMs and Azure App Service",
  "Audit network security and publicly exposed resources",
  "Review cost and recommend optimizations",
  "Diagnose storage account connectivity problems",
  "Triage incidents from alerts and create tickets",
];

type WizardStage = "intent" | "interview" | "generating" | "error";

function AgentWizard({
  tools,
  onCancel,
  onDraft,
}: {
  tools: { name: string; description: string; connector_name: string }[];
  onCancel: () => void;
  onDraft: (draft: Partial<CustomAgent>) => void;
}) {
  const [stage, setStage] = useState<WizardStage>("intent");
  const [goal, setGoal] = useState("");
  const [step, setStep] = useState(0);
  const [questions, setQuestions] = useState<AgentWizardQuestion[]>([]);
  const [note, setNote] = useState("");
  // Accumulated answers across all interview steps (keyed by question id).
  const [answers, setAnswers] = useState<AgentAnswer[]>([]);
  // Working answers for the CURRENT step's questions: id -> value(s).
  const [current, setCurrent] = useState<Record<string, string | string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function startInterview() {
    if (!goal.trim()) {
      setError("Tell the wizard what the agent should do.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const res = await api.agentInterview(goal.trim(), [], 0);
      if (res.done || res.questions.length === 0) {
        await generate([]);
        return;
      }
      setQuestions(res.questions);
      setNote(res.note);
      setStep(1);
      setCurrent({});
      setCustom({});
      setStage("interview");
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  function setAnswer(qid: string, value: string | string[]) {
    setCurrent((c) => ({ ...c, [qid]: value }));
  }
  function toggleMulti(qid: string, opt: string) {
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      const next = prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt];
      return { ...c, [qid]: next };
    });
  }

  // Merge the current step's answers, then either fetch the next batch or generate.
  async function submitStep() {
    const merged: AgentAnswer[] = questions.map((q) => {
      let value: string | string[] = current[q.id] ?? (q.kind === "multi" ? [] : "");
      const extra = (custom[q.id] ?? "").trim();
      if (extra) {
        value = q.kind === "multi" ? [...(Array.isArray(value) ? value : []), extra] : extra;
      }
      return { id: q.id, prompt: q.prompt, answer: value };
    });
    const all = [...answers, ...merged];
    setAnswers(all);
    setBusy(true);
    setError("");
    try {
      const res = await api.agentInterview(goal.trim(), all, step);
      if (res.done || res.questions.length === 0) {
        await generate(all);
        return;
      }
      setQuestions(res.questions);
      setNote(res.note);
      setStep((s) => s + 1);
      setCurrent({});
      setCustom({});
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  async function generate(all: AgentAnswer[]) {
    setStage("generating");
    setBusy(true);
    setError("");
    try {
      const { draft } = await api.agentGenerate(goal.trim(), all);
      const wantsTool = new Set(draft.connector_tools);
      onDraft({
        name: draft.name,
        instructions: draft.instructions,
        connector_tools: tools.filter((t) => wantsTool.has(t.name)).map((t) => t.name),
        allow_all_azure: draft.allow_all_azure,
        run_mode: draft.run_mode,
        category: draft.category || undefined,
        provider: draft.suggested_provider || undefined,
        model: draft.suggested_model || undefined,
      });
    } catch (e) {
      setError(formatError(e));
      setStage("error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 space-y-4 rounded-xl border border-brand/30 bg-brand/5 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-800">
          ✨ Design an agent with AI
        </div>
        <button onClick={onCancel} className="text-xs text-gray-400 hover:text-gray-600">
          ✕ Close
        </button>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <span className={stage === "intent" ? "font-semibold text-brand" : ""}>1. Goal</span>
        <span>→</span>
        <span className={stage === "interview" ? "font-semibold text-brand" : ""}>
          2. AI interview{stage === "interview" ? ` (Q${step})` : ""}
        </span>
        <span>→</span>
        <span className={stage === "generating" ? "font-semibold text-brand" : ""}>
          3. Generate
        </span>
        <span>→</span>
        <span>4. Review &amp; save</span>
      </div>

      {stage === "intent" && (
        <div className="space-y-3">
          <div>
            <label className={label}>What should this agent do?</label>
            <textarea
              rows={3}
              className={input}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="e.g. Investigate performance issues for VMs and Azure App Service — high CPU/memory, slow response times — and email a root-cause summary."
            />
          </div>
          <div className="flex flex-wrap gap-1.5">
            {WIZARD_EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => setGoal(ex)}
                className="rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[11px] text-gray-600 transition hover:border-brand/40 hover:text-brand"
              >
                {ex}
              </button>
            ))}
          </div>
          {error && <div className="text-xs text-red-600">{error}</div>}
          <div className="flex gap-2">
            <button
              onClick={() => void startInterview()}
              disabled={busy}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-60"
            >
              {busy ? "Thinking…" : "Start AI interview →"}
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </div>
      )}

      {stage === "interview" && (
        <div className="space-y-4">
          {note && <div className="text-xs text-gray-500">{note}</div>}
          {questions.map((q) => (
            <div key={q.id} className="space-y-1.5">
              <div className="text-sm font-medium text-gray-800">{q.prompt}</div>
              {q.kind === "text" ? (
                <textarea
                  rows={2}
                  className={input}
                  value={(current[q.id] as string) ?? ""}
                  onChange={(e) => setAnswer(q.id, e.target.value)}
                  placeholder="Type your answer…"
                />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {q.options.map((opt) => {
                    const sel =
                      q.kind === "multi"
                        ? Array.isArray(current[q.id]) && (current[q.id] as string[]).includes(opt)
                        : current[q.id] === opt;
                    return (
                      <button
                        key={opt}
                        onClick={() =>
                          q.kind === "multi" ? toggleMulti(q.id, opt) : setAnswer(q.id, opt)
                        }
                        className={`rounded-lg border px-2.5 py-1.5 text-sm transition ${
                          sel
                            ? "border-brand bg-brand/10 font-medium text-brand"
                            : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
                        }`}
                      >
                        {q.kind === "multi" && <span className="mr-1">{sel ? "✓" : "+"}</span>}
                        {opt}
                      </button>
                    );
                  })}
                </div>
              )}
              {q.allow_custom && q.kind !== "text" && (
                <input
                  className={input}
                  value={custom[q.id] ?? ""}
                  onChange={(e) => setCustom((c) => ({ ...c, [q.id]: e.target.value }))}
                  placeholder="Or add your own…"
                />
              )}
            </div>
          ))}
          {error && <div className="text-xs text-red-600">{error}</div>}
          <div className="flex items-center gap-2">
            <button
              onClick={() => void submitStep()}
              disabled={busy}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-60"
            >
              {busy ? "Thinking…" : "Continue →"}
            </button>
            <button
              onClick={() => void generate(answers)}
              disabled={busy}
              className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm text-brand transition hover:bg-brand/5 disabled:opacity-60"
              title="Skip remaining questions and generate now"
            >
              Generate now
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </div>
      )}

      {stage === "generating" && (
        <div className="flex items-center gap-3 py-6 text-sm text-gray-600">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          Designing your agent — writing instructions, choosing tools and run mode…
        </div>
      )}

      {stage === "error" && (
        <div className="space-y-3">
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error || "The AI could not draft an agent."}
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => void generate(answers)}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
            >
              Retry
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI agent ENHANCER (existing agent) — assess → interview → review before/after
// ---------------------------------------------------------------------------
type EnhanceStage = "loading" | "interview" | "generating" | "review" | "error";

function AgentEnhanceWizard({
  agent,
  tools,
  onCancel,
  onApply,
}: {
  agent: CustomAgent;
  tools: { name: string; description: string; connector_name: string }[];
  onCancel: () => void;
  onApply: (draft: Partial<CustomAgent>) => void;
}) {
  const [stage, setStage] = useState<EnhanceStage>("loading");
  const [assessment, setAssessment] = useState("");
  const [step, setStep] = useState(0);
  const [questions, setQuestions] = useState<AgentWizardQuestion[]>([]);
  const [note, setNote] = useState("");
  const [answers, setAnswers] = useState<AgentAnswer[]>([]);
  const [current, setCurrent] = useState<Record<string, string | string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Review state.
  const [draft, setDraft] = useState<AgentEnhanceDraft | null>(null);
  const [baseline, setBaseline] = useState<AgentEnhanceCurrent | null>(null);
  const [showDiff, setShowDiff] = useState(false);

  // Kick off the first assessment + questions on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.enhanceInterview(agent.id, [], 0);
        if (cancelled) return;
        setAssessment(res.assessment);
        if (res.done || res.questions.length === 0) {
          await generate([]);
          return;
        }
        setQuestions(res.questions);
        setNote(res.note);
        setStep(1);
        setStage("interview");
      } catch (e) {
        if (!cancelled) {
          setError(formatError(e));
          setStage("error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id]);

  function setAnswer(qid: string, value: string | string[]) {
    setCurrent((c) => ({ ...c, [qid]: value }));
  }
  function toggleMulti(qid: string, opt: string) {
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      const next = prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt];
      return { ...c, [qid]: next };
    });
  }

  async function submitStep() {
    const merged: AgentAnswer[] = questions.map((qq) => {
      let value: string | string[] = current[qq.id] ?? (qq.kind === "multi" ? [] : "");
      const extra = (custom[qq.id] ?? "").trim();
      if (extra) value = qq.kind === "multi" ? [...(Array.isArray(value) ? value : []), extra] : extra;
      return { id: qq.id, prompt: qq.prompt, answer: value };
    });
    const all = [...answers, ...merged];
    setAnswers(all);
    setBusy(true);
    setError("");
    try {
      const res = await api.enhanceInterview(agent.id, all, step);
      if (res.assessment) setAssessment(res.assessment);
      if (res.done || res.questions.length === 0) {
        await generate(all);
        return;
      }
      setQuestions(res.questions);
      setNote(res.note);
      setStep((s) => s + 1);
      setCurrent({});
      setCustom({});
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  async function generate(all: AgentAnswer[]) {
    setStage("generating");
    setBusy(true);
    setError("");
    try {
      const res = await api.enhanceGenerate(agent.id, all);
      setDraft(res.draft);
      setBaseline(res.current);
      setStage("review");
    } catch (e) {
      setError(formatError(e));
      setStage("error");
    } finally {
      setBusy(false);
    }
  }

  function apply() {
    if (!draft) return;
    const wantsTool = new Set(draft.connector_tools);
    onApply({
      name: draft.name,
      instructions: draft.instructions,
      connector_tools: tools.filter((t) => wantsTool.has(t.name)).map((t) => t.name),
      allow_all_azure: draft.allow_all_azure,
      run_mode: draft.run_mode,
    });
  }

  const beforeLen = baseline?.instructions?.length ?? agent.instructions.length;
  const afterLen = draft?.instructions?.length ?? 0;

  return (
    <div className="mb-4 space-y-4 rounded-xl border border-brand/30 bg-brand/5 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-800">
          ✨ Enhance “{agent.name}” with AI
        </div>
        <button onClick={onCancel} className="text-xs text-gray-400 hover:text-gray-600">
          ✕ Close
        </button>
      </div>

      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <span className={stage === "interview" ? "font-semibold text-brand" : ""}>
          1. Assess &amp; interview{stage === "interview" ? ` (Q${step})` : ""}
        </span>
        <span>→</span>
        <span className={stage === "generating" ? "font-semibold text-brand" : ""}>2. Enhance</span>
        <span>→</span>
        <span className={stage === "review" ? "font-semibold text-brand" : ""}>3. Review &amp; confirm</span>
      </div>

      {assessment && stage !== "review" && (
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-600">
          <span className="font-medium text-gray-700">AI assessment:</span> {assessment}
        </div>
      )}

      {stage === "loading" && (
        <div className="flex items-center gap-3 py-6 text-sm text-gray-600">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          Analyzing the current agent and finding improvement opportunities…
        </div>
      )}

      {stage === "interview" && (
        <div className="space-y-4">
          {note && <div className="text-xs text-gray-500">{note}</div>}
          {questions.map((qq) => (
            <div key={qq.id} className="space-y-1.5">
              <div className="text-sm font-medium text-gray-800">{qq.prompt}</div>
              {qq.kind === "text" ? (
                <textarea
                  rows={2}
                  className={input}
                  value={(current[qq.id] as string) ?? ""}
                  onChange={(e) => setAnswer(qq.id, e.target.value)}
                  placeholder="Type your answer…"
                />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {qq.options.map((opt) => {
                    const sel =
                      qq.kind === "multi"
                        ? Array.isArray(current[qq.id]) && (current[qq.id] as string[]).includes(opt)
                        : current[qq.id] === opt;
                    return (
                      <button
                        key={opt}
                        onClick={() => (qq.kind === "multi" ? toggleMulti(qq.id, opt) : setAnswer(qq.id, opt))}
                        className={`rounded-lg border px-2.5 py-1.5 text-sm transition ${
                          sel
                            ? "border-brand bg-brand/10 font-medium text-brand"
                            : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
                        }`}
                      >
                        {qq.kind === "multi" && <span className="mr-1">{sel ? "✓" : "+"}</span>}
                        {opt}
                      </button>
                    );
                  })}
                </div>
              )}
              {qq.allow_custom && qq.kind !== "text" && (
                <input
                  className={input}
                  value={custom[qq.id] ?? ""}
                  onChange={(e) => setCustom((c) => ({ ...c, [qq.id]: e.target.value }))}
                  placeholder="Or add your own…"
                />
              )}
            </div>
          ))}
          {error && <div className="text-xs text-red-600">{error}</div>}
          <div className="flex items-center gap-2">
            <button
              onClick={() => void submitStep()}
              disabled={busy}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-60"
            >
              {busy ? "Thinking…" : "Continue →"}
            </button>
            <button
              onClick={() => void generate(answers)}
              disabled={busy}
              className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm text-brand transition hover:bg-brand/5 disabled:opacity-60"
              title="Skip remaining questions and enhance now"
            >
              Enhance now
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </div>
      )}

      {stage === "generating" && (
        <div className="flex items-center gap-3 py-6 text-sm text-gray-600">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          Rewriting the agent — deepening methodology, output format, and guardrails…
        </div>
      )}

      {stage === "review" && draft && (
        <div className="space-y-3">
          <div className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">
            ✓ {draft.summary || "Enhanced draft ready."}
          </div>
          {draft.changes.length > 0 && (
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="mb-1.5 text-xs font-semibold text-gray-700">What changed</div>
              <ul className="list-disc space-y-0.5 pl-5 text-xs text-gray-600">
                {draft.changes.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3 text-[11px] text-gray-500">
            <span>
              Instructions: <span className="font-mono">{beforeLen}</span> →{" "}
              <span className="font-mono font-semibold text-brand">{afterLen}</span> chars
            </span>
            {draft.run_mode !== (baseline?.run_mode ?? agent.run_mode) && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-700">
                run mode → {draft.run_mode}
              </span>
            )}
            <button
              onClick={() => setShowDiff((v) => !v)}
              className="rounded border border-gray-200 px-2 py-0.5 text-gray-600 hover:bg-gray-50"
            >
              {showDiff ? "Hide" : "Compare before / after"}
            </button>
          </div>
          {showDiff && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <div className="mb-1 text-[11px] font-medium text-gray-500">Before</div>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border bg-gray-50 p-2 text-[11px] text-gray-600">
                  {baseline?.instructions || agent.instructions || "(empty)"}
                </pre>
              </div>
              <div>
                <div className="mb-1 text-[11px] font-medium text-brand">After (enhanced)</div>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-brand/30 bg-white p-2 text-[11px] text-gray-700">
                  {draft.instructions}
                </pre>
              </div>
            </div>
          )}
          <div className="flex items-center gap-2">
            <button
              onClick={apply}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90"
            >
              Review &amp; save →
            </button>
            <button
              onClick={() => void generate(answers)}
              className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm text-brand transition hover:bg-brand/5"
            >
              Regenerate
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
          <p className="text-[11px] text-gray-400">
            “Review &amp; save” opens the agent editor with the enhanced instructions prefilled — nothing is
            saved until you click Save there.
          </p>
        </div>
      )}

      {stage === "error" && (
        <div className="space-y-3">
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error || "The AI could not enhance this agent."}
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => void generate(answers)}
              className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
            >
              Retry
            </button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function AgentForm({
  value,
  tools,
  categories,
  onCancel,
  onSaved,
}: {
  value: Partial<CustomAgent>;
  tools: { name: string; description: string; connector_name: string }[];
  categories: AgentCategory[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Partial<CustomAgent>>(value);
  const [error, setError] = useState("");
  const set = (patch: Partial<CustomAgent>) => setForm((f) => ({ ...f, ...patch }));
  const selected = new Set(form.connector_tools ?? []);

  // Tool filtering — the connector catalog can be large, so let users search by name/
  // description/connector and optionally narrow to a single connector or just the
  // currently-selected tools.
  const [toolQuery, setToolQuery] = useState("");
  const [toolConnector, setToolConnector] = useState("");
  const [showSelectedOnly, setShowSelectedOnly] = useState(false);
  const connectorNames = Array.from(new Set(tools.map((t) => t.connector_name))).sort((a, b) =>
    a.localeCompare(b),
  );
  const q = toolQuery.trim().toLowerCase();
  const filteredTools = tools.filter((t) => {
    if (toolConnector && t.connector_name !== toolConnector) return false;
    if (showSelectedOnly && !selected.has(t.name)) return false;
    if (!q) return true;
    return (
      t.name.toLowerCase().includes(q) ||
      t.description.toLowerCase().includes(q) ||
      t.connector_name.toLowerCase().includes(q)
    );
  });
  // Group the (filtered) tools by connector for a tidier, scannable list.
  const groupedTools = connectorNames
    .map((name) => ({ name, items: filteredTools.filter((t) => t.connector_name === name) }))
    .filter((g) => g.items.length > 0);
  const setMany = (names: string[], on: boolean) => {
    const next = new Set(selected);
    for (const n of names) {
      if (on) next.add(n);
      else next.delete(n);
    }
    set({ connector_tools: [...next] });
  };

  // Provider + model selection. The provider list comes from the LLM config; models
  // are loaded per chosen provider. Empty provider/model = use the global default.
  const cfg = useQuery({ queryKey: ["llmConfig"], queryFn: api.llmConfig });
  const providers = cfg.data
    ? Object.entries(cfg.data.providers)
        .filter(([, p]) => !p.disabled)
        .map(([id]) => id)
    : [];
  const models = useQuery({
    queryKey: ["agentModels", form.provider],
    queryFn: () => api.llmModels(form.provider as string),
    enabled: !!form.provider,
  });
  const modelList = models.data?.models ?? [];

  async function save() {
    if (!form.name?.trim()) {
      setError("Give the agent a name.");
      return;
    }
    setError("");
    try {
      await api.upsertAgent(form);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onCancel}
    >
      <div
        className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">
            {form.id ? "Edit agent" : "New sub agent"}
          </h2>
          <button
            onClick={onCancel}
            className="rounded-lg p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Scrollable body */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-5">
      {!form.id && (form.instructions?.length ?? 0) > 200 && (
        <div className="rounded-lg border border-brand/30 bg-brand/5 px-3 py-2 text-xs text-gray-600">
          ✨ <span className="font-medium text-brand">AI-generated draft.</span> Review the
          name, instructions, tools, and run mode below, edit anything you like, then Save.
        </div>
      )}
      <div>
        <label className={label}>Name</label>
        <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} placeholder="e.g. Health Reporter" />
      </div>
      <div>
        <label className={label}>Category</label>
        <select className={input} value={form.category ?? "general"} onChange={(e) => set({ category: e.target.value })}>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>{c.icon} {c.label}</option>
          ))}
        </select>
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className={label + " mb-0"}>Instructions</label>
          <span className="text-[11px] text-gray-400">{(form.instructions ?? "").length.toLocaleString()} chars</span>
        </div>
        <textarea
          rows={18}
          className={input + " min-h-[40vh] resize-y font-mono text-[12px] leading-relaxed"}
          value={form.instructions ?? ""}
          onChange={(e) => set({ instructions: e.target.value })}
          placeholder="You are a health check reporter. Check Azure resource health and send a summary via email."
        />
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className={label}>Provider</label>
          <select
            className={input}
            value={form.provider ?? ""}
            onChange={(e) => set({ provider: e.target.value, model: "" })}
          >
            <option value="">Default ({cfg.data?.active_provider || "global"})</option>
            {providers.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABELS[p] ?? p}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className={label}>Model</label>
          <select
            className={input}
            value={form.model ?? ""}
            onChange={(e) => set({ model: e.target.value })}
            disabled={!form.provider}
          >
            <option value="">
              {form.provider ? (models.isLoading ? "Loading…" : "Provider default") : "—"}
            </option>
            {modelList.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="-mt-1 text-[11px] text-gray-400">
        Leave provider empty to use the globally active provider/model for this agent.
      </p>
      <div>
        <label className={label}>Run mode</label>
        <div className="flex gap-2">
          {(["review", "autonomous"] as const).map((m) => (
            <button key={m} onClick={() => set({ run_mode: m })} className={`rounded-lg border px-3 py-1.5 text-sm capitalize transition ${form.run_mode === m ? "border-brand bg-brand/5 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>{m}</button>
          ))}
        </div>
        <p className="mt-1 text-[11px] text-gray-400">
          Autonomous executes write actions immediately; Review gates them for approval.
        </p>
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className={label + " mb-0"}>Connector tools</label>
          {tools.length > 0 && (
            <span className="text-[11px] text-gray-400">
              {selected.size} of {tools.length} selected
            </span>
          )}
        </div>
        {tools.length === 0 ? (
          <p className="text-[11px] text-gray-400">No connector tools available — add a connector first.</p>
        ) : (
          <>
            {/* Filter controls */}
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <div className="relative min-w-[10rem] flex-1">
                <svg
                  className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                >
                  <circle cx="9" cy="9" r="6" />
                  <path d="M14 14l3 3" strokeLinecap="round" />
                </svg>
                <input
                  className={input + " pl-7"}
                  value={toolQuery}
                  onChange={(e) => setToolQuery(e.target.value)}
                  placeholder="Search tools…"
                />
              </div>
              <select
                className={input + " w-auto"}
                value={toolConnector}
                onChange={(e) => setToolConnector(e.target.value)}
              >
                <option value="">All connectors</option>
                {connectorNames.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => setShowSelectedOnly((v) => !v)}
                className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${
                  showSelectedOnly
                    ? "border-brand bg-brand/5 font-medium text-brand"
                    : "border-gray-200 text-gray-600 hover:bg-gray-50"
                }`}
              >
                Selected only
              </button>
              {(toolQuery || toolConnector || showSelectedOnly) && (
                <button
                  type="button"
                  onClick={() => {
                    setToolQuery("");
                    setToolConnector("");
                    setShowSelectedOnly(false);
                  }}
                  className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs text-gray-500 transition hover:bg-gray-50"
                >
                  Clear
                </button>
              )}
            </div>
            <div className="max-h-52 space-y-2 overflow-y-auto rounded-lg border bg-white p-2">
              {groupedTools.length === 0 ? (
                <p className="px-1 py-2 text-[11px] text-gray-400">No tools match your filter.</p>
              ) : (
                groupedTools.map((g) => {
                  const groupNames = g.items.map((t) => t.name);
                  const allOn = groupNames.every((n) => selected.has(n));
                  return (
                    <div key={g.name}>
                      <div className="flex items-center justify-between px-1 py-0.5">
                        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                          {g.name}
                        </span>
                        <button
                          type="button"
                          onClick={() => setMany(groupNames, !allOn)}
                          className="text-[11px] text-brand hover:underline"
                        >
                          {allOn ? "Clear" : "Select all"}
                        </button>
                      </div>
                      {g.items.map((t) => (
                        <label key={t.name} className="flex items-start gap-2 rounded px-1 py-0.5 text-sm hover:bg-gray-50">
                          <input
                            type="checkbox"
                            className="mt-1"
                            checked={selected.has(t.name)}
                            onChange={(e) => setMany([t.name], e.target.checked)}
                          />
                          <span className="min-w-0">
                            <span className="font-mono text-xs text-gray-800">{t.name}</span>
                            <span className="block text-[11px] text-gray-500">{t.description}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  );
                })
              )}
            </div>
          </>
        )}
      </div>
      <label className="flex items-center gap-2 text-sm text-gray-600">
        <input type="checkbox" checked={form.allow_all_azure ?? true} onChange={(e) => set({ allow_all_azure: e.target.checked })} />
        Also allow all Azure investigation tools (MCP)
      </label>
      <label className="flex items-center gap-2 text-sm text-gray-600">
        <input type="checkbox" checked={form.allow_all_entra ?? false} onChange={(e) => set({ allow_all_entra: e.target.checked })} />
        Also allow all EntraID (Microsoft Graph) tools (MCP)
      </label>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3">
          {error && <div className="mr-auto text-xs text-red-600">{error}</div>}
          <button onClick={onCancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// Scheduled Tasks
// ===========================================================================
function TasksSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["scheduledTasks"], queryFn: api.scheduledTasks });
  const agentsQ = useQuery({ queryKey: ["customAgents"], queryFn: api.customAgents });
  const archivedQ = useQuery({ queryKey: ["archivedTasks"], queryFn: api.archivedTasks });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const [editing, setEditing] = useState<Partial<ScheduledTask> | null>(null);
  const [openRuns, setOpenRuns] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  // Persistent (dismissible, NOT auto-hiding) confirmation for "Run now", so the user
  // gets a clear acknowledgement and a path to the result instead of a fleeting toast.
  const [runNotice, setRunNotice] = useState<{ id: string; text: string; error?: boolean } | null>(
    null,
  );

  const tasks = q.data?.tasks ?? [];
  const metrics = q.data?.metrics ?? { active: 0, total: 0, total_runs: 0 };
  const agents = agentsQ.data?.agents ?? [];
  const archived = archivedQ.data?.tasks ?? [];
  // Connector id → {name, type} for rendering each schedule's notification methods.
  const connectorById = useMemo(() => {
    const m: Record<string, { name: string; type: string }> = {};
    for (const c of connectorsQ.data?.connectors ?? []) m[c.id] = { name: c.name, type: c.type };
    return m;
  }, [connectorsQ.data]);

  // Group-by-type / filter-by-type / status / search for the unified schedules table.
  const [groupByType, setGroupByType] = useState(true);
  const [typeFilter, setTypeFilter] = useState<"all" | TargetType>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "on" | "off">("all");
  const [search, setSearch] = useState("");

  const visibleTasks = tasks.filter((t) => {
    const tt = (t.target_type ?? "agent") as TargetType;
    if (typeFilter !== "all" && tt !== typeFilter) return false;
    if (statusFilter === "on" && t.status !== "on") return false;
    if (statusFilter === "off" && t.status === "on") return false;
    if (search.trim() && !`${t.name} ${t.target_label ?? ""}`.toLowerCase().includes(search.trim().toLowerCase())) return false;
    return true;
  });
  const typeCounts = tasks.reduce<Record<string, number>>((acc, t) => {
    const tt = t.target_type ?? "agent";
    acc[tt] = (acc[tt] ?? 0) + 1;
    return acc;
  }, {});

  async function act(fn: () => Promise<unknown>) {
    try {
      await fn();
      qc.invalidateQueries({ queryKey: ["scheduledTasks"] });
      qc.invalidateQueries({ queryKey: ["archivedTasks"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  // Trigger a manual run: confirm clearly, auto-open the run history (which polls while
  // running), and surface the resulting thread link there — no auto-hiding message.
  async function runNow(id: string) {
    setRunNotice({ id, text: "Starting…" });
    try {
      const r = await api.runTaskNow(id);
      setOpenRuns(id);
      setRunNotice({ id, text: r.message || "Task started. Watch its progress below." });
      qc.invalidateQueries({ queryKey: ["scheduledTasks"] });
      qc.invalidateQueries({ queryKey: ["taskRuns", id] });
    } catch (e) {
      setRunNotice({ id, text: formatError(e), error: true });
    }
  }

  function renderTaskRow(t: ScheduledTask) {
    const tt = (t.target_type ?? "agent") as TargetType;
    return (
      <Fragment key={t.id}>
        <tr className="border-b last:border-0 hover:bg-gray-50">
          <td className="py-2 pr-3 font-medium text-gray-800">
            {t.name}
            {t.target_label && <div className="truncate text-[11px] font-normal text-gray-400" title={t.target_label}>{t.target_label}</div>}
          </td>
          <td className="py-2 pr-3">
            <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">
              {TARGET_META[tt].icon} {TARGET_META[tt].label}
            </span>
          </td>
          <td className="py-2 pr-3">
            <div className="flex items-center gap-2">
              <button
                type="button"
                role="switch"
                aria-checked={t.status === "on"}
                title={t.status === "on" ? "Disable schedule" : "Enable schedule"}
                onClick={() => act(() => api.toggleTask(t.id))}
                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${t.status === "on" ? "bg-green-500" : "bg-gray-300"}`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${t.status === "on" ? "translate-x-[18px]" : "translate-x-0.5"}`} />
              </button>
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${statusClass(t.status)}`}>{statusLabel(t.status)}</span>
            </div>
          </td>
          <td className="py-2 pr-3 text-gray-600">{t.schedule_label}</td>
          <td className="py-2 pr-3"><NotifyMethods task={t} connectorById={connectorById} /></td>
          <td className="py-2 pr-3 text-gray-500">{t.last_run_at ? formatTimestamp(t.last_run_at) : "—"}</td>
          <td className="py-2 pr-3 text-gray-500">
            {t.next_run_at ? (
              <div className="flex flex-col">
                <span>{formatTimestamp(t.next_run_at)}</span>
                <span className="text-[11px] text-gray-400">{formatRelativeFromNow(t.next_run_at)}</span>
              </div>
            ) : (
              "\u2014"
            )}
          </td>
          <td className="py-2 pr-3 text-gray-500">{t.completed_runs}</td>
          <td className="py-2 text-right">
            <div className="flex justify-end gap-1 text-xs">
              <button onClick={() => runNow(t.id)} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Run now</button>
              <button onClick={() => setOpenRuns(openRuns === t.id ? null : t.id)} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">History</button>
              <button onClick={() => setEditing(t)} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Edit</button>
              <button onClick={() => { if (confirm("Archive this schedule? It stops running but its run history is preserved. You can restore or permanently delete it later.")) act(() => api.deleteTask(t.id)); }} className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50">Delete</button>
            </div>
          </td>
        </tr>
        {(openRuns === t.id || runNotice?.id === t.id) && (
          <tr key={`${t.id}-runs`}>
            <td colSpan={9} className="bg-gray-50 px-3 py-2">
              {runNotice?.id === t.id && (
                <div
                  className={`mb-2 flex items-start justify-between gap-3 rounded-md border px-3 py-2 text-xs ${
                    runNotice.error
                      ? "border-red-200 bg-red-50 text-red-700"
                      : "border-brand/30 bg-brand/5 text-gray-700"
                  }`}
                >
                  <span>{runNotice.error ? "✗ " : "▶ "}{runNotice.text}</span>
                  <button
                    onClick={() => setRunNotice(null)}
                    className="shrink-0 text-gray-400 hover:text-gray-600"
                    title="Dismiss"
                  >
                    ✕
                  </button>
                </div>
              )}
              <TaskRuns taskId={t.id} />
            </td>
          </tr>
        )}
      </Fragment>
    );
  }

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        {[
          ["Active tasks", metrics.active],
          ["Total tasks", metrics.total],
          ["Total runs", metrics.total_runs],
        ].map(([k, v]) => (
          <div key={k as string} className="rounded-lg border bg-white p-4 text-center shadow-sm">
            <div className="text-2xl font-semibold text-gray-800">{v as number}</div>
            <div className="text-xs text-gray-500">{k as string}</div>
          </div>
        ))}
      </div>

      <Card
        title="Schedules"
        action={
          !editing && (
            <button onClick={() => setEditing({ name: "", target_type: "agent", target_config: {}, instructions: "", schedule_kind: "daily", time_of_day: "08:00", timezone: "UTC", run_mode: "review", message_grouping: "new_thread", status: "on" })} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90">
              + New schedule
            </button>
          )
        }
      >
        <p className="mb-3 text-xs text-gray-500">
          One place for every recurring job — Sub Agents, Assessments, Workbooks, and Playbooks — on a shared cadence (daily / weekly / cron).
        </p>
        {msg && <div className="mb-2 text-xs text-red-600">{msg}</div>}

        {tasks.length > 0 && !editing && (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="inline-flex overflow-hidden rounded-md border text-xs">
              <button onClick={() => setTypeFilter("all")} className={`px-2.5 py-1 ${typeFilter === "all" ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>All ({tasks.length})</button>
              {TARGET_TYPES.map((tt) => (
                <button key={tt} onClick={() => setTypeFilter(tt)} className={`border-l px-2.5 py-1 ${typeFilter === tt ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>
                  {TARGET_META[tt].icon} {TARGET_META[tt].label} ({typeCounts[tt] ?? 0})
                </button>
              ))}
            </div>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as "all" | "on" | "off")} className="rounded-md border px-2 py-1 text-xs">
              <option value="all">All statuses</option>
              <option value="on">Enabled</option>
              <option value="off">Paused</option>
            </select>
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search…" className="w-40 rounded-md border px-2 py-1 text-xs" />
            <label className="ml-auto flex items-center gap-1.5 text-xs text-gray-500">
              <span>Group by type</span>
              <button type="button" role="switch" aria-checked={groupByType} onClick={() => setGroupByType((v) => !v)}
                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${groupByType ? "bg-brand" : "bg-gray-300"}`}>
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${groupByType ? "translate-x-[18px]" : "translate-x-0.5"}`} />
              </button>
            </label>
          </div>
        )}

        {q.isLoading && <div className="h-16 animate-pulse rounded-lg border bg-gray-100" />}
        {!q.isLoading && tasks.length === 0 && !editing && (
          <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-sm text-gray-500">No schedules yet. Create one to run an agent, assessment, workbook, or playbook on a cadence.</div>
        )}

        {visibleTasks.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-gray-500">
                <tr className="border-b">
                  <th className="py-1.5 pr-3 font-medium">Name</th>
                  <th className="py-1.5 pr-3 font-medium">Type</th>
                  <th className="py-1.5 pr-3 font-medium">Status</th>
                  <th className="py-1.5 pr-3 font-medium">Schedule</th>
                  <th className="py-1.5 pr-3 font-medium">Notify</th>
                  <th className="py-1.5 pr-3 font-medium">Last run</th>
                  <th className="py-1.5 pr-3 font-medium">Next run</th>
                  <th className="py-1.5 pr-3 font-medium">Runs</th>
                  <th className="py-1.5 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {(groupByType
                  ? TARGET_TYPES.flatMap((tt) => {
                      const group = visibleTasks.filter((t) => (t.target_type ?? "agent") === tt);
                      if (group.length === 0) return [];
                      return [
                        <tr key={`grp-${tt}`} className="bg-gray-50/70">
                          <td colSpan={9} className="py-1.5 pl-1 text-xs font-semibold text-gray-600">
                            {TARGET_META[tt].icon} {TARGET_META[tt].label} <span className="font-normal text-gray-400">({group.length})</span>
                          </td>
                        </tr>,
                        ...group.map((t) => renderTaskRow(t)),
                      ];
                    })
                  : visibleTasks.map((t) => renderTaskRow(t)))}
              </tbody>
            </table>
          </div>
        )}

        {editing && (
          <TaskForm
            value={editing}
            agents={agents}
            onCancel={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              qc.invalidateQueries({ queryKey: ["scheduledTasks"] });
            }}
          />
        )}
      </Card>

      {archived.length > 0 && (
        <Card title={`Archived schedules (${archived.length})`}>
          <p className="mb-3 text-xs text-gray-500">
            Deleted schedules don't run, but their run history is preserved. Restore one to bring it back (paused), or permanently delete it to also remove its history.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-gray-500">
                <tr className="border-b">
                  <th className="py-1.5 pr-3 font-medium">Name</th>
                  <th className="py-1.5 pr-3 font-medium">Schedule</th>
                  <th className="py-1.5 pr-3 font-medium">Deleted</th>
                  <th className="py-1.5 pr-3 font-medium">Runs</th>
                  <th className="py-1.5 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {archived.map((t) => (
                  <Fragment key={t.id}>
                    <tr className="border-b last:border-0 hover:bg-gray-50">
                      <td className="py-2 pr-3 font-medium text-gray-700">{t.name}</td>
                      <td className="py-2 pr-3 text-gray-500">{t.schedule_label}</td>
                      <td className="py-2 pr-3 text-gray-500">{t.deleted_at ? formatTimestamp(t.deleted_at) : "—"}</td>
                      <td className="py-2 pr-3 text-gray-500">{t.run_count ?? 0}</td>
                      <td className="py-2 text-right">
                        <div className="flex justify-end gap-1 text-xs">
                          <button onClick={() => setOpenRuns(openRuns === t.id ? null : t.id)} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">History</button>
                          <button onClick={() => act(() => api.restoreTask(t.id))} className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50">Restore</button>
                          <button onClick={() => { if (confirm("Permanently delete this schedule AND its run history? This cannot be undone.")) act(() => api.purgeTask(t.id)); }} className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50">Delete permanently</button>
                        </div>
                      </td>
                    </tr>
                    {openRuns === t.id && (
                      <tr key={`${t.id}-runs`}>
                        <td colSpan={5} className="bg-gray-50 px-3 py-2">
                          <TaskRuns taskId={t.id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

function statusClass(status: string): string {
  if (status === "on") return "bg-green-100 text-green-700";
  if (status === "failed") return "bg-red-100 text-red-700";
  if (status === "ended") return "bg-gray-200 text-gray-600";
  return "bg-gray-100 text-gray-500";
}

function statusLabel(status: string): string {
  if (status === "on") return "enabled";
  if (status === "off") return "disabled";
  return status;
}

/** Renders the notification methods a schedule delivers its result through. Every run is
 *  always published to the in-app notification center; selected connectors (Slack / Teams /
 *  email / Jira / ServiceNow / webhook …) get the result summary too. */
function NotifyMethods({ task, connectorById }: { task: ScheduledTask; connectorById: Record<string, { name: string; type: string }> }) {
  const ids = task.notify_connector_ids ?? [];
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span
        className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600"
        title="Every scheduled run is delivered to the in-app notification center"
      >
        🔔 In-app
      </span>
      {ids.map((id) => {
        const c = connectorById[id];
        return (
          <span
            key={id}
            className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-white px-1.5 py-0.5 text-[10px] text-gray-600"
            title={c ? `Delivered to ${c.name} (${c.type})` : "Delivered to a connector"}
          >
            <BrandIcon type={c?.type ?? "webhook"} className="h-3 w-3" />
            {c?.name ?? "Connector"}
          </span>
        );
      })}
    </div>
  );
}

function TaskRuns({ taskId }: { taskId: string }) {
  const q = useQuery({
    queryKey: ["taskRuns", taskId],
    queryFn: () => api.taskRuns(taskId),
    // Poll while any run is still in progress so the user watches it go
    // running → succeeded/failed (and the "Open thread →" link appears) live.
    refetchInterval: (query) => {
      const runs = query.state.data?.runs ?? [];
      return runs.some((r: TaskRunInfo) => r.status === "running") ? 3000 : false;
    },
  });
  const runs = q.data?.runs ?? [];
  if (q.isLoading) return <div className="text-xs text-gray-400">Loading runs…</div>;
  if (runs.length === 0) return <div className="text-xs text-gray-400">No runs yet.</div>;
  return (
    <div className="space-y-1">
      {runs.map((r: TaskRunInfo) => (
        <div key={r.id} className="flex items-center gap-3 text-xs">
          <span className={`rounded px-1.5 py-0.5 ${statusClass(r.status === "succeeded" ? "on" : r.status === "failed" ? "failed" : "")}`}>{r.status}</span>
          <span className="text-gray-400">{formatTimestamp(r.started_at)}</span>
          <span className="text-gray-400">{r.trigger}</span>
          <span className="min-w-0 flex-1 truncate text-gray-600">{r.error || r.summary || ""}</span>
          {resultLink(r)}
        </div>
      ))}
    </div>
  );
}

/** Deep-link to the artifact a task run produced, routed by target/result_ref. */
function resultLink(r: TaskRunInfo) {
  const ref = (r.result_ref ?? {}) as { kind?: string; id?: string };
  if (r.thread_id && (r.target_type ?? "agent") === "agent") {
    return <Link to={`/c/${r.thread_id}`} className="shrink-0 text-brand hover:underline">Open thread →</Link>;
  }
  if (ref.kind === "assessment_run" && ref.id) {
    return <Link to={`/assessments/${ref.id}`} className="shrink-0 text-brand hover:underline">Open report →</Link>;
  }
  if (ref.kind === "assessment_runs") {
    return <Link to="/assessments" className="shrink-0 text-brand hover:underline">Open reports →</Link>;
  }
  if (ref.kind === "workbook_run") {
    return <Link to="/automations/workbooks" className="shrink-0 text-brand hover:underline">Open workbook →</Link>;
  }
  if (ref.kind === "playbook_run") {
    return <Link to="/automations/playbooks" className="shrink-0 text-brand hover:underline">Open playbook →</Link>;
  }
  return null;
}

function TaskForm({
  value,
  agents,
  onCancel,
  onSaved,
}: {
  value: Partial<ScheduledTask>;
  agents: CustomAgent[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Partial<ScheduledTask>>(value);
  const [error, setError] = useState("");
  const set = (patch: Partial<ScheduledTask>) => setForm((f) => ({ ...f, ...patch }));
  const targetType = (form.target_type ?? "agent") as TargetType;
  const cfg = (form.target_config ?? {}) as Record<string, unknown>;
  const setCfg = (patch: Record<string, unknown>) => set({ target_config: { ...cfg, ...patch } });

  // Enabled connectors available as notification targets for this task.
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const notifyConnectors = (connectorsQ.data?.connectors ?? []).filter((c) => !c.disabled);
  const selectedNotify = form.notify_connector_ids ?? [];
  const toggleNotify = (id: string) =>
    set({
      notify_connector_ids: selectedNotify.includes(id)
        ? selectedNotify.filter((x) => x !== id)
        : [...selectedNotify, id],
    });

  // Data for the per-type config sub-forms.
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, enabled: targetType === "assessment" });
  const workbooksQ = useQuery({ queryKey: ["workbooks"], queryFn: api.workbooks, enabled: targetType === "workbook" });
  const playbooksQ = useQuery({ queryKey: ["playbooks"], queryFn: api.playbooks, enabled: targetType === "playbook" });
  const workloads = workloadsQ.data?.workloads ?? [];
  const workbooks = workbooksQ.data?.workbooks ?? [];
  const playbooks = playbooksQ.data?.playbooks ?? [];
  const selectedWorkbook = workbooks.find((w) => w.id === cfg.workbook_id);

  async function save() {
    if (!form.name?.trim()) {
      setError("Give the schedule a name.");
      return;
    }
    if (targetType === "agent" && !form.instructions?.trim()) {
      setError("Describe what the agent should do.");
      return;
    }
    if (targetType === "assessment" && !((cfg.workload_ids as string[] | undefined)?.length)) {
      setError("Select at least one workload to assess.");
      return;
    }
    if (targetType === "assessment" && !((cfg.pillars as string[] | undefined)?.length) && !cfg.pack) {
      setError("Select an assessment pack or at least one pillar.");
      return;
    }
    if (targetType === "workbook" && !cfg.workbook_id) {
      setError("Select a workbook to run.");
      return;
    }
    if (targetType === "playbook" && !cfg.playbook_id) {
      setError("Select a playbook to run.");
      return;
    }
    setError("");
    try {
      // Agent schedules require non-empty instructions server-side; supply a default.
      const payload: Partial<ScheduledTask> = { ...form };
      if (targetType !== "agent" && !payload.instructions?.trim()) payload.instructions = form.name;
      await api.upsertTask(payload);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  const cfgWorkloadIds = (cfg.workload_ids as string[] | undefined) ?? [];
  const cfgPillars = (cfg.pillars as string[] | undefined) ?? ["security", "reliability"];
  const cfgPack = (cfg.pack as string | undefined) ?? "";

  return (
    <div className="mt-4 space-y-3 rounded-lg border border-brand/30 bg-brand/5 p-4">
      <div className="text-sm font-medium text-gray-800">{form.id ? "Edit schedule" : "New schedule"}</div>

      {/* Target type picker */}
      <div>
        <label className={label}>What should this schedule run?</label>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {TARGET_TYPES.map((tt) => {
            const on = targetType === tt;
            return (
              <button
                key={tt}
                type="button"
                onClick={() => set({ target_type: tt, target_config: tt === targetType ? cfg : (tt === "assessment" ? { pillars: ["security", "reliability"], use_ai: true, alert_on_new_findings: true, alert_min_severity: "warning", workload_ids: [] } : {}) })}
                className={`rounded-lg border p-2.5 text-left transition ${on ? "border-brand bg-brand/10" : "border-gray-200 bg-white hover:bg-gray-50"}`}
              >
                <div className="text-sm font-medium text-gray-800">{TARGET_META[tt].icon} {TARGET_META[tt].label}</div>
                <div className="mt-0.5 text-[11px] text-gray-500">{TARGET_META[tt].blurb}</div>
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <label className={label}>Schedule name</label>
        <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} placeholder="daily-health-report" />
      </div>

      {/* --- Per-type configuration --- */}
      {targetType === "agent" && (
        <>
          <div>
            <label className={label}>Sub agent</label>
            <select className={input} value={form.agent_id ?? ""} onChange={(e) => set({ agent_id: e.target.value || null })}>
              <option value="">— None (use task prompt only) —</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={label}>Task details (what the agent should do)</label>
            <textarea rows={3} className={input} value={form.instructions ?? ""} onChange={(e) => set({ instructions: e.target.value })} placeholder="Check the health of resources in my resource group, verify all apps are running, summarize findings and send the report." />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className={label}>Run mode</label>
              <select className={input} value={form.run_mode ?? "review"} onChange={(e) => set({ run_mode: e.target.value })}>
                <option value="review">Review (gate writes)</option>
                <option value="autonomous">Autonomous</option>
              </select>
            </div>
            <div>
              <label className={label}>Message grouping</label>
              <select className={input} value={form.message_grouping ?? "new_thread"} onChange={(e) => set({ message_grouping: e.target.value })}>
                <option value="new_thread">New thread per run</option>
                <option value="same_thread">Same thread</option>
              </select>
            </div>
          </div>
        </>
      )}

      {targetType === "assessment" && (
        <>
          <div>
            <label className={label}>Workloads {cfgWorkloadIds.length > 0 && <span className="text-brand">({cfgWorkloadIds.length})</span>}</label>
            <div className="max-h-40 space-y-0.5 overflow-y-auto rounded-lg border bg-white p-1.5">
              {workloads.length === 0 && <div className="px-2 py-2 text-xs text-gray-400">{workloadsQ.isLoading ? "Loading…" : "No workloads."}</div>}
              {workloads.map((w) => (
                <label key={w.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-gray-50">
                  <input type="checkbox" checked={cfgWorkloadIds.includes(w.id)} onChange={() => setCfg({ workload_ids: cfgWorkloadIds.includes(w.id) ? cfgWorkloadIds.filter((x) => x !== w.id) : [...cfgWorkloadIds, w.id] })} />
                  <span className="truncate text-gray-700">{w.name}</span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <label className={label}>Assessment pack</label>
            <div className="mb-2 flex flex-wrap gap-1.5">
              {ASSESSMENT_PACKS.map((pk) => (
                <button key={pk.id} type="button" title={pk.label}
                  onClick={() => setCfg({ pack: pk.id, pillars: pk.pillars })}
                  className={`rounded-lg border px-2.5 py-1 text-xs ${cfgPack === pk.id ? "border-brand bg-brand font-medium text-white" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>
                  {pk.icon} {pk.short}
                </button>
              ))}
              <span className="self-center text-[11px] text-gray-400">
                {cfgPack ? ASSESSMENT_PACKS.find((p) => p.id === cfgPack)?.label : "Custom pillars"}
              </span>
            </div>
            <label className={label}>Pillars</label>
            <div className="flex flex-wrap gap-1.5">
              {ASSESSMENT_PILLARS.map((p) => {
                const on = cfgPillars.includes(p.id);
                return (
                  <button key={p.id} type="button" onClick={() => setCfg({ pack: "", pillars: on ? cfgPillars.filter((x) => x !== p.id) : [...cfgPillars, p.id] })}
                    className={`rounded-lg border px-2.5 py-1 text-xs ${on ? "border-brand bg-brand/10 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>{p.label}</button>
                );
              })}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={cfg.use_ai !== false} onChange={(e) => setCfg({ use_ai: e.target.checked })} />AI executive summary</label>
            <label className="flex items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={cfg.alert_on_new_findings !== false} onChange={(e) => setCfg({ alert_on_new_findings: e.target.checked })} />Alert on new findings ≥
              <select value={(cfg.alert_min_severity as string) ?? "warning"} onChange={(e) => setCfg({ alert_min_severity: e.target.value })} className="rounded border px-1.5 py-0.5 text-[11px]"><option value="warning">Warning</option><option value="error">Error</option><option value="critical">Critical</option></select>
            </label>
          </div>
          <label className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
            <input type="checkbox" checked={!!cfg.alert_on_low_confidence} onChange={(e) => setCfg({ alert_on_low_confidence: e.target.checked })} />
            Alert when result confidence is low (&lt;
            <select value={String((cfg.min_completeness_pct as number) ?? 98)} onChange={(e) => setCfg({ min_completeness_pct: Number(e.target.value) })} className="rounded border px-1.5 py-0.5 text-[11px]">
              <option value="98">98%</option><option value="90">90%</option><option value="80">80%</option><option value="70">70%</option>
            </select>
            controls evaluated)
          </label>
        </>
      )}

      {targetType === "workbook" && (
        <>
          <div>
            <label className={label}>Workbook</label>
            <select className={input} value={(cfg.workbook_id as string) ?? ""} onChange={(e) => setCfg({ workbook_id: e.target.value, params: {} })}>
              <option value="">{workbooksQ.isLoading ? "Loading…" : "Select a workbook…"}</option>
              {workbooks.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          </div>
          {selectedWorkbook && (selectedWorkbook.params ?? []).length > 0 && (
            <div className="space-y-2">
              <label className={label}>Parameters</label>
              {(selectedWorkbook.params ?? []).map((p) => (
                <div key={p.key}>
                  <span className="mb-0.5 block text-[11px] text-gray-500">{p.label || p.key}{p.required && <span className="text-red-500"> *</span>}</span>
                  <input className={input} value={(((cfg.params as Record<string, string>) ?? {})[p.key]) ?? String(p.default ?? "")}
                    onChange={(e) => setCfg({ params: { ...((cfg.params as Record<string, string>) ?? {}), [p.key]: e.target.value } })} placeholder={p.help} />
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {targetType === "playbook" && (
        <div>
          <label className={label}>Playbook</label>
          <select className={input} value={(cfg.playbook_id as string) ?? ""} onChange={(e) => setCfg({ playbook_id: e.target.value })}>
            <option value="">{playbooksQ.isLoading ? "Loading…" : "Select a playbook…"}</option>
            {playbooks.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
      )}

      {/* --- Shared cadence --- */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <label className={label}>Frequency</label>
          <select className={input} value={form.schedule_kind ?? "daily"} onChange={(e) => set({ schedule_kind: e.target.value as ScheduledTask["schedule_kind"] })}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="cron">Custom (cron)</option>
          </select>
        </div>
        {form.schedule_kind !== "cron" ? (
          <div>
            <label className={label}>Time of day</label>
            <input type="time" className={input} value={form.time_of_day ?? "08:00"} onChange={(e) => set({ time_of_day: e.target.value })} />
          </div>
        ) : (
          <div className="sm:col-span-2">
            <label className={label}>Cron expression</label>
            <input className={`${input} font-mono`} value={form.cron_expr ?? ""} onChange={(e) => set({ cron_expr: e.target.value })} placeholder="0 8 * * *" />
          </div>
        )}
        {form.schedule_kind === "weekly" && (
          <div>
            <label className={label}>Weekday</label>
            <select className={input} value={form.weekday ?? 0} onChange={(e) => set({ weekday: Number(e.target.value) })}>
              {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => (
                <option key={d} value={i}>{d}</option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label className={label}>Timezone</label>
          <select className={input} value={form.timezone ?? "UTC"} onChange={(e) => set({ timezone: e.target.value })}>
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
        </div>
        <div>
          <label className={label}>Run limit (optional)</label>
          <input type="number" className={input} value={form.max_runs ?? ""} onChange={(e) => set({ max_runs: e.target.value ? Number(e.target.value) : null })} placeholder="∞" />
        </div>
      </div>

      <div>
        <label className={label}>Notify these connectors with the result</label>
        {notifyConnectors.length === 0 ? (
          <p className="text-xs text-gray-400">
            No connectors configured.{" "}
            <Link to="/admin/connectors" className="text-brand hover:underline">Add one</Link> to deliver results.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {notifyConnectors.map((c) => {
              const on = selectedNotify.includes(c.id);
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => toggleNotify(c.id)}
                  className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                    on ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  <BrandIcon type={c.type} className="h-3.5 w-3.5" />
                  {c.name}
                </button>
              );
            })}
          </div>
        )}
        <p className="mt-1 text-[11px] text-gray-400">
          After each run, the result summary is delivered to the selected connectors.
        </p>
      </div>
      <label className="flex items-center gap-2 text-sm text-gray-700">
        <button
          type="button"
          role="switch"
          aria-checked={(form.status ?? "on") === "on"}
          onClick={() => set({ status: (form.status ?? "on") === "on" ? "off" : "on" })}
          className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${(form.status ?? "on") === "on" ? "bg-green-500" : "bg-gray-300"}`}
        >
          <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${(form.status ?? "on") === "on" ? "translate-x-[18px]" : "translate-x-0.5"}`} />
        </button>
        <span className="font-medium">Schedule enabled</span>
        <span className="text-xs text-gray-400">{(form.status ?? "on") === "on" ? "Runs automatically on its schedule" : "Paused — won't run until enabled"}</span>
      </label>
      {error && <div className="text-xs text-red-600">{error}</div>}
      <div className="flex gap-2">
        <button onClick={() => void save()} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90">{form.id ? "Save schedule" : "Create schedule"}</button>
        <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
      </div>
    </div>
  );
}
