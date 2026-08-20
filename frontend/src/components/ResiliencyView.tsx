/**
 * Recovery Readiness — recover from what, in how long, losing how much.
 *
 * Structurally a sibling of BackupManagerView: `:tab` route param, ScopePicker, and the
 * analyze gate whose contract is kept verbatim —
 *
 *     Nothing is fetched automatically, so the numbers never move while you are working
 *     a decision.
 *
 * Three renderings are load-bearing and must not be softened:
 *   * `unknown` never looks like a pass — it means a source could not be read, not that a
 *     resource is unprotected;
 *   * `no recovery path` is not a shade of "slow"; it gets its own treatment;
 *   * no bare numbers — every RTO/RPO carries its basis and confidence.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type {
  ResiliencyAnalysis, ResiliencyBreach, ResiliencyMeta, ResiliencyReference,
  ResiliencyResource, ResiliencyScope, ResiliencySnapshot, ResiliencyTrend, ResiliencyWorkload,
} from "../api";
import { formatError } from "../utils/format";
import { hasEffectivePermission } from "../utils/accessControl";
import { usePersistedState } from "../utils/persistedState";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { PdfGeneratingOverlay } from "./PdfGeneratingOverlay";
import { Legend, PortalLink, ScenarioHeatmap, rpoText } from "./resiliency/ScenarioHeatmap";
import { ResourceDrawer, useResource } from "./resiliency/ResourceDrawer";
import { ObjectivesEditor } from "./resiliency/ObjectivesEditor";

type Tab = "overview" | "matrix" | "analysis" | "resources" | "targets" | "workloads";
type ScopeKind = "workload" | "subscription";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "♻️" },
  { id: "matrix", label: "Recovery matrix", icon: "🧭" },
  { id: "analysis", label: "Analysis", icon: "📈" },
  { id: "resources", label: "Resources", icon: "📦" },
  { id: "targets", label: "Targets & breaches", icon: "🎯" },
  { id: "workloads", label: "Workloads", icon: "🏗️" },
];
const VALID = new Set<string>(TABS.map((t) => t.id));

const SCOPE_KEY = "azsup.resiliency";

function toScope(kind: ScopeKind, workloadId: string, subId: string,
                 connId: string): ResiliencyScope {
  const base = { connectionId: connId || null };
  if (kind === "subscription") return { ...base, subscriptionId: subId || null };
  return { ...base, workloadId: workloadId || null };
}

/** Shown on every tab until this scope has been analyzed at least once. */
function NeedsAnalysis({ onAnalyze, running, message }: {
  onAnalyze: () => void; running: boolean; message: string;
}) {
  return (
    <div className="rounded-xl border border-dashed bg-white p-10 text-center"
         data-testid="resiliency-gate">
      <div className="text-4xl">♻️</div>
      <h2 className="mt-3 text-base font-semibold text-gray-800">
        No recovery analysis yet for this scope
      </h2>
      <p className="mx-auto mt-1 max-w-xl text-sm text-gray-500">
        Recovery Readiness reads redundancy configuration, platform backup settings, the backup
        estate and Azure Advisor once, then answers <em>recover from what, in how long, losing
        how much</em> from that single analysis. Nothing is fetched automatically, so the
        numbers never move while you are working a decision.
      </p>
      <button onClick={onAnalyze} disabled={running} data-testid="resiliency-analyze"
              className="mt-4 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
        {running ? "Analyzing…" : "Analyze recovery posture"}
      </button>
      {message && <div className="mt-2 text-xs text-gray-500">{message}</div>}
    </div>
  );
}

function Stat({ label, value, tone = "" }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="text-[11px] uppercase text-gray-500">{label}</div>
      <div className={`text-xl font-semibold tabular-nums ${tone || "text-gray-900"}`}>{value}</div>
    </div>
  );
}

