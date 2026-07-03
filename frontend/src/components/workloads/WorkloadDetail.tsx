// Workload detail page (/workloads/:id) — the deep dive that unifies every per-workload
// analyzer the product already has. Cache-first: the page renders instantly from the
// command-center profile; the "Analyze" button fans out to the existing per-feature refresh
// endpoints (on-demand, never automatic) and then re-reads the profile. Deep-links scope the
// heavy analyzers (Performance, Policy, RBAC, Change Explorer, …) to this workload.
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Workload, type WorkloadProfile } from "../../api";
import { AzureIcon } from "../AzureIcon";
import { WorkloadForm } from "../WorkloadsView";
import {
  CompositionDonut,
  HealthRadar,
  ScoreBadge,
  MetricBar,
  ClassPills,
  categoryColor,
  bandBg,
  Sparkline,
  bandColor,
} from "./viz";

type Tab = "overview" | "resources";

// The workload detail page is intentionally two tabs: a single scrollable "Overview"
// that stacks the at-a-glance, health & coverage, watchers, security and lifecycle
// sections (each is short on its own), and a dedicated "Resources" table.
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "📊 Overview" },
  { id: "resources", label: "📦 Resources" },
];

const SIGNAL_LABELS: Record<string, string> = {
  monitoring: "Monitoring", telemetry: "Telemetry", backupdr: "Backup / DR",
  performance: "Performance", ownership: "Ownership", policy: "Policy", tags: "Tags",
};

