/**
 * Azure Workload Change Explorer — a read-only, multi-tab screen that shows every meaningful
 * change made to a workload's Azure resources during a selected time window, with risk scores,
 * plain-English explanations, technical diffs, dependency/blast-radius hints and export.
 *
 * Not a chat feature. The user picks tenant + workload + time range + scope, clicks Analyze,
 * and reviews the results across nine URL-routed tabs (Summary, Timeline, All Changes, Risk
 * Insights, Resources, Actors, Technical Diff, Dependency Impact, Export). Read-only — no
 * remediation, no writes. Demo data (Contoso Website Prod) lets it run with no live Azure.
 */
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer, PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip,
} from "recharts";
import {
  api, streamChangeExplorerAnalyze,
  type ChangeAnalysisRun, type ChangeEvent, type ChangeProgress, type ChangeRunSummary, type ChangeAnalyzeBody,
  type ChangeAskResponse,
} from "../api";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { ScopePicker, type ScopeKind } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { TimeRangePicker } from "./changeexplorer/TimeRangePicker";
import { PageIntro } from "./PageIntro";
import { CHANGEEXPLORER_NAV, type ChangeExplorerTab } from "./navConfig";
import { formatError } from "../utils/format";

const RISK_COLOR: Record<string, string> = {
  Critical: "#dc2626", High: "#ea580c", Medium: "#d97706", Low: "#2563eb", Informational: "#64748b",
};
const RISK_BG: Record<string, string> = {
  Critical: "bg-red-100 text-red-700", High: "bg-orange-100 text-orange-700",
  Medium: "bg-amber-100 text-amber-700", Low: "bg-blue-100 text-blue-700",
  Informational: "bg-gray-100 text-gray-600",
};

// --------------------------------------------------------------- background analysis registry
// An analysis is keyed by its scopeKey and runs at MODULE scope, so it keeps streaming — and the
// result auto-surfaces — even if the user navigates away and comes back (exactly like the
// Performance Profiler / coverage screens). Per-scope so a scope with no active run isn't shown
// as "Analyzing…".
interface AnalysisState { progress: ChangeProgress | null; result: ChangeAnalysisRun | null; error: string; abort: AbortController }
const _runs = new Map<string, AnalysisState>();
const _lastResult = new Map<string, ChangeAnalysisRun>();   // last completed run per scope (survives nav)
let _version = 0;
const _subs = new Set<() => void>();
function _bump() { _version++; _subs.forEach((f) => f()); }
function useAnalysisVersion(): number {
  return useSyncExternalStore((cb) => { _subs.add(cb); return () => _subs.delete(cb); }, () => _version, () => _version);
}
function startAnalysis(scopeKey: string, body: ChangeAnalyzeBody) {
  if (_runs.has(scopeKey)) return;
  const abort = new AbortController();
  _runs.set(scopeKey, { progress: { phase: "start", message: "Starting analysis…" }, result: null, error: "", abort });
  _bump();
  void streamChangeExplorerAnalyze(body, {
    onProgress: (p) => { const s = _runs.get(scopeKey); if (s) { s.progress = p; _bump(); } },
    onDone: (run) => { _lastResult.set(scopeKey, run); _runs.delete(scopeKey); _bump(); },
    onError: (msg) => { const s = _runs.get(scopeKey); if (s) { s.error = msg; } _runs.delete(scopeKey); _bump(); },
  }, abort.signal).catch((e) => { _runs.delete(scopeKey); _lastResult.delete(scopeKey); console.error(e); _bump(); });
}

function fmtTime(iso: string): string {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}
function shortType(t: string): string { return (t || "").split("/").slice(1).join("/") || t; }