export function ResiliencyPanel() {
  const { tab: routeTab } = useParams<{ tab?: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const tab: Tab = routeTab && VALID.has(routeTab) ? (routeTab as Tab) : "overview";

  const [scopeKind, setScopeKind] = usePersistedState<ScopeKind>(`${SCOPE_KEY}.kind`, "workload");
  const [workloadId, setWorkloadId] = usePersistedState<string>(`${SCOPE_KEY}.workload`, "");
  const [subId, setSubId] = usePersistedState<string>(`${SCOPE_KEY}.sub`, "");
  const [subName, setSubName] = usePersistedState<string>(`${SCOPE_KEY}.subName`, "");
  const [connId, setConnId] = usePersistedState<string>(`${SCOPE_KEY}.conn`, "");

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, staleTime: 60_000 });
  const workloads = workloadsQ.data?.workloads ?? [];

  const scope = useMemo(
    () => toScope(scopeKind, workloadId, subId, connId),
    [scopeKind, workloadId, subId, connId]);
  const scopeReady = scopeKind === "workload" ? !!workloadId : !!subId;

  const [openResource, setOpenResource] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const pdfAbort = useRef<AbortController | null>(null);
  const [savingRef, setSavingRef] = useState(false);
  const [rejected, setRejected] = useState<string[]>([]);

  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 300_000 });
  const canEdit = hasEffectivePermission(me.data, "resiliency.admin");

  const meta = useQuery({ queryKey: ["resiliency", "meta"], queryFn: api.resiliencyMeta });
  const snapshot = useQuery({
    queryKey: ["resiliency", "snapshot", scope],
    queryFn: () => api.resiliencySnapshot(scope),
    enabled: scopeReady,
  });
  const job = useQuery({
    queryKey: ["resiliency", "job", scope],
    queryFn: () => api.resiliencyAnalyzeJob(scope),
    enabled: scopeReady,
    refetchInterval: (q) => (q.state.data?.job?.status === "running" ? 2000 : false),
  });
  const reference = useQuery({
    queryKey: ["resiliency", "reference"], queryFn: api.resiliencyReference,
  });
  const analysisQ = useQuery({
    queryKey: ["resiliency", "analysis", scope],
    queryFn: () => api.resiliencyAnalysis(scope),
    enabled: scopeReady && tab === "analysis",
  });
  const trendQ = useQuery({
    queryKey: ["resiliency", "trend", scope],
    queryFn: () => api.resiliencyTrend(scope),
    enabled: scopeReady,
  });
  const detail = useResource(scope, openResource);

  const running = job.data?.job?.status === "running";
  // Keyed on WHICH run finished, not on the status string. A demo-sized analysis can finish
  // before the first poll, so status goes "done" -> "done" and an effect watching it never
  // re-fires — the second run then refreshed nothing at all.
  const finishedRun = job.data?.job?.status === "done"
    ? (job.data.job.finished_at || job.data.job.started_at || "") : "";
  useEffect(() => {
    if (!finishedRun) return;
    // Everything DERIVED from the snapshot, not just the snapshot. The Analysis tab, the
    // Trend strip and the drawer are separate queries, and leaving them cached showed an
    // empty Analysis tab after the first run until the user reloaded the page.
    for (const key of ["snapshot", "analysis", "trend", "resource"]) {
      void qc.invalidateQueries({ queryKey: ["resiliency", key] });
    }
  }, [finishedRun, qc]);

  const snap = snapshot.data;
  const scenarios = meta.data?.scenarios ?? [];
  // Configuration is the only source that ENUMERATES resources. `protection` is routinely
  // unreadable (Backup Manager has not run) and must stay a footnote, not a headline.
  const estateUnreadable = snap?.provenance?.configuration?.unreadable
    ? (snap.provenance.configuration.reason || "The resource configuration could not be read.")
    : "";
  const classLabels = useMemo(
    () => Object.fromEntries((meta.data?.rto_classes ?? []).map((c) => [c.id, c.label])),
    [meta.data]);

  const rows: ResiliencyResource[] = useMemo(() => {
    let out = snap?.resources ?? [];
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      out = out.filter((r) => r.name.toLowerCase().includes(needle)
        || r.type.toLowerCase().includes(needle));
    }
    return out;
  }, [snap?.resources, search]);

  async function analyze() {
    setErr("");
    try {
      await api.resiliencyAnalyzeStart(scope);
      await job.refetch();
    } catch (e) { setErr(formatError(e)); }
  }

  /** A plain <a href> cannot show a refusal: the 409 for un-agreed objectives would just
   *  navigate nowhere, leaving the reader with no file and no reason. */
  async function download(format: "xlsx" | "pdf") {
    if (busy) return;
    setErr(""); setNote(""); setBusy(format);
    const controller = new AbortController();
    if (format === "pdf") pdfAbort.current = controller;
    try {
      const { blob, filename } = await api.resiliencyExport(scope, format, controller.signal);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      // The server's name if it gave one: it knows the scope and the analysis date, and one
      // file should not have two names depending on how it was fetched.
      const label = (scopeKind === "workload"
        ? workloads.find((w) => w.id === workloadId)?.name || workloadId
        : subName || subId).replace(/[^A-Za-z0-9._-]+/g, "-");
      a.href = url;
      a.download = filename
        || `recovery-readiness-${label}-${String(snap?.generated_at ?? "").slice(0, 10)}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      if ((e as { name?: string } | null)?.name !== "AbortError") setErr(formatError(e));
    } finally { pdfAbort.current = null; setBusy(""); }
  }

  async function saveEvidence() {
    if (busy) return;
    setErr(""); setNote(""); setBusy("evidence");
    try {
      const res = await api.resiliencySaveEvidence(scope);
      setNote(`Captured in the Evidence Locker as "${res.evidence.name}".`);
    } catch (e) { setErr(formatError(e)); }
    finally { setBusy(""); }
  }

  const go = (next: Tab) => navigate(`/resiliency/${next}`);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              <span aria-hidden="true">♻️</span> Recovery Readiness
            </h1>
            <p className="mt-0.5 max-w-3xl text-[13px] text-gray-500">
              Recover from what, in how long, losing how much — per-scenario RTO and RPO derived
              from redundancy, backup frequency and replication, measured against your objectives.
              Redundancy does <span className="font-medium text-red-700">not</span> protect you
              from a bad deployment.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ConnectionScopePicker value={connId} align="right" onChange={(id) => {
              if (id === connId) return;
              setConnId(id); setWorkloadId(""); setSubId(""); setSubName("");
            }} />
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              {(["workload", "subscription"] as const).map((k) => (
                <button key={k} type="button" aria-pressed={scopeKind === k}
                        onClick={() => setScopeKind(k)}
                        className={`rounded-md px-2.5 py-1 ${scopeKind === k ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}>
                  {k === "workload" ? "Workload" : "Subscription"}
                </button>
              ))}
            </div>
            <ScopePicker
              scopeKind={scopeKind}
              onScopeKindChange={() => {}}
              workloads={workloads}
              workloadId={workloadId}
              onWorkloadChange={(id) => {
                const workload = workloads.find((item) => item.id === id);
                if (workload?.connection_id) setConnId(workload.connection_id);
                setWorkloadId(id);
              }}
              subId={subId}
              subName={subName}
              onSubPick={(id, name) => { setSubId(id); setSubName(name); }}
              workloadPlaceholder="Select a workload…"
              connectionId={connId}
              hideKindToggle
            />
            {snap?.report_exists && (
              <>
                <span className="text-[11px] text-gray-500">
                  Analyzed {String(snap.generated_at).slice(0, 16).replace("T", " ")}
                  {snap.demo && " · demo data"}
                </span>
                <button onClick={() => void download("xlsx")} disabled={!!busy}
                        data-testid="resiliency-export"
                        title="Every row: the complete workbook, including the reasoning and provenance sheets."
                        className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  {busy === "xlsx" ? "Building…" : "⬇ Excel"}
                </button>
                <button onClick={() => void download("pdf")} disabled={!!busy}
                        data-testid="resiliency-export-pdf"
                        title="The readable report: the argument, the analysis and the appendices."
                        className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  📄 PDF
                </button>
                <button onClick={() => void saveEvidence()} disabled={!!busy}
                        data-testid="resiliency-evidence"
                        title="Freeze this analysis as an immutable Evidence Locker snapshot."
                        className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  {busy === "evidence" ? "Saving…" : "🗄 Evidence"}
                </button>
                <button onClick={analyze} disabled={running} data-testid="resiliency-reanalyze"
                        title="Recovery Readiness never re-reads Azure on its own. Everything you see is from the last analysis."
                        className="rounded-lg bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-50">
                  {running ? "Analyzing…" : "↻ Analyze again"}
                </button>
              </>
            )}
          </div>
        </div>
        <nav aria-label="Recovery Readiness sections" className="mt-3 flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => go(t.id)} data-testid={`resiliency-tab-${t.id}`}
                    aria-current={tab === t.id ? "page" : undefined}
                    className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm ${
                      tab === t.id
                        ? "bg-brand-dark font-medium text-white"
                        : "text-gray-600 hover:bg-gray-100"}`}>
              <span aria-hidden="true">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="min-h-0 flex-1 overflow-auto bg-gray-50 p-5">
        {err && <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800"
                     data-testid="resiliency-error">{err}</div>}
        {note && <div className="mb-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800"
                      data-testid="resiliency-note">{note}</div>}
        {/* Above the content and on EVERY tab: an empty grid on a scope we could not read
            is indistinguishable from a clean one, and the Overview banner is below the fold. */}
        {estateUnreadable && (
          <div className="mb-3 rounded-lg border border-rose-300 bg-rose-50 p-3 text-sm text-rose-900"
               data-testid="resiliency-unreadable">
            <div className="font-semibold">This scope could not be read — there are no findings here</div>
            <p className="mt-0.5 text-xs">
              Every count on this page is zero because no resource was enumerated, not because
              nothing is at risk. {estateUnreadable}
            </p>
          </div>
        )}
        {!scopeReady ? (
          <div className="rounded-xl border border-dashed bg-white p-10 text-center text-sm text-gray-500"
               data-testid="resiliency-no-scope">
            Choose a workload or subscription to begin.
          </div>
        ) : !snap?.report_exists ? (
          <NeedsAnalysis onAnalyze={analyze} running={running}
                         message={job.data?.job?.messages?.slice(-1)[0]?.message ?? ""} />
        ) : (
          <>
            {tab === "overview" && (
              <OverviewTab snap={snap} scenarios={scenarios} classLabels={classLabels}
                           trend={trendQ.data} unreadable={estateUnreadable}
                           onOpen={(rid) => setOpenResource(rid)} />
            )}
            {tab === "analysis" && (
              <AnalysisTab data={analysisQ.data} loading={analysisQ.isLoading}
                           classLabels={classLabels} />
            )}
            {tab === "matrix" && (
              <div className="rounded-xl border bg-white p-4">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <input value={search} onChange={(e) => setSearch(e.target.value)}
                         placeholder="Filter resources…" data-testid="resiliency-search"
                         className="rounded border px-2 py-1 text-xs" />
                  <Legend />
                </div>
                <ScenarioHeatmap rows={rows} scenarios={scenarios} classLabels={classLabels}
                                 portalHost={snap.portal_host}
                                 onOpen={(rid) => setOpenResource(rid)} />
              </div>
            )}
            {tab === "resources" && <ResourcesTab rows={rows} search={search} setSearch={setSearch}
                                                  portalHost={snap.portal_host}
                                                  onOpen={setOpenResource} />}
            {tab === "targets" && (
              <TargetsTab breaches={snap.breaches} acknowledged={!!snap.targets_acknowledged}
                          classLabels={classLabels} reference={reference.data} meta={meta.data}
                          canEdit={canEdit} saving={savingRef} rejected={rejected}
                          onOpen={setOpenResource}
                          onSave={async (body) => {
                            setSavingRef(true);
                            try {
                              const r = await api.resiliencySaveReference(body);
                              setRejected(r.rejected ?? []);
                              await reference.refetch();
                            } catch (e) { setErr(formatError(e)); }
                            finally { setSavingRef(false); }
                          }}
                          onAcknowledge={async () => {
                            await api.resiliencySaveReference({ targets_acknowledged: true });
                            await reference.refetch();
                            await snapshot.refetch();
                          }} />
            )}
            {tab === "workloads" && <WorkloadsTab rows={snap.workloads} scenarios={scenarios}
                                                  classLabels={classLabels} onOpen={setOpenResource} />}
          </>
        )}
      </main>

      {openResource && detail.data && (
        <ResourceDrawer resource={detail.data.resource} meta={meta.data} scope={scope}
                        portalHost={snap?.portal_host}
                        onClose={() => setOpenResource(null)} />
      )}
      <PdfGeneratingOverlay open={busy === "pdf"} onCancel={() => pdfAbort.current?.abort()}
                            title="Building the Recovery Readiness report"
                            message="Rendering twice so the contents page can show real page numbers." />
    </div>
  );
}

// ---------------------------------------------------------------------------- overview
function OverviewTab({ snap, scenarios, classLabels, trend, unreadable, onOpen }: {
  snap: ResiliencySnapshot;
  scenarios: { id: string; label: string; description: string; redundancy_helps: boolean }[];
  classLabels: Record<string, string>;
  trend: ResiliencyTrend | undefined;
  unreadable: string;
  onOpen: (id: string, scenario: string) => void;
}) {
  const summary = snap.summary;
  const noPath = summary.worst?.no_recovery_path ?? 0;
  // A dash, not a zero. "Protection unknown: 0" on a scope we never enumerated reads as
  // "nothing is unknown" when in fact everything is.
  const stat = (value: number) => (unreadable ? "\u2014" : value);
  return (
    <div className="space-y-4">
      {/* Above the numbers, not below them: a caveat met after the figures have been read
          has already failed to qualify them. */}
      {Object.entries(snap.provenance ?? {}).some(([, p]) => p.unreadable) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900"
             data-testid="resiliency-degraded">
          <div className="font-semibold">Some sources could not be read</div>
          <ul className="ml-4 list-disc">
            {Object.entries(snap.provenance ?? {}).filter(([, p]) => p.unreadable).map(([name, p]) => (
              <li key={name}><span className="font-medium">{name}</span>: {p.reason}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Resources" value={stat(summary.resources)} />
        <Stat label="No recovery path" value={stat(noPath)}
              tone={!unreadable && noPath ? "text-rose-700" : "text-gray-900"} />
        <Stat label="Protection unknown" value={stat(summary.protection?.unknown ?? 0)}
              tone="text-gray-500" />
        <Stat label="Not protected" value={stat(summary.protection?.not_protected ?? 0)}
              tone={!unreadable && (summary.protection?.not_protected ?? 0) ? "text-amber-700" : "text-gray-900"} />
      </div>

      <TrendStrip trend={trend} />

      <div className="rounded-xl border bg-white p-4">
        <h2 className="mb-1 text-sm font-semibold text-gray-900">Recovery by failure scenario</h2>
        <p className="mb-2 text-[11px] text-gray-500">
          Redundancy answers infrastructure loss and does nothing for corruption or deletion —
          zone- and geo-replication copy the damage. A row that is healthy on the left and red
          on the right is a resource that every redundancy check calls resilient.
        </p>
        <div className="mb-2"><Legend /></div>
        <ScenarioHeatmap rows={snap.resources} scenarios={scenarios as never}
                         classLabels={classLabels} portalHost={snap.portal_host}
                         onOpen={onOpen} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- trend
function TrendStrip({ trend }: { trend: ResiliencyTrend | undefined }) {
  if (!trend) return null;
  if (!trend.available) {
    return (
      <div className="rounded-xl border bg-white p-3 text-[11px] text-gray-500"
           data-testid="resiliency-trend-empty">
        <span className="font-medium text-gray-700">No direction yet.</span>{" "}
        {trend.reason || "At least two analyses are needed."} A line through one point would
        invite you to see a change that was never measured.
      </div>
    );
  }
  const delta = trend.deltas?.no_recovery_path ?? 0;
  const better = delta < 0;
  return (
    <div className="rounded-xl border bg-white p-3" data-testid="resiliency-trend">
      <div className="flex flex-wrap items-baseline gap-2 text-xs">
        <span className="font-semibold text-gray-900">Trend</span>
        <span className="text-gray-500">across {trend.points.length} analyses</span>
        <span className={`font-semibold ${better ? "text-emerald-700" : delta > 0 ? "text-rose-700" : "text-gray-500"}`}>
          {delta === 0 ? "no change" : `${better ? "−" : "+"}${Math.abs(delta)} with no recovery path`}
        </span>
      </div>
      {trend.reading_degraded && (
        <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-900"
             data-testid="resiliency-trend-caveat">
          <span className="font-semibold">Not necessarily an improvement.</span>{" "}
          {trend.caveat}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- analysis
/**
 * The aggregate lens. Reads the same endpoint the exports read, so a number here and a
 * number in the workbook cannot disagree.
 *
 * Two renderings are load-bearing: `undetermined` is a column of its own and never folded
 * into a rate, and a median RPO always shows how many resources it excluded.
 */
function AnalysisTab({ data, loading, classLabels }: {
  data: ResiliencyAnalysis | undefined;
  loading: boolean;
  classLabels: Record<string, string>;
}) {
  const [scenario, setScenario] = useState("");
  if (loading) {
    return <div className="rounded-xl border bg-white p-10 text-center text-xs text-gray-400">
      Aggregating…
    </div>;
  }
  if (!data?.report_exists) {
    // The parent only renders this tab once a snapshot exists, so landing here means the
    // aggregate read is stale. Say so — rendering null was a blank screen that gave the
    // reader nothing to act on and hid a cache bug.
    return <div className="rounded-xl border border-dashed bg-white p-10 text-center text-sm text-gray-500"
                data-testid="resiliency-analysis-empty">
      No analysis is loaded for this scope yet. Reopen this tab, or run an analysis.
    </div>;
  }

  const scenarios = Array.from(new Set(data.by_type.map((r) => r.scenario)));
  const rows = scenario ? data.by_type.filter((r) => r.scenario === scenario) : data.by_type;

  return (
    <div className="space-y-4" data-testid="resiliency-analysis">
      {!!data.redundancy_gap.length && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4"
             data-testid="analysis-thesis">
          <h2 className="text-sm font-semibold text-rose-900">
            {data.redundancy_gap.length === 1
              ? "1 redundant resource is far worse against a bad deployment than against losing a region"
              : `${data.redundancy_gap.length} redundant resources are far worse against a bad deployment than against losing a region`}
          </h2>
          <p className="mt-1 text-[11px] text-rose-800">
            Zone and geo replication copy corruption and deletion, usually within seconds.
            Every redundancy check calls these resilient, so no zone-centric tool flags them.
          </p>
          <ul className="mt-2 grid gap-1 text-xs text-rose-900 md:grid-cols-2">
            {data.redundancy_gap.slice(0, 8).map((r) => (
              <li key={r.id}>
                <span className="font-medium">{r.name}</span>
                <span className="text-rose-700">
                  {" — "}{classLabels[r.infra_rto_class] ?? r.infra_rto_class} for
                  infrastructure, {classLabels[r.logical_rto_class] ?? r.logical_rto_class}
                  {" "}for {r.worse_for.join(" and ").toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">RTO and RPO by resource type</h2>
            <p className="text-[11px] text-gray-500">
              Ranked by consequence. Where a whole type shares one weakness, the fix is
              usually one change.
            </p>
          </div>
          <select value={scenario} onChange={(e) => setScenario(e.target.value)}
                  data-testid="analysis-scenario"
                  className="rounded border px-2 py-1 text-xs">
            <option value="">All scenarios</option>
            {scenarios.map((s) => (
              <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
            ))}
          </select>
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-500">
              {["Resource type", "Scenario", "Res.", "No path", "Breach", "Worst RTO",
                "Undet.", "Median RPO", "RPO excl.", "Dominant reason"].map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.type}-${r.scenario}`} className="border-t" data-testid="analysis-type-row">
                <td className="max-w-[200px] truncate px-2 py-1 font-medium" title={r.type}>
                  {r.type.replace(/^microsoft\./i, "")}
                </td>
                <td className="px-2 py-1 text-gray-500">{r.scenario.replace(/_/g, " ")}</td>
                <td className="px-2 py-1 tabular-nums">{r.resources}</td>
                <td className={`px-2 py-1 tabular-nums ${r.no_recovery_path ? "font-semibold text-rose-700" : "text-gray-300"}`}>
                  {r.no_recovery_path || "—"}
                </td>
                <td className={`px-2 py-1 tabular-nums ${r.breached ? "text-amber-700" : "text-gray-300"}`}>
                  {r.breached || "—"}
                </td>
                <td className="px-2 py-1">{classLabels[r.worst_rto_class] ?? r.worst_rto_class}</td>
                {/* Its own column, never folded into a rate: a type with three unreadable
                    resources must not render as "94% fine". */}
                <td className={`px-2 py-1 tabular-nums ${r.undetermined ? "text-gray-600" : "text-gray-300"}`}>
                  {r.undetermined || "—"}
                </td>
                <td className="px-2 py-1 tabular-nums">{minutesLabel(r.rpo.median_minutes)}</td>
                <td className={`px-2 py-1 tabular-nums ${r.rpo.excluded ? "text-gray-600" : "text-gray-300"}`}
                    title="Resources the median could not cover, because their recovery point is unknown or absent.">
                  {r.rpo.excluded || "—"}
                </td>
                <td className="max-w-[260px] px-2 py-1 text-[11px] text-gray-600">
                  {r.dominant_reason}
                  {r.dominant_reason_count > 1 && (
                    <span className="ml-1 font-semibold text-gray-500">×{r.dominant_reason_count}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-[11px] text-gray-500">
          &ldquo;Median RPO&rdquo; covers only resources whose recovery point could be
          measured; &ldquo;RPO excl.&rdquo; is how many it leaves out. A type that cannot
          experience a scenario is absent from it rather than shown as meeting its objective.
        </p>
      </div>

      {!!data.reasons.length && (
        <div className="rounded-xl border bg-white p-4">
          <h2 className="text-sm font-semibold text-gray-900">
            Why — the reasons that explain the most
          </h2>
          <p className="mb-2 text-[11px] text-gray-500">
            The same misconfiguration recurs. Working down this list moves more resources
            than working down a resource list, because one row can be one change.
          </p>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500">
                {["Scenario", "Reason", "Resources", "No path", "Types"].map((h) => (
                  <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.reasons.map((r, i) => (
                <tr key={i} className="border-t" data-testid="analysis-reason-row">
                  <td className="px-2 py-1 text-gray-500">{r.scenario.replace(/_/g, " ")}</td>
                  <td className="px-2 py-1">{r.reason}</td>
                  <td className="px-2 py-1 tabular-nums">{r.resources}</td>
                  <td className={`px-2 py-1 tabular-nums ${r.no_recovery_path ? "font-semibold text-rose-700" : "text-gray-300"}`}>
                    {r.no_recovery_path || "—"}
                  </td>
                  <td className="max-w-[220px] truncate px-2 py-1 text-[11px] text-gray-500"
                      title={r.types.join(", ")}>
                    {r.types.map((t) => t.replace(/^microsoft\./i, "")).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function minutesLabel(minutes: number | null): string {
  if (minutes === null) return "—";
  if (minutes === 0) return "0 (sync)";
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

// --------------------------------------------------------------------------- resources
function ResourcesTab({ rows, search, setSearch, portalHost, onOpen }: {
  rows: ResiliencyResource[]; search: string; setSearch: (v: string) => void;
  portalHost?: string;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="rounded-xl border bg-white p-4">
      <input value={search} onChange={(e) => setSearch(e.target.value)}
             placeholder="Filter resources…"
             className="mb-2 rounded border px-2 py-1 text-xs" />
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-500">
            {["Resource", "Type", "Redundancy", "Protection", "Backup frequency", "Worst"].map((h) => (
              <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="cursor-pointer border-t hover:bg-gray-50"
                onClick={() => onOpen(r.id)} data-testid="resource-row">
              <td className="max-w-[240px] px-2 py-1 font-medium">
                <span className="flex items-center gap-1.5">
                  <span className="truncate" title={r.name}>{r.name}</span>
                  <PortalLink resourceId={r.id} portalHost={portalHost}
                              label={`Open ${r.name} in the Azure portal`} />
                </span>
              </td>
              <td className="px-2 py-1 text-gray-500">{r.type.split("/").pop()}</td>
              <td className="px-2 py-1">{r.redundancy.replication || (r.redundancy.zone_redundant ? "zone-redundant" : "—")}</td>
              <td className="px-2 py-1">
                <span className={r.protection.state === "unknown" ? "text-gray-400"
                  : r.protection.state === "not_protected" ? "text-amber-700" : "text-emerald-700"}>
                  {r.protection.state.replace("_", " ")}
                </span>
              </td>
              <td className="px-2 py-1 text-gray-600">{r.protection.frequency || "—"}</td>
              <td className={`px-2 py-1 ${r.worst.rto_class === "none" ? "font-semibold text-rose-700" : "text-gray-600"}`}>
                {r.worst.rto_class === "none" ? "no recovery path" : r.worst.rto_class}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ----------------------------------------------------------------------------- targets
function TargetsTab({
  breaches, acknowledged, classLabels, reference, meta, canEdit, saving, rejected,
  onOpen, onSave, onAcknowledge,
}: {
  breaches: ResiliencyBreach[]; acknowledged: boolean;
  classLabels: Record<string, string>;
  reference: ResiliencyReference | undefined;
  meta: ResiliencyMeta | undefined;
  canEdit: boolean; saving: boolean; rejected: string[];
  onOpen: (id: string) => void;
  onSave: (body: Partial<ResiliencyReference>) => Promise<void>;
  onAcknowledge: () => Promise<void>;
}) {
  const [view, setView] = useState<"breaches" | "objectives">("breaches");
  return (
    <div className="space-y-4">
      <div className="flex w-fit items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
        {(["breaches", "objectives"] as const).map((v) => (
          <button key={v} type="button" aria-pressed={view === v}
                  onClick={() => setView(v)} data-testid={`targets-view-${v}`}
                  className={`rounded-md px-2.5 py-1 ${view === v ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}>
            {v === "breaches" ? "Breaches" : "Objectives & rates"}
          </button>
        ))}
      </div>

      {!acknowledged && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900"
             data-testid="resiliency-defaults-banner">
          <div className="font-semibold">These objectives are the shipped defaults</div>
          <p className="mt-1">
            They are usable here immediately, but a report that quotes them is refused until
            somebody agrees them — a number handed to an auditor has to have an owner.
          </p>
          <button onClick={() => void onAcknowledge()} data-testid="resiliency-acknowledge"
                  disabled={!canEdit}
                  className="mt-2 rounded bg-gray-900 px-2 py-1 text-[11px] text-white disabled:opacity-50">
            Acknowledge these objectives
          </button>
        </div>
      )}

      {view === "objectives" ? (
        reference ? (
          <ObjectivesEditor reference={reference} meta={meta} canEdit={canEdit}
                            onSave={onSave} saving={saving} rejected={rejected} />
        ) : (
          <div className="rounded-xl border bg-white p-6 text-center text-xs text-gray-400">
            Loading objectives…
          </div>
        )
      ) : (
      <div className="rounded-xl border bg-white p-4">
        <h2 className="mb-1 text-sm font-semibold text-gray-900">Breaches ({breaches.length})</h2>
        <p className="mb-2 text-[11px] text-gray-500">
          Ordered by consequence: no recovery path first, then total data loss, then the size
          of the miss weighted by tier.
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-500">
              {["Resource", "Scenario", "Tier", "RPO", "RTO", "Objective", "Why"].map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {breaches.map((b, i) => (
              <tr key={`${b.resource_id}-${b.scenario}-${i}`}
                  className="cursor-pointer border-t hover:bg-gray-50"
                  onClick={() => onOpen(b.resource_id)} data-testid="breach-row">
                <td className="max-w-[200px] truncate px-2 py-1 font-medium">{b.name}</td>
                <td className="px-2 py-1">{b.scenario.replace(/_/g, " ")}</td>
                <td className="px-2 py-1 text-gray-500">{b.tier}</td>
                <td className="px-2 py-1">{rpoText(b as never)}</td>
                <td className={`px-2 py-1 ${b.no_recovery_path ? "font-semibold text-rose-700" : ""}`}>
                  {classLabels[b.rto_class] ?? b.rto_class}
                </td>
                <td className="px-2 py-1 text-gray-500">
                  {b.target?.rto_class ? (classLabels[b.target.rto_class] ?? b.target.rto_class) : "—"}
                </td>
                <td className="px-2 py-1 text-[11px] text-gray-600">
                  {b.basis?.[0]?.detail ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!breaches.length && (
          <div className="p-4 text-center text-xs text-gray-400">
            Nothing breaches its objective in this scope.
          </div>
        )}
      </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- workloads
function WorkloadsTab({ rows, scenarios, classLabels, onOpen }: {
  rows: ResiliencyWorkload[];
  scenarios: { id: string; label: string }[];
  classLabels: Record<string, string>;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="space-y-3">
      {rows.map((w) => (
        <div key={w.workload_id} className="rounded-xl border bg-white p-4" data-testid="workload-card">
          <div className="mb-2 flex flex-wrap items-baseline gap-2">
            <h2 className="text-sm font-semibold text-gray-900">{w.name}</h2>
            <span className="text-[11px] text-gray-500">{w.tier} · {w.components} components</span>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500">
                {["Scenario", "RPO", "RTO", "Weakest link", "Coverage"].map((h) => (
                  <th key={h} className="px-2 py-1 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => {
                const spec = w.scenarios[s.id];
                if (!spec?.applicable) return null;
                return (
                  <tr key={s.id} className="border-t">
                    <td className="px-2 py-1">{s.label}</td>
                    <td className="px-2 py-1">{rpoText(spec as never)}</td>
                    <td className={`px-2 py-1 ${spec.rto_class === "none" ? "font-semibold text-rose-700" : ""}`}>
                      {classLabels[spec.rto_class] ?? spec.rto_class}
                    </td>
                    <td className="px-2 py-1">
                      {spec.weakest_link ? (
                        <button className="text-brand hover:underline"
                                data-testid="weakest-link"
                                onClick={() => onOpen(spec.weakest_link!.id)}>
                          {spec.weakest_link.name}
                        </button>
                      ) : <span className="text-gray-400">—</span>}
                    </td>
                    <td className="px-2 py-1 tabular-nums text-gray-500">
                      {spec.coverage.determined}/{spec.coverage.total}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-2 text-[10px] text-gray-400">
            {(w.scenarios[scenarios[0]?.id]?.assumptions ?? []).join(" ")}
          </p>
        </div>
      ))}
      {!rows.length && (
        <div className="rounded-xl border bg-white p-6 text-center text-xs text-gray-400">
          No workloads in this scope.
        </div>
      )}
    </div>
  );
}