export function WorkloadDetailPanel() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("overview");
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState("");
  const [editing, setEditing] = useState(false);

  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workload: Workload | undefined = wlQ.data?.workloads.find((w) => w.id === id);
  const profQ = useQuery({ queryKey: ["workloadProfile", id], queryFn: () => api.workloadProfile(id), enabled: !!id });
  const profile: WorkloadProfile | undefined = profQ.data?.profile;
  // The workload's architecture (if one has been built) — so the Architecture button opens it
  // directly instead of the list.
  const archQ = useQuery({ queryKey: ["architectures"], queryFn: api.architectures });
  const workloadArch = archQ.data?.architectures.find((a) => a.workload_id === id);

  const openArchitecture = () => {
    if (workloadArch) navigate(`/architectures/${workloadArch.id}`);
    else navigate(`/architectures?workload_id=${encodeURIComponent(id)}`);
  };

  // Overview is one scrollable page; "next best action" chips jump to the right section.
  const scrollToSection = (sectionId: string) => {
    if (typeof document !== "undefined") {
      document.getElementById(`wl-section-${sectionId}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  async function analyzeAll() {
    if (!workload) return;
    setAnalyzing(true);
    setAnalyzeMsg("Analyzing — scanning monitoring, telemetry, backup/DR, ownership and retirements…");
    const cid = workload.connection_id || "";
    const tasks: Promise<unknown>[] = [
      api.refreshAmba({ workload_id: id, connection_id: cid }),
      api.refreshTelemetry({ workload_id: id, connection_id: cid }),
      api.refreshBackupDr({ workload_id: id, connection_id: cid }),
      api.refreshRadar({ workload_id: id, connection_id: cid }),
      api.refreshOwnershipCoverage("workload", id, "", cid),
    ];
    const results = await Promise.allSettled(tasks);
    const failed = results.filter((r) => r.status === "rejected").length;
    // Record a composite-score trend point now the caches are warm (best-effort).
    try { await api.recordWorkloadTrend(id); } catch { /* ignore */ }
    setAnalyzing(false);
    setAnalyzeMsg(failed ? `Analysis finished with ${failed} signal(s) unavailable.` : "Analysis complete.");
    qc.invalidateQueries({ queryKey: ["workloadProfile", id] });
    qc.invalidateQueries({ queryKey: ["workloadProfiles"] });
    setTimeout(() => setAnalyzeMsg(""), 6000);
  }

  if (wlQ.isLoading) return <div className="p-8 text-sm text-gray-500">Loading…</div>;
  if (!workload) {
    return (
      <div className="p-8">
        <button onClick={() => navigate("/workloads")} className="text-sm text-brand hover:underline">← Back to workloads</button>
        <div className="mt-6 rounded-xl border border-dashed p-10 text-center text-sm text-gray-500">Workload not found.</div>
      </div>
    );
  }

  const health = profile?.health;
  const comp = profile?.composition;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="border-b bg-white px-6 py-4">
        <button onClick={() => navigate("/workloads")} className="text-xs text-gray-400 hover:text-gray-600">← Workloads</button>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <ScoreBadge score={health?.score ?? null} band={health?.band ?? "unknown"} size="lg" />
            <div>
              <h1 className="text-xl font-semibold text-gray-900">{workload.name}</h1>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                {profile && <ClassPills c={profile.classification} />}
                <span className="text-xs text-gray-400">{comp?.total ?? workload.summary?.total_resources ?? 0} resources</span>
              </div>
              {workload.description && <p className="mt-1 max-w-2xl text-sm text-gray-500">{workload.description}</p>}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={analyzeAll} disabled={analyzing} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-dark disabled:opacity-60">
              {analyzing ? "Analyzing…" : "⚡ Analyze workload"}
            </button>
            <button onClick={openArchitecture} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">📐 Architecture</button>
            <button onClick={() => navigate(`/assessments?workload_id=${encodeURIComponent(id)}`)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">✓ Assess</button>
            <button onClick={() => navigate(`/mission-control/${id}`)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">🚀 Mission</button>
            <button onClick={() => navigate(`/graph?workload_id=${encodeURIComponent(id)}`)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">🕸️ Graph</button>
            <button onClick={() => setEditing(true)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50" title="Edit name, description, classification and resources">✏️ Edit</button>
          </div>
        </div>
        {analyzeMsg && <div className="mt-2 rounded-lg bg-brand/5 px-3 py-1.5 text-xs text-brand">{analyzeMsg}</div>}
        <nav className="mt-3 flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)} className={`rounded-lg px-3 py-1.5 text-sm transition ${tab === t.id ? "bg-brand text-white" : "text-gray-600 hover:bg-gray-100"}`}>
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto bg-gray-50 p-6">
        {tab === "overview" && (
          <div className="space-y-6">
            <Section id="glance" title="📊 At a glance">
              <OverviewTab workload={workload} profile={profile} onAnalyze={analyzeAll} onGoSection={scrollToSection} />
            </Section>
            <Section id="coverage" title="🩺 Health & Coverage">
              <CoverageTab id={id} profile={profile} navigate={navigate} />
            </Section>
            <Section id="watchers" title="🔭 Watchers">
              <WatchersTab id={id} navigate={navigate} />
            </Section>
            <Section id="security" title="🛡️ Security & Governance">
              <DeepLinkTab id={id} navigate={navigate} kind="security" />
            </Section>
            <Section id="lifecycle" title="🛰️ Lifecycle">
              <DeepLinkTab id={id} navigate={navigate} kind="lifecycle" profile={profile} />
            </Section>
          </div>
        )}
        {tab === "resources" && <ResourcesTab workload={workload} profile={profile} />}
      </div>

      {editing && (
        <WorkloadForm
          value={workload}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            qc.invalidateQueries({ queryKey: ["workloads"] });
            qc.invalidateQueries({ queryKey: ["workloadProfile", id] });
            qc.invalidateQueries({ queryKey: ["workloadProfiles"] });
          }}
        />
      )}
    </div>
  );
}

// ---- Section wrapper (single-page Overview) -------------------------------------
function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={`wl-section-${id}`} className="scroll-mt-4">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">{title}</h2>
      {children}
    </section>
  );
}

// ---- Overview -------------------------------------------------------------------
function OverviewTab({ profile, onAnalyze, onGoSection }: { workload: Workload; profile?: WorkloadProfile; onAnalyze: () => void; onGoSection: (id: string) => void }) {
  const comp = profile?.composition;
  const health = profile?.health;
  const nextActions = useMemo(() => {
    const out: { label: string; section: string }[] = [];
    if (!health) return out;
    const add = (sig: string, label: string, section: string) => {
      const v = (health as unknown as Record<string, number | null>)[sig];
      if (v == null) out.push({ label: `${SIGNAL_LABELS[sig]} not analyzed — run Analyze`, section });
      else if (v < 50) out.push({ label, section });
    };
    add("backupdr", "Backup/DR coverage is low — generate protection runbooks", "coverage");
    add("monitoring", "Monitoring coverage is low — close alert gaps", "coverage");
    add("ownership", "Under-owned — assign accountable owners", "security");
    return out.slice(0, 4);
  }, [health]);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {/* Composition */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Composition</div>
        <div className="flex items-center gap-4">
          <CompositionDonut data={comp?.by_category ?? []} size={120} centerLabel={String(comp?.total ?? 0)} centerSub="resources" />
          <div className="flex-1 space-y-1">
            {(comp?.by_category ?? []).map((c) => (
              <div key={c.category} className="flex items-center gap-2 text-xs">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: categoryColor(c.category) }} />
                <span className="text-gray-600">{c.category}</span>
                <span className="ml-auto tabular-nums font-medium text-gray-700">{c.count}</span>
              </div>
            ))}
            {(comp?.by_category.length ?? 0) === 0 && <div className="text-xs text-gray-400">No resources yet</div>}
          </div>
        </div>
      </div>

      {/* Health */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">Health</span>
          <div className="flex items-center gap-2">
            {(profile?.score_trend?.points?.length ?? 0) > 1 && (
              <span title={`Score trend (${profile!.score_trend.count} points)`}>
                <Sparkline points={profile!.score_trend.points} width={56} height={16} color={bandColor(health?.band ?? "unknown")} />
              </span>
            )}
            <ScoreBadge score={health?.score ?? null} band={health?.band ?? "unknown"} size="sm" />
          </div>
        </div>
        <div className="flex items-center gap-3">
          {health && <HealthRadar health={health} size={130} />}
          <div className="flex-1 space-y-1.5">
            {["monitoring", "telemetry", "backupdr", "performance", "ownership"].map((s) => (
              <MetricBar key={s} label={SIGNAL_LABELS[s]} value={(health as unknown as Record<string, number | null> | undefined)?.[s] ?? null} />
            ))}
          </div>
        </div>
        {!profile?.analyzed && (
          <button onClick={onAnalyze} className="mt-3 w-full rounded-lg border border-brand/40 bg-brand/5 py-1.5 text-xs font-medium text-brand hover:bg-brand/10">
            ⚡ Analyze this workload to populate health
          </button>
        )}
      </div>

      {/* Risk + next actions */}
      <div className="space-y-4">
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Risk</div>
          <div className="space-y-1.5 text-sm">
            <RiskRow label="Retirements ≤90 days" value={profile?.risk.retirements_90d} tone="amber" />
            <RiskRow label="Critical retirements" value={profile?.risk.criticals} tone="red" />
            <RiskRow label="DR pairs unhealthy" value={profile?.health.extras?.backupdr?.dr_pairs_unhealthy ?? null} tone="amber" />
          </div>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Next best actions</div>
          {nextActions.length === 0 ? (
            <div className="text-xs text-gray-400">{profile?.analyzed ? "No urgent gaps — nice." : "Run Analyze to surface recommended actions."}</div>
          ) : (
            <ul className="space-y-1.5">
              {nextActions.map((a, i) => (
                <li key={i}>
                  <button onClick={() => onGoSection(a.section)} className="flex w-full items-start gap-2 rounded-lg border px-2.5 py-1.5 text-left text-xs text-gray-600 hover:border-brand/40 hover:bg-brand/5">
                    <span className="text-amber-500">➜</span>
                    <span>{a.label}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function RiskRow({ label, value, tone }: { label: string; value: number | null | undefined; tone: "red" | "amber" }) {
  const n = value ?? null;
  const color = n && n > 0 ? (tone === "red" ? "text-red-600" : "text-amber-600") : "text-gray-400";
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-600">{label}</span>
      <span className={`tabular-nums font-semibold ${color}`}>{n == null ? "—" : n}</span>
    </div>
  );
}

// ---- Resources ------------------------------------------------------------------
function ResourcesTab({ workload, profile }: { workload: Workload; profile?: WorkloadProfile }) {
  const [cat, setCat] = useState<string>("");
  const resources = (workload.nodes || []).filter((n) => n.kind === "resource");
  const byCat = profile?.composition.by_category ?? [];
  const filtered = cat
    ? resources.filter((r) => {
        const t = (r.resource_type || "").toLowerCase();
        // use the friendly label group via the profile's by_type isn't keyed by category;
        // fall back to a simple match on the category chips through taxonomy on the client.
        return categoryOf(t) === cat;
      })
    : resources;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <button onClick={() => setCat("")} className={`rounded-full px-2.5 py-1 text-xs ${cat === "" ? "bg-brand text-white" : "bg-white text-gray-600 ring-1 ring-gray-200"}`}>All {resources.length}</button>
        {byCat.map((c) => (
          <button key={c.category} onClick={() => setCat(c.category)} className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs ${cat === c.category ? "text-white" : "bg-white text-gray-600 ring-1 ring-gray-200"}`} style={cat === c.category ? { backgroundColor: categoryColor(c.category) } : {}}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: cat === c.category ? "#fff" : categoryColor(c.category) }} />
            {c.category} {c.count}
          </button>
        ))}
      </div>
      <div className="overflow-x-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Location</th>
              <th className="px-3 py-2 font-medium">Resource group</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {filtered.map((r, i) => (
              <tr key={r.id || i} className="hover:bg-gray-50">
                <td className="px-3 py-1.5"><span className="flex items-center gap-2"><AzureIcon kind="resource" type={r.resource_type} className="h-4 w-4 text-gray-400" /><span className="truncate text-gray-800">{r.name || "—"}</span></span></td>
                <td className="px-3 py-1.5 text-gray-500">{r.resource_type || "—"}</td>
                <td className="px-3 py-1.5 text-gray-500">{r.location || "—"}</td>
                <td className="px-3 py-1.5 text-gray-500">{r.resource_group || "—"}</td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr><td colSpan={4} className="px-3 py-6 text-center text-xs text-gray-400">No resource-level members. This workload is defined by scope (subscription / resource group) — Refresh it to expand resources.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Minimal client-side category mapping (mirrors backend taxonomy prefixes) for the Resources filter.
function categoryOf(t: string): string {
  const p = t.toLowerCase();
  if (p.startsWith("microsoft.compute/")) return "Compute";
  if (p.startsWith("microsoft.web/")) return "Web";
  if (p.startsWith("microsoft.containerservice/") || p.startsWith("microsoft.containerregistry/") || p.startsWith("microsoft.app/")) return "Containers";
  if (p.startsWith("microsoft.sql/") || p.startsWith("microsoft.dbfor") || p.startsWith("microsoft.documentdb/") || p.startsWith("microsoft.cache/")) return "Data";
  if (p.startsWith("microsoft.storage/")) return "Storage";
  if (p === "microsoft.network/networksecuritygroups" || p.startsWith("microsoft.keyvault/") || p.startsWith("microsoft.security/") || p.startsWith("microsoft.managedidentity/")) return "Security";
  if (p.startsWith("microsoft.network/") || p.startsWith("microsoft.cdn/")) return "Networking";
  if (p.startsWith("microsoft.servicebus/") || p.startsWith("microsoft.eventhub/") || p.startsWith("microsoft.eventgrid/") || p.startsWith("microsoft.logic/") || p.startsWith("microsoft.apimanagement/")) return "Integration";
  if (p.startsWith("microsoft.cognitiveservices/") || p.startsWith("microsoft.machinelearningservices/") || p.startsWith("microsoft.search/")) return "AI / ML";
  if (p.startsWith("microsoft.datafactory/") || p.startsWith("microsoft.synapse/") || p.startsWith("microsoft.databricks/")) return "Analytics";
  if (p.startsWith("microsoft.insights/") || p.startsWith("microsoft.operationalinsights/")) return "Monitoring";
  if (p.startsWith("microsoft.recoveryservices/") || p.startsWith("microsoft.automation/")) return "Management";
  return "Other";
}

// ---- Coverage (health detail + deep links) --------------------------------------
function CoverageTab({ id, profile, navigate }: { id: string; profile?: WorkloadProfile; navigate: ReturnType<typeof useNavigate> }) {
  const links: { label: string; to: string; sig: string }[] = [
    { label: "Monitoring Coverage", to: `/coverage`, sig: "monitoring" },
    { label: "Telemetry Coverage", to: `/telemetry`, sig: "telemetry" },
    { label: "Backup & DR Coverage", to: `/backupdr`, sig: "backupdr" },
    { label: "Performance Profiler", to: `/performance`, sig: "performance" },
    { label: "Ownership", to: `/ownership/coverage`, sig: "ownership" },
  ];
  const open = (to: string) => navigate(`${to}${to.includes("?") ? "&" : "?"}workload_id=${encodeURIComponent(id)}`);
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {links.map((l) => {
        const v = (profile?.health as unknown as Record<string, number | null> | undefined)?.[l.sig] ?? null;
        const band = v == null ? "unknown" : v >= 80 ? "good" : v >= 50 ? "warn" : "poor";
        return (
          <button key={l.sig} onClick={() => open(l.to)} className="rounded-xl border bg-white p-4 text-left transition hover:border-brand/40 hover:shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-800">{l.label}</span>
              <span className={`rounded px-1.5 py-0.5 text-xs font-semibold ${bandBg(band)}`}>{v == null ? "—" : `${Math.round(v)}%`}</span>
            </div>
            <div className="mt-2"><MetricBar label="" value={v} /></div>
            <div className="mt-2 text-[11px] text-brand">Open scoped to this workload →</div>
          </button>
        );
      })}
    </div>
  );
}

// ---- Deep-link tabs (Security & Governance, Lifecycle) --------------------------
function DeepLinkTab({ id, navigate, kind, profile }: { id: string; navigate: ReturnType<typeof useNavigate>; kind: "security" | "lifecycle"; profile?: WorkloadProfile }) {
  // `scoped` cards carry the workload filter to the destination (it reads ?workload_id=);
  // the others are connection/subscription-scoped features that just open.
  const open = (to: string, scoped: boolean) =>
    navigate(scoped ? `${to}${to.includes("?") ? "&" : "?"}workload_id=${encodeURIComponent(id)}` : to);
  const groups = kind === "security"
    ? [
        { label: "Ownership", to: "/ownership", desc: "Accountable owners and teams for this workload.", scoped: true },
        { label: "Azure Policy compliance", to: "/policy", desc: "Compliance against the baseline + assignment gaps.", scoped: false },
        { label: "RBAC access review", to: "/rbac", desc: "Who has role assignments across your estate.", scoped: false },
        { label: "Identity exposure", to: "/identity", desc: "Secrets, expiry and MFA gaps across the tenant.", scoped: false },
      ]
    : [
        { label: "Retirement Radar", to: "/radar", desc: `${profile?.risk.retirements_total ?? "—"} retirements impacting this workload.`, scoped: true },
        { label: "Change Explorer", to: "/change-explorer", desc: "What changed in this workload over time.", scoped: true },
        { label: "Telemetry Intelligence", to: "/telemetry-intel", desc: "AI correlation & triage over App Insights.", scoped: true },
        { label: "Reservations Monitor", to: "/reservations", desc: "Reservation expiry across the account.", scoped: false },
      ];
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {groups.map((g) => (
        <button key={g.to} onClick={() => open(g.to, g.scoped)} className="rounded-xl border bg-white p-4 text-left transition hover:border-brand/40 hover:shadow-sm">
          <div className="text-sm font-medium text-gray-800">{g.label}</div>
          <div className="mt-1 text-xs text-gray-500">{g.desc}</div>
          <div className="mt-2 text-[11px] text-brand">{g.scoped ? "Open scoped to this workload →" : "Open →"}</div>
        </button>
      ))}
    </div>
  );
}

// ---- Watchers (insight-pack coverage) -------------------------------------------
// Distinct from the "Health & Coverage" tab: this shows which AI Insight Packs are
// scheduled to watch this workload — their cadence, freshness and latest verdict.
const WATCH_STATUS = {
  covered: { label: "Covered", badge: "bg-emerald-50 text-emerald-700 border border-emerald-200", ring: "border-emerald-200" },
  stale: { label: "Stale", badge: "bg-amber-50 text-amber-700 border border-amber-200", ring: "border-amber-200" },
  paused: { label: "Paused", badge: "bg-gray-100 text-gray-500 border border-gray-200", ring: "border-gray-200" },
  gap: { label: "Gap", badge: "bg-rose-50 text-rose-700 border border-rose-200", ring: "border-dashed border-rose-200" },
} as const;
const VERDICT_STYLE = {
  urgent: "bg-rose-50 text-rose-700",
  notable: "bg-amber-50 text-amber-700",
  nothing_notable: "bg-gray-100 text-gray-500",
} as const;

function fmtWhen(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = (Date.now() - t) / 1000;
  const ahead = diff < 0;
  const s = Math.abs(diff);
  const rel = s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)}m` : s < 129600 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
  if (rel === "just now") return rel;
  return ahead ? `in ${rel}` : `${rel} ago`;
}

function WatchPill({ n, label, cls }: { n: number; label: string; cls: string }) {
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{n} {label}</span>;
}

function WatchersTab({ id, navigate }: { id: string; navigate: ReturnType<typeof useNavigate> }) {
  const covQ = useQuery({ queryKey: ["insightCoverage", id], queryFn: () => api.insightCoverage(id), enabled: !!id });
  const cov = covQ.data;
  // Land in the Insights library pre-scoped to this workload (and optionally the gap's
  // category) so scheduling a pack watches THIS workload without re-picking the scope.
  const addWatcher = (category?: string) =>
    navigate("/insights/library", { state: { anchorWorkloadId: id, anchorWorkloadName: cov?.workload_name, category } });
  if (covQ.isLoading) return <div className="p-8 text-sm text-gray-500">Loading watchers…</div>;
  if (!cov) return <div className="p-8 text-sm text-gray-500">No coverage data available.</div>;
  const s = cov.summary ?? { covered: 0, stale: 0, paused: 0, gaps: 0 };
  const areas = cov.areas ?? [];
  const upcoming = cov.upcoming ?? [];
  const recent = cov.recent_runs ?? [];
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-gray-700">Watcher coverage</span>
        <WatchPill n={s.covered} label="covered" cls="bg-emerald-50 text-emerald-700" />
        <WatchPill n={s.stale} label="stale" cls="bg-amber-50 text-amber-700" />
        <WatchPill n={s.paused} label="paused" cls="bg-gray-100 text-gray-500" />
        <WatchPill n={s.gaps} label="gaps" cls="bg-rose-50 text-rose-700" />
        <button onClick={() => addWatcher()} className="ml-auto rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">＋ Add a watcher</button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {areas.map((a) => {
          const st = WATCH_STATUS[a.status] ?? WATCH_STATUS.gap;
          return (
            <div key={a.area} className={`rounded-xl border bg-white p-4 ${st.ring}`}>
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-sm font-medium text-gray-800"><span>{a.icon}</span>{a.label}</span>
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${st.badge}`}>{st.label}</span>
              </div>
              {a.packs.length === 0 ? (
                <div className="mt-3">
                  <div className="text-xs text-gray-400">No watcher scheduled.</div>
                  <button onClick={() => addWatcher(a.area)} className="mt-2 text-[11px] font-medium text-brand hover:underline">＋ Add a {a.label.toLowerCase()} watcher →</button>
                </div>
              ) : (
                <div className="mt-3 space-y-2.5">
                  {a.packs.map((w) => {
                    const ws = WATCH_STATUS[w.status] ?? WATCH_STATUS.paused;
                    return (
                      <div key={w.task_id} className="rounded-lg border border-gray-100 bg-gray-50/60 p-2.5">
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-gray-800"><span>{w.pack_icon}</span><span className="truncate">{w.pack_name}</span></span>
                          <span className={`shrink-0 rounded px-1 py-0.5 text-[10px] font-semibold ${ws.badge}`}>{ws.label}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-gray-500">
                          <span>{w.schedule_label}</span>
                          {w.enabled && w.next_run_at ? <span>· next {fmtWhen(w.next_run_at)}</span> : null}
                        </div>
                        {w.last_verdict ? (
                          <div className="mt-1 flex items-center gap-1.5">
                            <span className={`rounded px-1 py-0.5 text-[10px] font-semibold ${VERDICT_STYLE[w.last_verdict] ?? ""}`}>{w.last_verdict.replace("_", " ")}</span>
                            <span className="truncate text-[11px] text-gray-400">{fmtWhen(w.last_run_at)}</span>
                          </div>
                        ) : (
                          <div className="mt-1 text-[11px] text-gray-400">No runs yet</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-xl border bg-white p-4">
          <div className="text-sm font-medium text-gray-800">Upcoming runs</div>
          {upcoming.length === 0 ? (
            <div className="mt-2 text-xs text-gray-400">No scheduled runs in the next 7 days.</div>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {upcoming.slice(0, 8).map((o, i) => (
                <li key={`${o.task_id}-${i}`} className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-1.5 text-gray-700"><span>{o.pack_icon}</span><span className="truncate">{o.pack_name}</span></span>
                  <span className="shrink-0 text-gray-400">{fmtWhen(o.at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="flex items-center justify-between">
            <div className="text-sm font-medium text-gray-800">Recent runs</div>
            <button onClick={() => navigate("/insights")} className="text-[11px] text-brand hover:underline">Open Insights →</button>
          </div>
          {recent.length === 0 ? (
            <div className="mt-2 text-xs text-gray-400">No runs recorded for this workload yet.</div>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {recent.slice(0, 8).map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-1.5 text-gray-700"><span>{r.pack_icon}</span><span className={`shrink-0 rounded px-1 py-0.5 text-[10px] font-semibold ${VERDICT_STYLE[r.verdict] ?? ""}`}>{r.verdict.replace("_", " ")}</span><span className="truncate">{r.headline}</span></span>
                  <span className="shrink-0 text-gray-400">{fmtWhen(r.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