function RiskChip({ label, score }: { label: string; score?: number }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${RISK_BG[label] || "bg-gray-100 text-gray-600"}`}>
      {label}{score !== undefined ? ` ${score}` : ""}
    </span>
  );
}

function download(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// =====================================================================================
export function ChangeExplorerPanel({ tab = "summary" }: { tab?: ChangeExplorerTab }) {
  const [scopeKind, setScopeKind] = usePersistedState<ScopeKind>("azsup.changeexp.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState<string>("azsup.changeexp.workload", "");
  const [subId, setSubId] = usePersistedState<string>("azsup.changeexp.subId", "");
  const [subName, setSubName] = usePersistedState<string>("azsup.changeexp.subName", "");
  const [scopeMode, setScopeMode] = usePersistedState<string>("azsup.changeexp.scope", "workload");
  const [connId, setConnId] = usePersistedState<string>("azsup.changeexp.connId", "");
  const [start, setStart] = usePersistedState<string>("azsup.changeexp.start", defaultStart());
  const [end, setEnd] = usePersistedState<string>("azsup.changeexp.end", defaultEnd());
  const [rangeLabel, setRangeLabel] = usePersistedState<string>("azsup.changeexp.rangeLabel", "Last 24 hours");
  const [shownRun, setShownRun] = useState<ChangeAnalysisRun | null>(null);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState<ChangeEvent | null>(null);
  const [confirmTenant, setConfirmTenant] = useState(false);   // tenant-wide scope yes/no gate
  useWorkloadDeepLink(setScopeKind, setWorkloadId);

  // Result-side filters.
  const [fRisk, setFRisk] = useState(""); const [fCat, setFCat] = useState("");
  const [fActor, setFActor] = useState(""); const [fType, setFType] = useState(""); const [search, setSearch] = useState("");
  // NL "Ask AI" change search — composes with the manual filters across the list tabs.
  const [aiQ, setAiQ] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiRes, setAiRes] = useState<ChangeAskResponse | null>(null);
  const [aiMatchIds, setAiMatchIds] = useState<Set<string> | null>(null);

  const workloadsQ = useQuery({ queryKey: ["changeExplorerWorkloads"], queryFn: api.changeExplorerWorkloads });
  const workloads = workloadsQ.data?.workloads ?? [];
  const effWorkloadId = scopeKind === "workload" ? (workloadId || workloads[0]?.id || "") : "";
  const scopeKey = `${scopeKind}:${effWorkloadId || subId}`;
  const runKey = scopeKind === "workload" ? effWorkloadId : `sub:${subId}`;   // server run-store key
  const scopeReady = scopeKind === "workload" ? !!effWorkloadId : !!subId;

  // Background analysis state for THIS scope (survives navigation).
  useAnalysisVersion();
  const active = _runs.get(scopeKey) ?? null;
  const analyzing = !!active;
  const bgResult = _lastResult.get(scopeKey) ?? null;

  // Run history for this scope (the small grid + auto-load-latest).
  const runsQ = useQuery({
    queryKey: ["changeExplorerRuns", runKey],
    queryFn: () => api.changeExplorerRuns(runKey),
    enabled: scopeReady,
  });
  const history = runsQ.data?.runs ?? [];

  // Trashed runs for this scope (soft-deleted; restorable or purged from the Trash view).
  const trashQ = useQuery({
    queryKey: ["changeExplorerTrash", runKey],
    queryFn: () => api.changeExplorerTrash(runKey),
    enabled: scopeReady,
  });
  const trashed = trashQ.data?.runs ?? [];

  // The run actually displayed: an explicit selection, else a background result that just landed,
  // else nothing (the user loads from history or clicks Analyze). When a background run finishes,
  // surface it automatically.
  useEffect(() => {
    if (bgResult && bgResult !== shownRun) setShownRun(bgResult);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bgResult]);

  // Auto-load the latest cached run for a scope on first arrival (NO new analysis) — only if we
  // aren't already showing/analyzing something for it.
  useEffect(() => {
    if (!scopeReady || analyzing || (shownRun && belongsToScope(shownRun, scopeKind, effWorkloadId, subId))) return;
    const latest = history[0];
    if (latest) void loadRun(latest.runId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, history.length]);

  async function loadRun(runId: string) {
    setErr("");
    try { const r = await api.changeExplorerRun(runId); setShownRun(r); setSelected(null); }
    catch (e) { setErr(formatError(e)); }
  }
  async function deleteRun(runId: string) {
    try {
      await api.changeExplorerDeleteRun(runId);                         // soft-delete → Trash
      await Promise.all([runsQ.refetch(), trashQ.refetch()]);
      if (shownRun?.runId === runId) setShownRun(null);
    } catch (e) { setErr(formatError(e)); }
  }
  async function restoreRun(runId: string) {
    try { await api.changeExplorerRestoreRun(runId); await Promise.all([runsQ.refetch(), trashQ.refetch()]); }
    catch (e) { setErr(formatError(e)); }
  }
  async function purgeRun(runId: string) {
    try { await api.changeExplorerPurgeRun(runId); await trashQ.refetch(); }   // permanent
    catch (e) { setErr(formatError(e)); }
  }

  function analyze(confirmed = false) {
    if (!scopeReady) { setErr(scopeKind === "workload" ? "Pick a workload first." : "Pick a subscription first."); return; }
    // Tenant-wide is a heavy, broad scan — require an explicit yes/no first.
    if (scopeMode === "tenant" && !confirmed) { setConfirmTenant(true); return; }
    setConfirmTenant(false);
    setErr(""); setSelected(null);
    const body: ChangeAnalyzeBody = scopeKind === "workload"
      ? { workload_id: effWorkloadId, connection_id: connId, start_time: toIso(start), end_time: toIso(end), scope_mode: scopeMode }
      : { subscription_id: subId, subscription_name: subName, connection_id: connId, start_time: toIso(start), end_time: toIso(end), scope_mode: scopeMode };
    startAnalysis(scopeKey, body);
    // Refresh history shortly after the run persists.
    setTimeout(() => void runsQ.refetch(), 1500);
  }

  // When a background run for this scope completes, refresh the history grid.
  useEffect(() => { if (bgResult) void runsQ.refetch(); /* eslint-disable-next-line */ }, [bgResult]);

  const run = shownRun;
  const events = run?.events ?? [];
  const filtered = useMemo(() => events.filter((e) =>
    (!fRisk || e.riskLabel === fRisk) && (!fCat || e.category === fCat) &&
    (!fActor || e.actor === fActor) && (!fType || e.resourceType === fType) &&
    (!aiMatchIds || aiMatchIds.has(e.changeId)) &&
    (!search || `${e.resourceName} ${e.plainEnglishSummary} ${e.operation}`.toLowerCase().includes(search.toLowerCase()))
  ), [events, fRisk, fCat, fActor, fType, search, aiMatchIds]);

  // Clear any AI result when the displayed run changes (its ids won't match a different run).
  useEffect(() => { setAiRes(null); setAiMatchIds(null); /* eslint-disable-next-line */ }, [run?.runId]);

  async function askAi(question: string) {
    if (!question.trim() || !run) return;
    setAiBusy(true); setAiQ(question);
    try {
      const r = await api.changeExplorerAsk({ question, run_id: run.runId });
      setAiRes(r);
      setAiMatchIds(r.in_window === false ? new Set() : new Set(r.matched_ids ?? []));
    } catch (e) { setErr(formatError(e)); } finally { setAiBusy(false); }
  }
  function clearAi() { setAiRes(null); setAiMatchIds(null); setAiQ(""); }
  // Re-scan the suggested (out-of-window) range, then re-run the same question.
  function rescanSuggested() {
    const w = aiRes?.suggested_window;
    if (!w) return;
    setStart(toLocalInput(new Date(w.start_iso)));
    setEnd(toLocalInput(new Date(w.end_iso)));
    setRangeLabel(w.label || `${new Date(w.start_iso).toLocaleDateString()} → ${new Date(w.end_iso).toLocaleDateString()}`);
    clearAi();
    setTimeout(() => analyze(false), 0);
  }


  const navItem = CHANGEEXPLORER_NAV.find((n) => n.id === tab) ?? CHANGEEXPLORER_NAV[0];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Filter bar */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex items-center gap-2">
          <span className="text-lg">🧭</span>
          <h1 className="text-base font-semibold text-gray-900">Workload Change Explorer</h1>
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">read-only</span>
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-0.5">
            <span className="text-xs text-gray-500">Azure tenant</span>
            <ConnectionScopePicker value={connId} align="left" onChange={(id) => { setConnId(id); setConfirmTenant(false); if (scopeKind === "subscription") { setSubId(""); setSubName(""); } }} />
          </div>
          <div className="flex flex-col gap-0.5">
            <span className="text-xs text-gray-500">Workload / subscription</span>
            <ScopePicker
              scopeKind={scopeKind}
              onScopeKindChange={setScopeKind}
              workloads={workloads}
              workloadId={effWorkloadId}
              onWorkloadChange={setWorkloadId}
              subId={subId}
              subName={subName}
              onSubPick={(id, name) => { setSubId(id); setSubName(name); }}
              connectionId={connId}
            />
          </div>
          <div className="flex flex-col gap-0.5">
            <span className="text-xs text-gray-500">Time range</span>
            <TimeRangePicker
              start={start}
              end={end}
              label={rangeLabel}
              onApply={(s, e, lbl) => { setStart(s); setEnd(e); setRangeLabel(lbl); }}
            />
          </div>
          <label className="text-xs text-gray-500">Scope
            <select value={scopeMode} onChange={(e) => { setScopeMode(e.target.value); setConfirmTenant(false); }} className="mt-0.5 block rounded border px-2 py-1 text-sm">
              <option value="workload">Workload only</option>
              <option value="workload_dependencies">Workload + dependencies</option>
              <option value="tenant">Tenant-wide</option>
            </select>
          </label>
          <button onClick={() => analyze()} disabled={analyzing} className="rounded-lg bg-gray-900 px-4 py-1.5 text-sm text-white disabled:opacity-50">
            {analyzing ? "Analyzing…" : "⚡ Analyze Changes"}
          </button>
        </div>
        {err && <div className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">{err}</div>}
        {active?.error && <div className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">{active.error}</div>}

        {/* Tenant-wide confirmation — this scope scans every subscription, so confirm yes/no first. */}
        {confirmTenant && !analyzing && (
          <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
            <span className="text-sm">⚠️</span>
            <span className="text-xs text-amber-800">
              <b>Tenant-wide scan.</b> This queries Resource Graph &amp; the Activity Log across <b>every subscription</b> in the tenant — it can take a while and pull a large number of changes. Run it?
            </span>
            <div className="ml-auto flex gap-2">
              <button onClick={() => setConfirmTenant(false)} className="rounded border border-amber-300 px-3 py-1 text-xs text-amber-700 hover:bg-amber-100">No, cancel</button>
              <button onClick={() => analyze(true)} className="rounded bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700">Yes, run tenant-wide</button>
            </div>
          </div>
        )}

        {/* Live progress — keeps streaming + updating even if you navigate away and back. */}
        {analyzing && active && <AnalysisProgress progress={active.progress} />}

        {/* History grid — previous cached runs for this scope (click to load instantly / trash). */}
        {scopeReady && (history.length > 0 || trashed.length > 0) && (
          <HistoryGrid runs={history} trashed={trashed} currentRunId={run?.runId}
            onLoad={loadRun} onDelete={deleteRun} onRestore={restoreRun} onPurge={purgeRun} />
        )}

        {/* Tabs */}
        {run && (
          <div className="mt-3 flex flex-wrap gap-1">
            {CHANGEEXPLORER_NAV.map((n) => (
              <Link key={n.id} to={n.id === "summary" ? "/change-explorer" : `/change-explorer/${n.id}`}
                className={`rounded-lg px-3 py-1.5 text-sm transition ${tab === n.id ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-100"}`}>{n.label}</Link>
            ))}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="px-5 pt-3">
          <PageIntro title={navItem.label.replace(/^\S+\s/, "")}
            blurb="Read-only analysis of what changed, when, who changed it, how risky it is, and what it could impact — in plain English."
            storageKey="changeexplorer" />
        </div>

        {!run ? (
          <div className="m-6 rounded-xl border border-dashed bg-gray-50 p-10 text-center">
            <div className="text-3xl">🧭</div>
            {analyzing
              ? <p className="mt-2 text-sm font-medium text-gray-700">Analyzing changes… progress is shown above and continues even if you navigate away.</p>
              : <>
                  <p className="mt-2 text-sm font-medium text-gray-700">Pick a workload or subscription, a time range and scope, then click <b>Analyze Changes</b>.</p>
                  <p className="mt-1 text-xs text-gray-500">Cached runs auto-load here; new analysis only runs when you click Analyze. Tip: the <b>Contoso Website Prod (demo)</b> workload has built-in sample data — no Azure connection needed.</p>
                </>}
          </div>
        ) : (
          <div className="px-5 pb-10">
            {/* Change-limit banner — shown on EVERY tab when the scan was capped at the source
                limit (e.g. 1,000 most recent), so the totals are never mistaken for the full set. */}
            {(run.changeLimit ?? 0) > 0 && (
              <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <span className="text-sm">⚠️</span>
                <span>
                  <b>Showing the {run.changeLimit!.toLocaleString()} most recent changes.</b> This window has more changes than the per-scan limit, so older changes aren’t shown. Narrow the time range or scope (workload / subscription) to see all changes.
                </span>
              </div>
            )}
            {/* Result filters (shared across tabs that list changes) */}
            {["timeline", "changes", "diff"].includes(tab) && (
              <>
              {/* ✨ Ask AI — natural-language change search ("show me all VMs modified yesterday").
                  Composes with the manual filters below by narrowing to the matched change ids. */}
              <div className="mb-3 rounded-xl border bg-gradient-to-br from-violet-50 to-white p-3">
                <div className="flex items-center gap-2">
                  <span className="text-base">✨</span>
                  <span className="text-sm font-medium text-gray-800">Ask AI</span>
                  <span className="text-[11px] text-gray-400">natural-language change search — type a window too (e.g. “yesterday”, “last 7 days”)</span>
                </div>
                <div className="mt-2 flex gap-2">
                  <input
                    value={aiQ} onChange={(e) => setAiQ(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && aiQ.trim()) void askAi(aiQ); }}
                    placeholder="e.g. show me all VMs modified yesterday"
                    className="flex-1 rounded-lg border px-3 py-2 text-sm"
                  />
                  <button onClick={() => aiQ.trim() && askAi(aiQ)} disabled={aiBusy} className="rounded-lg bg-violet-600 px-4 py-2 text-sm text-white disabled:opacity-50">{aiBusy ? "…" : "Ask"}</button>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {["VMs changed yesterday", "RBAC changes last 7 days", "Critical changes today", "Deletions this week"].map((s) => (
                    <button key={s} onClick={() => void askAi(s)} className="rounded-full border bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">{s}</button>
                  ))}
                </div>
                {aiBusy && (
                  <div className="mt-2 flex items-center gap-2 text-xs text-violet-700">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500" />
                    Thinking… interpreting your question.
                  </div>
                )}
                {aiRes && !aiBusy && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    {aiRes.in_window === false ? (
                      <>
                        <span className="rounded bg-amber-100 px-2 py-0.5 font-medium text-amber-700">⚠ {aiRes.suggested_window?.label ?? "that window"} isn’t in the loaded run</span>
                        <button onClick={rescanSuggested} className="rounded-lg bg-amber-600 px-2.5 py-1 font-medium text-white hover:bg-amber-700">↻ Analyze {fmtWindow(aiRes.suggested_window)}</button>
                      </>
                    ) : (
                      <>
                        <span className="rounded-full bg-violet-100 px-2 py-0.5 font-medium text-violet-700">{aiRes.match_count ?? 0} match{(aiRes.match_count ?? 0) === 1 ? "" : "es"}</span>
                        {aiRes.spec?.time_window && <span className="rounded-full bg-sky-100 px-2 py-0.5 text-sky-700">📅 {aiRes.spec.time_window.label}</span>}
                        {aiRes.explanation && <span className="text-gray-500">{aiRes.explanation}</span>}
                      </>
                    )}
                    <button onClick={clearAi} className="rounded border px-2 py-0.5 text-gray-500 hover:bg-white">✕ clear</button>
                  </div>
                )}
              </div>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <Filter label="Risk" value={fRisk} setValue={setFRisk} options={run.facets.risks} />
                <Filter label="Category" value={fCat} setValue={setFCat} options={run.facets.categories} />
                <Filter label="Actor" value={fActor} setValue={setFActor} options={run.facets.actors} />
                <Filter label="Type" value={fType} setValue={setFType} options={run.facets.resource_types} fmt={shortType} />
                <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search…" className="rounded border px-2 py-1 text-sm" />
                <span className="text-[11px] text-gray-400">{filtered.length} / {events.length}</span>
              </div>
              </>
            )}

            {tab === "summary" && <SummaryTab run={run} />}
            {tab === "timeline" && <TimelineTab events={filtered} onSelect={setSelected} />}
            {tab === "changes" && <AllChangesTab events={filtered} onSelect={setSelected} />}
            {tab === "risk" && <RiskTab run={run} onSelect={setSelected} />}
            {tab === "resources" && <ResourcesTab run={run} onSelectResource={(rid) => setSelected(events.find((e) => e.resourceId === rid) ?? null)} />}
            {tab === "actors" && <ActorsTab run={run} />}
            {tab === "diff" && <DiffTab events={filtered} />}
            {tab === "impact" && <ImpactTab run={run} />}
            {tab === "export" && <ExportTab run={run} />}
          </div>
        )}
      </div>

      {selected && <ChangeDrawer event={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function Filter({ label, value, setValue, options, fmt }: { label: string; value: string; setValue: (v: string) => void; options: string[]; fmt?: (s: string) => string }) {
  return (
    <select value={value} onChange={(e) => setValue(e.target.value)} className="rounded border px-2 py-1 text-xs">
      <option value="">{label}: all</option>
      {options.map((o) => <option key={o} value={o}>{fmt ? fmt(o) : o}</option>)}
    </select>
  );
}

// Does a (loaded) run belong to the current scope selection? Keeps auto-load from cross-loading a
// run from a different workload/subscription.
function belongsToScope(run: ChangeAnalysisRun, scopeKind: ScopeKind, workloadId: string, subId: string): boolean {
  return scopeKind === "workload" ? run.workloadId === workloadId : run.workloadId === `sub:${subId}`;
}

// --------------------------------------------------------------- live progress
const PHASE_LABEL: Record<string, string> = {
  start: "Starting", scope: "Resolving scope", collect: "Collecting changes from Azure",
  normalize: "Dissecting & classifying changes", ai: "AI analyzing changes", insights: "Building insights",
};
function AnalysisProgress({ progress }: { progress: ChangeProgress | null }) {
  const phase = progress?.phase || "start";
  const order = ["scope", "collect", "normalize", "ai", "insights"];
  const idx = order.indexOf(phase);
  const pct = progress?.total ? Math.round(((progress.done ?? 0) / Math.max(1, progress.total)) * 100) : undefined;
  return (
    <div className="mt-3 rounded-lg border-2 border-brand/50 bg-brand/5 p-3 shadow-[0_0_14px_rgba(31,111,235,0.25)]">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 animate-blink-bright rounded-full bg-brand shadow-[0_0_8px_2px_rgba(31,111,235,0.7)]" />
        <span className="animate-blink-bright text-sm font-semibold text-brand">{progress?.message || "Analyzing changes…"}</span>
        <span className="ml-auto text-[11px] text-gray-400">runs in the background — safe to navigate away</span>
      </div>

      {/* Bright, actively-moving status bar — shows it's progressing right now. A determinate fill
          is shown when a phase reports a percentage (AI batches), else an indeterminate sweep. */}
      <div className="relative mt-2 h-2 w-full overflow-hidden rounded-full bg-brand/15">
        {pct !== undefined ? (
          <div className="h-full rounded-full bg-brand shadow-[0_0_10px_2px_rgba(31,111,235,0.7)] transition-all duration-300 animate-blink-bright"
            style={{ width: `${Math.max(6, pct)}%` }} />
        ) : (
          <div className="absolute top-0 h-full rounded-full bg-brand shadow-[0_0_10px_2px_rgba(31,111,235,0.75)] animate-indeterminate" />
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {order.map((p, i) => (
          <span key={p} className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] ${i < idx ? "bg-emerald-100 text-emerald-700" : i === idx ? "animate-blink-bright bg-brand/20 font-semibold text-brand" : "bg-gray-100 text-gray-400"}`}>
            {i < idx ? "✓" : i === idx ? "●" : "○"} {PHASE_LABEL[p]}
            {p === "ai" && i === idx && pct !== undefined ? ` ${pct}%` : ""}
          </span>
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------- history grid
function HistoryGrid({ runs, trashed, currentRunId, onLoad, onDelete, onRestore, onPurge }: {
  runs: ChangeRunSummary[]; trashed: ChangeRunSummary[]; currentRunId?: string;
  onLoad: (id: string) => void; onDelete: (id: string) => void; onRestore: (id: string) => void; onPurge: (id: string) => void;
}) {
  const [showTrash, setShowTrash] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState("");
  const rows = showTrash ? trashed : runs;
  return (
    <div className="mt-3 overflow-x-auto rounded-lg border bg-white">
      <div className="flex items-center gap-2 border-b px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400">{showTrash ? "Trash (deleted runs)" : "Previous runs (cached)"}</span>
        <div className="ml-auto inline-flex overflow-hidden rounded-md border text-[10px]">
          <button onClick={() => setShowTrash(false)} className={`px-2 py-0.5 ${!showTrash ? "bg-brand text-white" : "bg-white text-gray-500 hover:bg-gray-50"}`}>History ({runs.length})</button>
          <button onClick={() => setShowTrash(true)} className={`px-2 py-0.5 ${showTrash ? "bg-brand text-white" : "bg-white text-gray-500 hover:bg-gray-50"}`}>🗑 Trash ({trashed.length})</button>
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-center text-[11px] text-gray-400">{showTrash ? "Trash is empty." : "No previous runs for this scope yet."}</p>
      ) : (
      <table className="w-full text-xs">
        <thead className="text-left text-[10px] uppercase text-gray-400">
          <tr>
            <th className="px-3 py-1.5">Workload / scope</th><th className="px-2">Mode</th><th className="px-2">Window</th>
            <th className="px-2 text-right">Changes</th><th className="px-2">Risk</th><th className="px-2">{showTrash ? "Deleted" : "Run at"}</th><th className="px-2" />
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 12).map((r) => (
            <tr key={r.runId} className={`border-t ${r.runId === currentRunId ? "bg-brand/5" : "hover:bg-gray-50"}`}>
              <td className="px-3 py-1.5 font-medium text-gray-700">{r.workloadName}{r.demo && <span className="ml-1 rounded bg-gray-100 px-1 text-[9px] text-gray-500">demo</span>}</td>
              <td className="px-2 text-gray-500">{r.scopeMode.replace("_", " + ")}</td>
              <td className="px-2 text-[10px] text-gray-500">
                {fmtShort(r.startTime)} → {fmtShort(r.endTime)}
                <span className="ml-1 rounded bg-gray-100 px-1 text-[9px] text-gray-500">{fmtDuration(r.startTime, r.endTime)}</span>
              </td>
              <td className="px-2 text-right tabular-nums">{r.totalChanges}</td>
              <td className="px-2">
                {r.criticalCount > 0 && <span className="mr-0.5 rounded bg-red-100 px-1 text-[9px] text-red-700">{r.criticalCount}C</span>}
                {r.highCount > 0 && <span className="mr-0.5 rounded bg-orange-100 px-1 text-[9px] text-orange-700">{r.highCount}H</span>}
                {r.mediumCount > 0 && <span className="rounded bg-amber-100 px-1 text-[9px] text-amber-700">{r.mediumCount}M</span>}
              </td>
              <td className="px-2 text-[10px] text-gray-500">
                {fmtShort(showTrash ? (r.deleted_at || "") : r.completedAt)}
                <span className="ml-1 text-gray-400">{fmtAgo(showTrash ? (r.deleted_at || "") : r.completedAt)}</span>
              </td>
              <td className="px-2 text-right whitespace-nowrap">
                {showTrash ? (
                  confirmPurge === r.runId ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="text-[9px] text-red-600">Delete forever?</span>
                      <button onClick={() => { onPurge(r.runId); setConfirmPurge(""); }} className="rounded bg-red-600 px-1.5 py-0.5 text-[10px] text-white">Yes</button>
                      <button onClick={() => setConfirmPurge("")} className="rounded border px-1.5 py-0.5 text-[10px] text-gray-500">No</button>
                    </span>
                  ) : (
                    <>
                      <button onClick={() => onRestore(r.runId)} className="rounded border px-1.5 py-0.5 text-[10px] text-emerald-700 hover:bg-emerald-50">Restore</button>
                      <button onClick={() => setConfirmPurge(r.runId)} className="ml-1 rounded border px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-red-600" title="Delete permanently">Delete forever</button>
                    </>
                  )
                ) : (
                  <>
                    <button onClick={() => onLoad(r.runId)} className="rounded border px-1.5 py-0.5 text-[10px] text-brand hover:bg-brand/5">{r.runId === currentRunId ? "Loaded" : "Load"}</button>
                    <button onClick={() => onDelete(r.runId)} className="ml-1 rounded border px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-red-600" title="Move to trash">🗑</button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}
    </div>
  );
}
function fmtShort(iso: string): string {
  if (!iso) return "—";
  try { const d = new Date(iso); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; } catch { return iso; }
}
// Compact relative age, e.g. "just now", "1m", "2h", "3day".
function fmtAgo(iso: string): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "";
  const s = Math.floor(ms / 1000);
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}day${d === 1 ? "" : "s"} ago`;
}
// Compact span of a time window, e.g. "45m", "3h 2m", "7d 4h".
function fmtDuration(startIso: string, endIso: string): string {
  if (!startIso || !endIso) return "";
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (Number.isNaN(ms) || ms <= 0) return "";
  const mins = Math.round(ms / 60000);
  const d = Math.floor(mins / 1440);
  const h = Math.floor((mins % 1440) / 60);
  const m = mins % 60;
  if (d > 0) return h > 0 ? `${d}d ${h}h` : `${d}d`;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return `${m}m`;
}

// A collector note that signals a source couldn't be queried (vs. an informational note like a
// demo/truncation message) — drives the prominent amber "sources unavailable" banner.
function isProblemNote(n: string): boolean {
  return /unavailable|not reachable|isn['’]t reachable|access denied|denied|unauthor|forbidden|not recognized|failed|isn['’]t signed in|no access|lacks (read )?permission/i.test(n || "");
}

// --------------------------------------------------------------- Summary
function Kpi({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-xl border bg-white p-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone || "text-gray-900"}`}>{value}</div>
    </div>
  );
}

function SummaryTab({ run }: { run: ChangeAnalysisRun }) {
  const h = run.headline;
  const donut = [
    { name: "Critical", value: run.criticalCount }, { name: "High", value: run.highCount },
    { name: "Medium", value: run.mediumCount }, { name: "Low", value: run.lowCount },
    { name: "Informational", value: run.informationalCount },
  ].filter((d) => d.value > 0);
  // Split collector notes: "problems" (a source couldn't be queried — access/tenant/denied) get a
  // prominent amber banner so an empty result isn't mistaken for "succeeded, no changes".
  // The change-limit note is dropped here — it's shown by the dedicated banner on every tab.
  const notes = (run.notes ?? []).filter((n) => !/capped at the/i.test(n));
  const problems = notes.filter(isProblemNote);
  const infoNotes = notes.filter((n) => !isProblemNote(n));
  const emptyButProblem = run.totalChanges === 0 && problems.length > 0;
  return (
    <div className="space-y-4">
      {problems.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <div className="font-medium">⚠ Some change sources couldn’t be queried</div>
          {problems.map((n, i) => <div key={i} className="mt-0.5">• {n}</div>)}
          <div className="mt-1 text-[11px] text-amber-700">Tip: confirm the <b>Azure tenant</b> picker is set to the connection that owns this scope, then Analyze again.</div>
        </div>
      )}
      {infoNotes.length > 0 && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-700">
          {infoNotes.map((n, i) => <div key={i}>• {n}</div>)}
        </div>
      )}
      {run.totalChanges === 0 && (
        <div className="rounded-xl border border-dashed bg-gray-50 p-6 text-center">
          <div className="text-2xl">{emptyButProblem ? "🔌" : "✅"}</div>
          <p className="mt-1 text-sm font-medium text-gray-700">
            {emptyButProblem
              ? "No changes returned — the change sources above couldn’t be queried for this connection/scope."
              : "No changes found in this time window for this scope."}
          </p>
          <p className="mt-1 text-xs text-gray-500">
            {emptyButProblem
              ? "This usually means the selected Azure tenant connection can’t reach the chosen subscription. Pick the owning connection and Analyze again."
              : "Try a wider time range, a broader scope (Workload + dependencies / Tenant-wide), or a different connection."}
          </p>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Kpi label="Total changes" value={run.totalChanges} />
        <Kpi label="Critical" value={run.criticalCount} tone="text-red-600" />
        <Kpi label="High" value={run.highCount} tone="text-orange-600" />
        <Kpi label="Medium" value={run.mediumCount} tone="text-amber-600" />
        <Kpi label="Low" value={run.lowCount} tone="text-blue-600" />
        <Kpi label="Informational" value={run.informationalCount} tone="text-gray-500" />
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-3">
          <div className="rounded-xl border bg-gradient-to-br from-violet-50 to-white p-4">
            <div className="text-sm font-medium text-gray-800">Plain-English summary</div>
            <p className="mt-1 text-sm text-gray-700">{run.summary}</p>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Kpi label="Resources changed" value={h.resources_changed} />
            <Kpi label="Unique actors" value={h.unique_actors} />
            <Kpi label="Most active actor" value={<span className="text-base">{h.most_active_actor || "—"}</span>} />
            <Kpi label="Most changed type" value={<span className="text-sm">{shortType(h.most_changed_resource_type) || "—"}</span>} />
            <Kpi label="Most risky category" value={<span className="text-base">{h.most_risky_category || "—"}</span>} />
            <Kpi label="Scope" value={<span className="text-sm capitalize">{run.scopeMode.replace("_", " + ")}</span>} />
          </div>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-1 text-sm font-medium text-gray-700">Risk distribution</div>
          {donut.length ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={donut} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80} paddingAngle={2}>
                  {donut.map((d) => <Cell key={d.name} fill={RISK_COLOR[d.name]} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : <p className="text-xs text-gray-400">No changes.</p>}
          <div className="mt-1 flex flex-wrap gap-2">
            {donut.map((d) => <span key={d.name} className="flex items-center gap-1 text-[11px] text-gray-600"><span className="h-2 w-2 rounded-full" style={{ background: RISK_COLOR[d.name] }} />{d.name} {d.value}</span>)}
          </div>
        </div>
      </div>
      {run.insights.length > 0 && (
        <div className="rounded-xl border bg-white">
          <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">Insights — review these first</div>
          <div className="divide-y">
            {run.insights.map((i) => (
              <div key={i.insightId} className="flex items-start gap-2 px-4 py-2">
                <RiskChip label={i.severity} />
                <div><div className="text-sm font-medium text-gray-800">{i.title}</div><div className="text-[12px] text-gray-500">{i.summary}</div></div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------- Timeline
function TimelineTab({ events, onSelect }: { events: ChangeEvent[]; onSelect: (e: ChangeEvent) => void }) {
  if (!events.length) return <Empty />;
  return (
    <div className="relative space-y-2 border-l-2 border-gray-100 pl-4">
      {events.map((e) => (
        <button key={e.changeId} onClick={() => onSelect(e)} className="block w-full rounded-lg border bg-white p-2 text-left hover:bg-gray-50">
          <div className="absolute -left-[7px] mt-1 h-3 w-3 rounded-full" style={{ background: RISK_COLOR[e.riskLabel] }} />
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs tabular-nums text-gray-400">{fmtTime(e.eventTime)}</span>
            <RiskChip label={e.riskLabel} score={e.riskScore} />
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{e.category}</span>
            <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
            <span className="text-[11px] text-gray-400">{e.actor}</span>
          </div>
          <div className="mt-0.5 text-[12px] text-gray-600">{e.plainEnglishSummary}</div>
        </button>
      ))}
    </div>
  );
}

// --------------------------------------------------------------- All Changes
function AllChangesTab({ events, onSelect }: { events: ChangeEvent[]; onSelect: (e: ChangeEvent) => void }) {
  if (!events.length) return <Empty />;
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-gray-50 text-left text-[11px] uppercase text-gray-400">
          <tr>
            <th className="px-3 py-2">Time</th><th className="px-2">Risk</th><th className="px-2">Category</th>
            <th className="px-2">Resource</th><th className="px-2">Type</th><th className="px-2">RG</th>
            <th className="px-2">Operation</th><th className="px-2">Actor</th><th className="px-2">Source</th><th className="px-2">Conf.</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.changeId} onClick={() => onSelect(e)} className="cursor-pointer border-t hover:bg-gray-50">
              <td className="px-3 py-1.5 text-[11px] tabular-nums text-gray-500">{fmtTime(e.eventTime)}</td>
              <td className="px-2"><RiskChip label={e.riskLabel} score={e.riskScore} /></td>
              <td className="px-2 text-[11px] text-gray-600">{e.category}</td>
              <td className="px-2 font-medium text-gray-800">{e.resourceName}</td>
              <td className="px-2 text-[11px] text-gray-500">{shortType(e.resourceType)}</td>
              <td className="px-2 text-[11px] text-gray-500">{e.resourceGroup}</td>
              <td className="px-2 text-[11px] text-gray-500">{e.operation.split("/").slice(-2).join("/")}</td>
              <td className="px-2 text-[11px] text-gray-600">{e.actor} <span className="text-gray-300">({e.actorType})</span></td>
              <td className="px-2 text-[11px] text-gray-400">{e.source}</td>
              <td className="px-2 text-[11px] text-gray-400">{e.confidence}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------- Risk Insights
function RiskTab({ run, onSelect }: { run: ChangeAnalysisRun; onSelect: (e: ChangeEvent) => void }) {
  const byCat = useMemo(() => rollup(run.events, (e) => e.category), [run.events]);
  const byActor = useMemo(() => rollup(run.events, (e) => e.actor), [run.events]);
  const top = [...run.events].sort((a, b) => b.riskScore - a.riskScore).slice(0, 10);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium text-gray-700">Risk by category (max score)</div>
          <ResponsiveContainer width="100%" height={Math.max(120, byCat.length * 30)}>
            <BarChart layout="vertical" data={byCat} margin={{ left: 20, right: 20 }}>
              <XAxis type="number" domain={[0, 100]} fontSize={11} /><YAxis type="category" dataKey="key" width={100} fontSize={11} />
              <Tooltip /><Bar dataKey="max" radius={[0, 4, 4, 0]}>{byCat.map((d, i) => <Cell key={i} fill={RISK_COLOR[labelFor(d.max)]} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium text-gray-700">Risk by actor (max score)</div>
          <ResponsiveContainer width="100%" height={Math.max(120, byActor.length * 30)}>
            <BarChart layout="vertical" data={byActor} margin={{ left: 20, right: 20 }}>
              <XAxis type="number" domain={[0, 100]} fontSize={11} /><YAxis type="category" dataKey="key" width={120} fontSize={11} />
              <Tooltip /><Bar dataKey="max" radius={[0, 4, 4, 0]}>{byActor.map((d, i) => <Cell key={i} fill={RISK_COLOR[labelFor(d.max)]} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">Highest-risk changes — why they're risky</div>
        <div className="divide-y">
          {top.map((e) => (
            <button key={e.changeId} onClick={() => onSelect(e)} className="block w-full px-4 py-2 text-left hover:bg-gray-50">
              <div className="flex flex-wrap items-center gap-2">
                <RiskChip label={e.riskLabel} score={e.riskScore} />
                <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{e.category}</span>
              </div>
              <div className="mt-0.5 text-[12px] text-gray-600">{e.whyRisk}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {e.riskFactors.map((f, i) => (
                  <span key={i} className={`rounded px-1.5 py-0.5 text-[10px] ${f.delta >= 0 ? "bg-red-50 text-red-600" : "bg-emerald-50 text-emerald-600"}`}>
                    {f.label} {f.delta >= 0 ? "+" : ""}{f.delta}
                  </span>
                ))}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- Resources
function ResourcesTab({ run, onSelectResource }: { run: ChangeAnalysisRun; onSelectResource: (rid: string) => void }) {
  if (!run.resources.length) return <Empty />;
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400">
          <tr><th className="px-3 py-2">Resource</th><th className="px-2">Type</th><th className="px-2">RG</th><th className="px-2 text-right">Changes</th><th className="px-2">Highest risk</th><th className="px-2">Last changed</th><th className="px-2">Last actor</th><th className="px-2">Role</th></tr>
        </thead>
        <tbody>
          {run.resources.map((r) => (
            <tr key={r.resourceId} onClick={() => onSelectResource(r.resourceId)} className="cursor-pointer border-t hover:bg-gray-50">
              <td className="px-3 py-1.5 font-medium text-gray-800">{r.resourceName}</td>
              <td className="px-2 text-[11px] text-gray-500">{shortType(r.resourceType)}</td>
              <td className="px-2 text-[11px] text-gray-500">{r.resourceGroup}</td>
              <td className="px-2 text-right tabular-nums">{r.changes}</td>
              <td className="px-2"><RiskChip label={r.highestRiskLabel} score={r.highestRiskScore} /></td>
              <td className="px-2 text-[11px] text-gray-500">{fmtTime(r.lastChanged)}</td>
              <td className="px-2 text-[11px] text-gray-600">{r.lastActor}</td>
              <td className="px-2 text-[11px] text-gray-500">{r.role}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------- Actors
function ActorsTab({ run }: { run: ChangeAnalysisRun }) {
  if (!run.actors.length) return <Empty />;
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400">
          <tr><th className="px-3 py-2">Actor</th><th className="px-2">Type</th><th className="px-2 text-right">Changes</th><th className="px-2">Highest risk</th><th className="px-2">Categories</th><th className="px-2 text-right">Resources</th><th className="px-2">First</th><th className="px-2">Last</th></tr>
        </thead>
        <tbody>
          {run.actors.map((a) => (
            <tr key={a.actor} className="border-t">
              <td className="px-3 py-1.5 font-medium text-gray-800">{a.actor}</td>
              <td className="px-2 text-[11px] text-gray-500">{a.actorType}</td>
              <td className="px-2 text-right tabular-nums">{a.changes}</td>
              <td className="px-2"><RiskChip label={a.highestRiskLabel} score={a.highestRiskScore} /></td>
              <td className="px-2 text-[11px] text-gray-500">{a.categories.join(", ")}</td>
              <td className="px-2 text-right tabular-nums">{a.resources}</td>
              <td className="px-2 text-[11px] text-gray-500">{fmtTime(a.firstChange)}</td>
              <td className="px-2 text-[11px] text-gray-500">{fmtTime(a.lastChange)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------- Technical Diff
function DiffTab({ events }: { events: ChangeEvent[] }) {
  if (!events.length) return <Empty />;
  return (
    <div className="space-y-3">
      {events.map((e) => (
        <div key={e.changeId} className="rounded-xl border bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <RiskChip label={e.riskLabel} score={e.riskScore} />
            <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
            <span className="text-[11px] text-gray-400">{e.operation}</span>
            <span className="ml-auto text-[11px] text-gray-400">corr: {e.correlationId || "—"}</span>
          </div>
          {e.details.length ? (
            <table className="mt-2 w-full text-[12px]">
              <thead className="text-left text-[10px] uppercase text-gray-400"><tr><th className="py-1">Property</th><th>Before</th><th>After</th><th>Type</th></tr></thead>
              <tbody>
                {e.details.map((d) => (
                  <tr key={d.detailId} className="border-t">
                    <td className="py-1 font-mono text-[11px] text-gray-700">{d.propertyPath}</td>
                    <td className="font-mono text-[11px] text-red-600">{String(d.beforeValue ?? "—")}</td>
                    <td className="font-mono text-[11px] text-emerald-700">{String(d.afterValue ?? "—")}</td>
                    <td className="text-[11px] text-gray-500">{d.changeType}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="mt-1 text-[11px] text-gray-400">No before/after diff available from the source.</div>}
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-gray-500">Raw event JSON · resourceId · subscriptionId · caller</summary>
            <div className="mt-1 text-[10px] text-gray-500">resourceId: {e.resourceId}</div>
            <div className="text-[10px] text-gray-500">subscriptionId: {e.subscriptionId} · caller: {e.actor} · source: {e.source}</div>
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-gray-900 p-2 text-[10px] text-emerald-300">{JSON.stringify(e.rawEventJson, null, 2)}</pre>
          </details>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------- Dependency Impact
function ImpactTab({ run }: { run: ChangeAnalysisRun }) {
  const byRole = useMemo(() => {
    const m = new Map<string, ChangeEvent[]>();
    for (const e of run.events) { const k = e.dependencyRole || "Workload resource"; (m.get(k) ?? m.set(k, []).get(k))!.push(e); }
    return [...m.entries()].sort((a, b) => Math.max(...b[1].map((e) => e.riskScore)) - Math.max(...a[1].map((e) => e.riskScore)));
  }, [run.events]);
  if (!run.events.length) return <Empty />;
  return (
    <div className="space-y-3">
      {byRole.map(([role, evs]) => (
        <div key={role} className="rounded-xl border bg-white p-4">
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-brand/10 px-2 py-0.5 text-xs font-medium text-brand">{role}</span>
            <span className="text-[11px] text-gray-400">{evs.length} change(s)</span>
          </div>
          <p className="mt-1 text-[12px] text-gray-600">{evs[0].blastRadius}</p>
          <div className="mt-2 flex flex-wrap gap-1">
            {[...new Set(evs.map((e) => e.resourceName))].map((n) => {
              const top = evs.filter((e) => e.resourceName === n).sort((a, b) => b.riskScore - a.riskScore)[0];
              return <span key={n} className="flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] text-gray-600"><span className="h-2 w-2 rounded-full" style={{ background: RISK_COLOR[top.riskLabel] }} />{n}</span>;
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------- Export
function ExportTab({ run }: { run: ChangeAnalysisRun }) {
  const [queries, setQueries] = useState<Record<string, string> | null>(null);
  async function ex(format: string) {
    const r = await api.changeExplorerExport(run.runId, format);
    if (r.queries) { setQueries(r.queries); return; }
    if (r.content && r.filename) download(r.filename, r.content, r.mime || "text/plain");
  }
  const items: [string, string, string][] = [
    ["csv", "⬇️ All changes (CSV)", "Every change as a spreadsheet."],
    ["csv_high", "⬇️ High-risk changes (CSV)", "Only Critical + High changes."],
    ["json", "⬇️ Full run (JSON)", "The complete ChangeAnalysisRun."],
    ["exec", "📄 Executive summary", "Plain-English report for leadership."],
    ["technical", "📄 Technical summary", "Per-change detail with diffs."],
    ["rca", "📄 RCA-style timeline", "High-risk changes as candidate root causes."],
    ["servicenow", "📄 ServiceNow change-review text", "Paste-ready review note."],
    ["queries", "🔎 Validation queries", "Read-only ARG / CLI / KQL to confirm current state."],
  ];
  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map(([fmt, label, desc]) => (
          <button key={fmt} onClick={() => ex(fmt)} className="rounded-xl border bg-white p-3 text-left hover:bg-gray-50">
            <div className="text-sm font-medium text-gray-800">{label}</div>
            <div className="text-[11px] text-gray-500">{desc}</div>
          </button>
        ))}
      </div>
      {queries && (
        <div className="space-y-2">
          {Object.entries(queries).map(([k, v]) => (
            <div key={k} className="rounded-xl border bg-white p-3">
              <div className="mb-1 text-[11px] font-medium uppercase text-gray-500">{k}</div>
              <pre className="max-h-48 overflow-auto rounded bg-gray-900 p-2 text-[11px] text-emerald-300">{v}</pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------- Drawer
function ChangeDrawer({ event: e, onClose }: { event: ChangeEvent; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="h-full w-full max-w-xl overflow-auto bg-white shadow-xl" onClick={(ev) => ev.stopPropagation()}>
        <div className="sticky top-0 flex items-center gap-2 border-b bg-white px-4 py-3">
          <RiskChip label={e.riskLabel} score={e.riskScore} />
          <span className="text-sm font-semibold text-gray-900">{e.resourceName}</span>
          <button onClick={onClose} className="ml-auto text-gray-400 hover:text-gray-700">✕</button>
        </div>
        <div className="space-y-3 p-4 text-sm">
          <Field label="What happened">{e.plainEnglishSummary}</Field>
          <Field label="Possible impact">{e.possibleImpact}</Field>
          <Field label="Why this risk score">{e.whyRisk}</Field>
          <div className="flex flex-wrap gap-1">
            {e.riskFactors.map((f, i) => <span key={i} className={`rounded px-1.5 py-0.5 text-[10px] ${f.delta >= 0 ? "bg-red-50 text-red-600" : "bg-emerald-50 text-emerald-600"}`}>{f.label} {f.delta >= 0 ? "+" : ""}{f.delta}</span>)}
          </div>
          <div className="grid grid-cols-2 gap-2 text-[12px]">
            <KV k="Category" v={e.category} /><KV k="Confidence" v={e.confidence} />
            <KV k="Operation" v={e.operation} /><KV k="Actor" v={`${e.actor} (${e.actorType})`} />
            <KV k="Source" v={e.source} /><KV k="Time" v={fmtTime(e.eventTime)} />
            <KV k="Resource group" v={e.resourceGroup} /><KV k="Subscription" v={e.subscriptionId} />
            <KV k="Correlation ID" v={e.correlationId || "—"} /><KV k="Role" v={e.dependencyRole} />
          </div>
          {e.details.length > 0 && (
            <Field label="Technical diff">
              <table className="w-full text-[12px]">
                <thead className="text-left text-[10px] uppercase text-gray-400"><tr><th>Property</th><th>Before</th><th>After</th></tr></thead>
                <tbody>{e.details.map((d) => <tr key={d.detailId} className="border-t"><td className="py-1 font-mono text-[11px]">{d.propertyPath}</td><td className="font-mono text-[11px] text-red-600">{String(d.beforeValue ?? "—")}</td><td className="font-mono text-[11px] text-emerald-700">{String(d.afterValue ?? "—")}</td></tr>)}</tbody>
              </table>
            </Field>
          )}
          <Field label="Blast radius (inferred)">{e.blastRadius}</Field>
          <details>
            <summary className="cursor-pointer text-[11px] text-gray-500">Raw event JSON</summary>
            <pre className="mt-1 max-h-60 overflow-auto rounded bg-gray-900 p-2 text-[10px] text-emerald-300">{JSON.stringify(e.rawEventJson, null, 2)}</pre>
          </details>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><div className="text-[11px] font-medium uppercase text-gray-400">{label}</div><div className="mt-0.5 text-gray-700">{children}</div></div>;
}
function KV({ k, v }: { k: string; v: string }) {
  return <div className="truncate"><span className="text-gray-400">{k}:</span> <span className="text-gray-700">{v}</span></div>;
}
function Empty() { return <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">No changes match.</div>; }

// helpers
function rollup(events: ChangeEvent[], keyFn: (e: ChangeEvent) => string): { key: string; max: number }[] {
  const m = new Map<string, number>();
  for (const e of events) { const k = keyFn(e) || "—"; m.set(k, Math.max(m.get(k) ?? 0, e.riskScore)); }
  return [...m.entries()].map(([key, max]) => ({ key, max })).sort((a, b) => b.max - a.max).slice(0, 12);
}
function labelFor(score: number): string {
  return score >= 90 ? "Critical" : score >= 70 ? "High" : score >= 40 ? "Medium" : score >= 10 ? "Low" : "Informational";
}
function pad(n: number): string { return String(n).padStart(2, "0"); }
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toIso(local: string): string { return local ? new Date(local).toISOString() : ""; }
function defaultEnd(): string { return toLocalInput(new Date()); }
function defaultStart(): string { return toLocalInput(new Date(Date.now() - 24 * 3600_000)); }
// Compact "Jun 22 → Jun 23" label for an out-of-window re-scan button.
function fmtWindow(w?: { start_iso: string; end_iso: string; label: string } | null): string {
  if (!w) return "";
  const o: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  return `${new Date(w.start_iso).toLocaleDateString(undefined, o)} → ${new Date(w.end_iso).toLocaleDateString(undefined, o)}`;
}
