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
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ResponsiveContainer, PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip,
} from "recharts";
import {
  api, streamChangeExplorerAnalyze, streamChangeExplorerAiEnrich,
  type ChangeAnalysisRun, type ChangeEvent, type ChangeProgress, type ChangeRunSummary, type ChangeAnalyzeBody,
  type ChangeAskResponse, type ChangeCompareResult,
} from "../api";
import { useNavigate } from "react-router-dom";
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
// Truncate a chart-axis label from the RIGHT (keep the start + ellipsis) so the leading,
// identifying characters of a name/email/object-id are always visible (recharts right-aligns
// category labels to the axis, otherwise clipping the START of long values).
function truncRight(s: string, max = 20): string {
  const v = String(s ?? "");
  return v.length > max ? `${v.slice(0, max - 1)}…` : v;
}
// recharts calls tickFormatter(value, index) — wrap so the index can't override `max`.
const axisTrunc = (v: string) => truncRight(v, 20);
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
  // AI analysis is the slowest phase, so it's OPT-IN per the "Perform AI analysis" checkbox.
  // Default OFF — the run completes fast (deterministic only), and the user runs AI on demand.
  const [runAi, setRunAi] = usePersistedState<boolean>("azsup.changeexp.runAi", false);
  const [shownRun, setShownRun] = useState<ChangeAnalysisRun | null>(null);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState<ChangeEvent | null>(null);
  const [resourceFocus, setResourceFocus] = useState<string | null>(null);   // A3 per-resource drill-in
  const [confirmTenant, setConfirmTenant] = useState(false);   // tenant-wide scope yes/no gate
  const navigate = useNavigate();
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

  async function loadRun(runId: string, syncSelectors = false) {
    setErr("");
    try {
      const r = await api.changeExplorerRun(runId);
      setShownRun(r); setSelected(null);
      // When the user EXPLICITLY loads a run from history, align the controls to that run's
      // window + scope so the selectors never contradict what's on screen (B3). Auto-load on
      // first arrival does NOT sync — the mismatch banner flags any discrepancy instead.
      if (syncSelectors && r.startTime && r.endTime) {
        setStart(toLocalInput(new Date(r.startTime)));
        setEnd(toLocalInput(new Date(r.endTime)));
        if (r.scopeMode) setScopeMode(r.scopeMode);
        setRangeLabel(`Run window (${fmtTime(r.startTime)} → ${fmtTime(r.endTime)})`);
      }
    }
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

  function analyze(confirmed = false, windowOverride?: { startIso: string; endIso: string }) {
    if (!scopeReady) { setErr(scopeKind === "workload" ? "Pick a workload first." : "Pick a subscription first."); return; }
    // Tenant-wide is a heavy, broad scan — require an explicit yes/no first.
    if (scopeMode === "tenant" && !confirmed) { setConfirmTenant(true); return; }
    setConfirmTenant(false);
    setErr(""); setSelected(null);
    // An explicit window override (e.g. from the timeline "Narrow search" popover) wins over the
    // current control state, avoiding a stale-closure race where setState hasn't flushed yet.
    const startTime = windowOverride?.startIso ?? toIso(start);
    const endTime = windowOverride?.endIso ?? toIso(end);
    const body: ChangeAnalyzeBody = scopeKind === "workload"
      ? { workload_id: effWorkloadId, connection_id: connId, start_time: startTime, end_time: endTime, scope_mode: scopeMode, run_ai: runAi }
      : { subscription_id: subId, subscription_name: subName, connection_id: connId, start_time: startTime, end_time: endTime, scope_mode: scopeMode, run_ai: runAi };
    startAnalysis(scopeKey, body);
    // Refresh history shortly after the run persists.
    setTimeout(() => void runsQ.refetch(), 1500);
  }

  // When a background run for this scope completes, refresh the history grid.
  useEffect(() => { if (bgResult) void runsQ.refetch(); /* eslint-disable-next-line */ }, [bgResult]);

  // ---- On-demand AI enrichment of an already-analyzed run -----------------------------------
  // Used by BOTH the "Run AI analysis" button and auto-triggered when a record is opened in a
  // grid (since the detail drawer shows AI-sharpened narrative). Streams progress; on done it
  // swaps in the enriched run and refreshes history. Guarded so it runs at most once per run.
  const [aiEnriching, setAiEnriching] = useState(false);
  const [aiEnrichProgress, setAiEnrichProgress] = useState<ChangeProgress | null>(null);
  const aiAbortRef = useRef<AbortController | null>(null);
  const aiTriggeredRef = useRef<string>("");   // runId we've already kicked AI for (avoids re-trigger)

  async function runAiAnalysis() {
    const r = shownRun;
    if (!r || r.aiAnalyzed || r.demo || (r.totalChanges ?? 0) === 0 || aiEnriching) return;
    if (aiTriggeredRef.current === r.runId) return;
    aiTriggeredRef.current = r.runId;
    setAiEnriching(true); setAiEnrichProgress({ phase: "ai", message: "Starting AI analysis…" });
    const abort = new AbortController(); aiAbortRef.current = abort;
    try {
      await streamChangeExplorerAiEnrich(r.runId, {
        onProgress: (p) => setAiEnrichProgress(p),
        onDone: (updated) => {
          setShownRun(updated); _lastResult.set(scopeKey, updated); void runsQ.refetch();
          // Re-point an open drawer at the freshly-enriched event so its AI narrative appears.
          setSelected((cur) => cur ? (updated.events.find((e) => e.changeId === cur.changeId) ?? cur) : cur);
        },
        onError: (msg) => setErr(msg),
      }, abort.signal);
    } catch (e) { if ((e as Error)?.name !== "AbortError") setErr(formatError(e)); }
    finally { setAiEnriching(false); setAiEnrichProgress(null); aiAbortRef.current = null; }
  }

  // Open a change record (detail drawer). Auto-kicks AI enrichment if it hasn't run for this run,
  // so the drawer's AI narrative is populated — exactly the "open a record" trigger.
  function openRecord(e: ChangeEvent | null) {
    setSelected(e);
    if (e) {
      void runAiAnalysis();
      // Deep-link the open change (U6) so a shared URL re-opens it.
      try { const u = new URL(window.location.href); u.searchParams.set("change", e.changeId); window.history.replaceState(null, "", u); } catch { /* ignore */ }
    }
  }
  function clearChangeDeepLink() {
    try { const u = new URL(window.location.href); u.searchParams.delete("change"); window.history.replaceState(null, "", u); } catch { /* ignore */ }
  }

  // Cancel an in-flight AI enrich if the displayed run changes.
  useEffect(() => { return () => aiAbortRef.current?.abort(); }, [shownRun?.runId]); // eslint-disable-line

  // ---- Case file (D1): pinned change ids + per-change notes, persisted server-side ----------
  const pinnedIds = useMemo(() => new Set(shownRun?.caseFile?.pinned ?? []), [shownRun?.caseFile?.pinned]);
  async function persistCase(patch: { pinned?: string[]; notes?: Record<string, string>; case_summary?: string }) {
    const r = shownRun;
    if (!r) return;
    try {
      const res = await api.changeExplorerSetCase(r.runId, patch);
      setShownRun((cur) => cur && cur.runId === r.runId ? { ...cur, caseFile: res.caseFile } : cur);
    } catch (e) { setErr(formatError(e)); }
  }
  function togglePin(changeId: string) {
    const cur = new Set(shownRun?.caseFile?.pinned ?? []);
    if (cur.has(changeId)) cur.delete(changeId); else cur.add(changeId);
    void persistCase({ pinned: [...cur] });
  }
  function saveNote(changeId: string, text: string) {
    void persistCase({ notes: { [changeId]: text } });
  }

  // ---- Hand-off to Deep Investigation (D2): seed a War Room chat with this change ----------
  function investigateChange(e: ChangeEvent) {
    try {
      const prompt = `Investigate this Azure change from the Change Explorer:\n`
        + `Resource: ${e.resourceName} (${e.resourceType})\n`
        + `Operation: ${e.operation}\nActor: ${e.actorDisplay || e.actor}\nWhen: ${e.eventTime}\n`
        + `Risk: ${e.riskLabel} (${e.riskScore})\n${e.plainEnglishSummary}\n`
        + (e.securityFlags?.length ? `Security flags: ${e.securityFlags.map((f) => f.label).join(", ")}\n` : "")
        + `Resource id: ${e.resourceId}`;
      sessionStorage.setItem("azsup.warRoomHandoff", JSON.stringify({ workloadId: effWorkloadId || undefined, prompt }));
    } catch { /* ignore */ }
    navigate("/chat");
  }

  const run = shownRun;
  const events = run?.events ?? [];
  const debouncedSearch = useDebounced(search, 150);
  const filtered = useMemo(() => {
    const q = debouncedSearch.toLowerCase();
    return events.filter((e) =>
      (!fRisk || e.riskLabel === fRisk) && (!fCat || e.category === fCat) &&
      (!fActor || e.actor === fActor) && (!fType || e.resourceType === fType) &&
      (!aiMatchIds || aiMatchIds.has(e.changeId)) &&
      (!q || `${e.resourceName} ${e.plainEnglishSummary} ${e.operation}`.toLowerCase().includes(q)));
  }, [events, fRisk, fCat, fActor, fType, debouncedSearch, aiMatchIds]);

  // Open a deep-linked change (?change=<id>) once its run is loaded (U6).
  useEffect(() => {
    if (!run) return;
    try {
      const id = new URL(window.location.href).searchParams.get("change");
      if (id && (!selected || selected.changeId !== id)) {
        const e = run.events.find((x) => x.changeId === id);
        if (e) setSelected(e);
      }
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.runId]);

  // STALE-RUN GUARD: a loaded cached run can cover a DIFFERENT window/scope than what the controls
  // currently say (e.g. a 2-day-old run while "Last 4 hours" is selected) — dangerously misleading
  // on a forensic screen. Detect a mismatch so we can warn + offer a one-click re-analyze.
  const windowMismatch = useMemo(() => {
    if (!run) return null;
    const selStart = Date.parse(toIso(start)), selEnd = Date.parse(toIso(end));
    const runStart = Date.parse(run.startTime), runEnd = Date.parse(run.endTime);
    if ([selStart, selEnd, runStart, runEnd].some((n) => Number.isNaN(n))) return null;
    const TOL = 90_000; // 90s tolerance for rounding between selection and persisted run
    const winDiffers = Math.abs(selStart - runStart) > TOL || Math.abs(selEnd - runEnd) > TOL;
    const scopeDiffers = !!run.scopeMode && run.scopeMode !== scopeMode;
    if (!winDiffers && !scopeDiffers) return null;
    return { winDiffers, scopeDiffers };
  }, [run, start, end, scopeMode]);


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
    analyze(false, { startIso: new Date(w.start_iso).toISOString(), endIso: new Date(w.end_iso).toISOString() });
  }

  // Narrow the WHOLE search to the brushed timeline window: set the time range to the slider's
  // selection, then re-run the analysis so every source/tab reflects only that window (not just a
  // client-side filter). Used by the "Narrow search to this window" popover on the Timeline tab.
  function narrowToWindow(startMs: number, endMs: number) {
    const s = new Date(startMs), e = new Date(endMs);
    setStart(toLocalInput(s));
    setEnd(toLocalInput(e));
    setRangeLabel(`${s.toLocaleString()} → ${e.toLocaleString()}`);
    clearAi();
    // Pass the window explicitly so the new search uses the brushed range immediately (the
    // setStart/setEnd above won't have flushed into `analyze`'s closure yet).
    analyze(false, { startIso: s.toISOString(), endIso: e.toISOString() });
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
          <label className="flex h-[29px] cursor-pointer select-none items-center gap-1.5 self-end text-xs text-gray-600" title="AI analysis is the slowest phase. Leave off for a fast deterministic run; you can run AI later from the result, or it runs automatically when you open a change record.">
            <input type="checkbox" checked={runAi} onChange={(e) => setRunAi(e.target.checked)} className="h-3.5 w-3.5 rounded border-gray-300" />
            <span>✨ Perform AI analysis</span>
          </label>
          <button onClick={() => analyze()} disabled={analyzing} className="self-end rounded-lg bg-gray-900 px-4 py-1.5 text-sm text-white disabled:opacity-50">
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
            onLoad={(id) => loadRun(id, true)} onDelete={deleteRun} onRestore={restoreRun} onPurge={purgeRun} />
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
          analyzing ? (
            <div className="px-5 pb-10">
              <div className="mb-3 flex items-center gap-2 rounded-lg border border-brand/20 bg-brand/5 px-3 py-2 text-sm text-brand">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-brand" /> Analyzing changes… progress is shown above and continues even if you navigate away.
              </div>
              <Skeleton rows={10} />
            </div>
          ) : (
          <div className="m-6 rounded-xl border border-dashed bg-gray-50 p-10 text-center">
            <div className="text-3xl">🧭</div>
            <p className="mt-2 text-sm font-medium text-gray-700">Pick a workload or subscription, a time range and scope, then click <b>Analyze Changes</b>.</p>
            <p className="mt-1 text-xs text-gray-500">Cached runs auto-load here; new analysis only runs when you click Analyze. Tip: the <b>Contoso Website Prod (demo)</b> workload has built-in sample data — no Azure connection needed.</p>
          </div>
          )
        ) : (
          <div className="px-5 pb-10">
            {/* STALE-RUN banner — the loaded run covers a different window/scope than the current
                selection. The #1 trap on a forensic screen: a 2-day-old run masquerading as fresh. */}
            {windowMismatch && (
              <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-400 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <span className="text-sm">🕒</span>
                <span>
                  You’re viewing a <b>cached run</b> for <b>{fmtTime(run.startTime)} → {fmtTime(run.endTime)}</b>
                  {windowMismatch.scopeDiffers && <> (scope: <b>{run.scopeMode}</b>)</>}
                  , not your current selection <b>{fmtTime(toIso(start))} → {fmtTime(toIso(end))}</b>
                  {windowMismatch.scopeDiffers && <> (scope: <b>{scopeMode}</b>)</>}.
                </span>
                <button
                  onClick={() => analyze()}
                  disabled={analyzing}
                  className="ml-auto rounded-lg bg-amber-600 px-2.5 py-1 font-medium text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  ↻ Re-analyze current selection
                </button>
              </div>
            )}
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
            {/* SOURCE-FAILURE banner — shown on EVERY tab (not just Summary) when a change source
                couldn't be queried (expired/missing token, wrong tenant, denied). Critical: a tab
                like Technical Diff or Actors otherwise shows an empty "0 results" with NO hint that
                the result is empty because the QUERY FAILED, not because nothing changed. */}
            {(() => {
              const problems = (run.notes ?? []).filter(isSourceProblemNote);
              if (problems.length === 0) return null;
              return (
                <div className="mb-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800">
                  <div className="flex items-center gap-2 font-medium">
                    <span className="text-sm">⛔</span>
                    <span>Change sources couldn’t be queried — results may be incomplete or empty.</span>
                    <button
                      onClick={() => analyze()}
                      disabled={analyzing}
                      className="ml-auto rounded-lg bg-red-600 px-2.5 py-1 font-medium text-white hover:bg-red-700 disabled:opacity-50"
                    >
                      ↻ Re-analyze
                    </button>
                  </div>
                  {problems.map((n, i) => <div key={i} className="mt-0.5 pl-6">• {n}</div>)}
                  <div className="mt-1 pl-6 text-[11px] text-red-600">
                    If a pasted token expired, refresh it on the connection (Settings → Azure connections), or pick the connection that owns this scope, then Re-analyze.
                  </div>
                </div>
              );
            })()}
            {/* AI-ANALYSIS status — shown on EVERY tab when the run was analyzed WITHOUT AI. Offers
                a one-click "Run AI analysis"; also auto-runs when a change record is opened. While
                running, shows live progress. */}
            {!run.demo && (run.totalChanges ?? 0) > 0 && !run.aiAnalyzed && (
              aiEnriching ? (
                <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-violet-300 bg-violet-50 px-3 py-2 text-xs text-violet-800">
                  <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500" />
                  <span><b>✨ AI analysis running…</b> {aiEnrichProgress?.message ?? "Analyzing changes"}{typeof aiEnrichProgress?.done === "number" && typeof aiEnrichProgress?.total === "number" ? ` (${aiEnrichProgress.done}/${aiEnrichProgress.total})` : ""}</span>
                </div>
              ) : (
                <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-800">
                  <span className="text-sm">✨</span>
                  <span>This run was analyzed <b>without AI</b> (deterministic risk + classification only). Run AI to sharpen narratives, resolve “Unknown” categories and refine risk.</span>
                  <button onClick={() => void runAiAnalysis()} className="ml-auto rounded-lg bg-violet-600 px-2.5 py-1 font-medium text-white hover:bg-violet-700">✨ Run AI analysis</button>
                </div>
              )
            )}
            {/* Result filters (shared across tabs that list changes) */}
            {["timeline", "changes", "diff"].includes(tab) && (
              <>
              {/* ✨ Ask AI — natural-language change search ("show me all VMs modified yesterday").
                  Composes with the manual filters below by narrowing to the matched change ids. */}
              <div className="mb-3 rounded-xl border bg-gradient-to-br from-violet-50 to-white p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-base">✨</span>
                  <span className="text-sm font-medium text-gray-800">Ask AI</span>
                  <span className="text-[11px] font-semibold text-violet-700">filters &amp; narrows the current results</span>
                  <span className="text-[11px] text-gray-400">— natural-language search; type a window too (e.g. “yesterday”, “last 7 days”)</span>
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
                <PerspectiveBar
                  current={{ fRisk, fCat, fActor, fType, search }}
                  onApply={(p) => { setFRisk(p.fRisk); setFCat(p.fCat); setFActor(p.fActor); setFType(p.fType); setSearch(p.search); }}
                />
              </div>
              </>
            )}

            {tab === "summary" && <SummaryTab run={run} />}
            {tab === "operations" && <OperationsTab run={run} onSelect={openRecord} />}
            {tab === "narrative" && <NarrativeTab run={run} onSelect={openRecord} />}
            {tab === "timeline" && <TimelineTab events={filtered} onSelect={openRecord} onNarrow={narrowToWindow} analyzing={analyzing} />}
            {tab === "changes" && <AllChangesTab events={filtered} onSelect={openRecord} pinnedIds={pinnedIds} />}
            {tab === "security" && <SecurityTab run={run} onSelect={openRecord} />}
            {tab === "risk" && <RiskTab run={run} onSelect={openRecord} />}
            {tab === "resources" && <ResourcesTab run={run} onSelectResource={(rid) => setResourceFocus(rid)} />}
            {tab === "actors" && <ActorsTab run={run} />}
            {tab === "diff" && <DiffTab events={filtered} />}
            {tab === "impact" && <ImpactTab run={run} />}
            {tab === "compare" && <CompareTab run={run} history={history} />}
            {tab === "export" && <ExportTab run={run} />}
          </div>
        )}
      </div>

      {resourceFocus && run && (
        <ResourceHistoryDrawer
          run={run}
          resourceId={resourceFocus}
          onClose={() => setResourceFocus(null)}
          onSelect={(e) => { setResourceFocus(null); openRecord(e); }}
        />
      )}
      {selected && (
        <ChangeDrawer
          event={selected}
          runId={run?.runId}
          aiPending={aiEnriching}
          pinned={!!selected && pinnedIds.has(selected.changeId)}
          note={(run?.caseFile?.notes || {})[selected.changeId] || ""}
          onTogglePin={() => togglePin(selected.changeId)}
          onSaveNote={(t) => saveNote(selected.changeId, t)}
          onInvestigate={() => investigateChange(selected)}
          onClose={() => { setSelected(null); clearChangeDeepLink(); }}
        />
      )}
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

// Saved perspectives (D3): persist named filter combos in localStorage so a reviewer can re-apply
// a frequent view (e.g. "RBAC by John, high risk") with one click.
type Perspective = { fRisk: string; fCat: string; fActor: string; fType: string; search: string };
const _PERSP_KEY = "azsup.changeexp.perspectives.v1";
function PerspectiveBar({ current, onApply }: { current: Perspective; onApply: (p: Perspective) => void }) {
  const [saved, setSaved] = useState<{ name: string; p: Perspective }[]>(() => {
    try { return JSON.parse(localStorage.getItem(_PERSP_KEY) || "[]"); } catch { return []; }
  });
  function persist(list: { name: string; p: Perspective }[]) {
    setSaved(list);
    try { localStorage.setItem(_PERSP_KEY, JSON.stringify(list)); } catch { /* ignore */ }
  }
  function save() {
    const hasFilter = current.fRisk || current.fCat || current.fActor || current.fType || current.search;
    if (!hasFilter) return;
    const name = window.prompt("Name this perspective (filter view):");
    if (!name) return;
    persist([...saved.filter((s) => s.name !== name), { name, p: current }]);
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      <button onClick={save} title="Save the current filters as a named perspective" className="rounded border px-1.5 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">💾 Save view</button>
      {saved.map((s) => (
        <span key={s.name} className="inline-flex items-center gap-0.5 rounded-full border bg-white px-1.5 py-0.5 text-[11px]">
          <button onClick={() => onApply(s.p)} className="text-gray-700 hover:text-brand">⭐ {s.name}</button>
          <button onClick={() => persist(saved.filter((x) => x.name !== s.name))} className="text-gray-300 hover:text-red-600">✕</button>
        </span>
      ))}
    </div>
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
  return /unavailable|not reachable|isn['’]t reachable|access denied|denied|unauthor|forbidden|not recognized|failed|isn['’]t signed in|no access|lacks (read )?permission|expired|no (azure |graph )?token|couldn['’]t acquire|sign(ed)? in/i.test(n || "");
}

// A stronger, panel-level check used to surface a RED "sources couldn't be queried" banner on
// EVERY tab — so an empty result on Technical Diff / Actors / Timeline isn't mistaken for
// "nothing changed" when the real cause is a failed query (expired/missing token, wrong tenant,
// denied). Identity-resolution degrade notes (names not resolved) are NOT source failures.
function isSourceProblemNote(n: string): boolean {
  const s = n || "";
  if (/identity names not resolved|object-ids are shown/i.test(s)) return false; // soft degrade, not a failure
  return isProblemNote(s);
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
  const problems = notes.filter(isSourceProblemNote);
  const infoNotes = notes.filter((n) => !isSourceProblemNote(n));
  const emptyButProblem = run.totalChanges === 0 && problems.length > 0;
  return (
    <div className="space-y-4">
      {/* Analyzed window is the HERO fact — distinct from "when the analysis ran" — so a stale
          window can never masquerade as fresh data. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border bg-white px-4 py-2.5">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Analyzed window</div>
          <div className="text-sm font-semibold text-gray-900">{fmtTime(run.startTime)} → {fmtTime(run.endTime)}</div>
        </div>
        <div className="h-8 w-px bg-gray-200" />
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Scope</div>
          <div className="text-sm text-gray-700">{run.scopeMode || "—"}</div>
        </div>
        <div className="h-8 w-px bg-gray-200" />
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Analysis run</div>
          <div className="text-sm text-gray-500" title={run.completedAt}>{run.completedAt ? fmtTime(run.completedAt) : "—"}</div>
        </div>
      </div>
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
function TimelineTab({ events, onSelect, onNarrow, analyzing }: { events: ChangeEvent[]; onSelect: (e: ChangeEvent) => void; onNarrow: (startMs: number, endMs: number) => void; analyzing: boolean }) {
  // Date/time window slicer (mirrors Azure Policy → Timeline). Narrows the events shown to a
  // brushable sub-window of the run, with a per-bucket histogram + dual-range handles + presets.
  const [range, setRange] = useState<[number, number] | null>(null);
  // Reset the brush when the underlying event set changes (different run / filters).
  useEffect(() => { setRange(null); }, [events]);

  const shown = useMemo(() => {
    if (!range) return events;
    const [lo, hi] = range;
    return events.filter((e) => {
      const t = new Date(e.eventTime).getTime();
      return !Number.isNaN(t) && t >= lo && t <= hi;
    });
  }, [events, range]);

  if (!events.length) return <Empty />;
  return (
    <div className="space-y-3">
      <TimelineSlicer events={events} value={range} onChange={setRange} shownCount={shown.length} onNarrow={onNarrow} analyzing={analyzing} />
      {shown.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-gray-50 p-6 text-center text-sm text-gray-500">No changes in the selected window. Widen the slider or click <b>All</b>.</div>
      ) : (
        <div className="relative space-y-2 border-l-2 border-gray-100 pl-4">
          {shown.map((e) => (
            <button key={e.changeId} onClick={() => onSelect(e)} className="block w-full rounded-lg border bg-white p-2 text-left hover:bg-gray-50">
              <div className="absolute -left-[7px] mt-1 h-3 w-3 rounded-full" style={{ background: RISK_COLOR[e.riskLabel] }} />
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs tabular-nums text-gray-400">{fmtTime(e.eventTime)}</span>
                <RiskChip label={e.riskLabel} score={e.riskScore} />
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{e.category}</span>
                <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
                <span className="text-[11px] text-gray-400">{e.actorDisplay || e.actor}</span>
              </div>
              <div className="mt-0.5 text-[12px] text-gray-600">{e.plainEnglishSummary}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Brushable date/time window over a run's change events (mirrors Azure Policy → Timeline's
// DateSlicer). Histogram for density + a selected band + dual-range handles + quick presets.
function TimelineSlicer({
  events, value, onChange, shownCount, onNarrow, analyzing,
}: { events: ChangeEvent[]; value: [number, number] | null; onChange: (v: [number, number] | null) => void; shownCount: number; onNarrow: (startMs: number, endMs: number) => void; analyzing: boolean }) {
  const { minTs, maxTs, buckets } = useMemo(() => {
    const ts = events.map((e) => new Date(e.eventTime).getTime()).filter((t) => !Number.isNaN(t));
    if (!ts.length) return { minTs: 0, maxTs: 0, buckets: [] as number[] };
    const lo = Math.min(...ts), hi = Math.max(...ts);
    const N = 48;
    const span = Math.max(1, hi - lo);
    const b = new Array(N).fill(0);
    for (const t of ts) b[Math.min(N - 1, Math.floor(((t - lo) / span) * N))]++;
    return { minTs: lo, maxTs: hi, buckets: b };
  }, [events]);

  if (!minTs || minTs === maxTs) return null;
  const lo = value ? value[0] : minTs;
  const hi = value ? value[1] : maxTs;
  const maxBar = Math.max(1, ...buckets);
  const pct = (t: number) => ((t - minTs) / Math.max(1, maxTs - minTs)) * 100;
  // Adaptive step so the handles move smoothly whether the run spans hours or weeks.
  const step = Math.max(60_000, Math.floor((maxTs - minTs) / 500));
  const fmt = (t: number) => new Date(t).toLocaleString();
  const hourMs = 3600_000, dayMs = 86400_000;
  // A sub-window is "narrowed" when the brush is meaningfully inside the full run span (so the
  // re-scan would actually change the query window). The popover keys off this.
  const fullSpan = Math.max(1, maxTs - minTs);
  const narrowed = !!value && (lo - minTs > fullSpan * 0.01 || maxTs - hi > fullSpan * 0.01);
  // Center the popover over the selected band.
  const bandMidPct = (pct(lo) + pct(hi)) / 2;

  function setLo(t: number) { onChange([Math.min(t, hi), hi]); }
  function setHi(t: number) { onChange([lo, Math.max(t, lo)]); }

  return (
    <div className="rounded-xl border bg-white p-3 shadow-sm">
      {/* Dynamic popover — appears above the slider when a sub-window is brushed. Re-runs the
          WHOLE analysis scoped to that window (a real new search, not just a client filter). */}
      <div className="relative h-9">
        {narrowed && (
          <div
            className="absolute -top-1 z-10 -translate-x-1/2"
            style={{ left: `${Math.min(88, Math.max(12, bandMidPct))}%` }}
          >
            <button
              onClick={() => onNarrow(lo, hi)}
              disabled={analyzing}
              title="Re-run the analysis scoped to this brushed window"
              className="flex items-center gap-1.5 whitespace-nowrap rounded-full border border-brand bg-brand px-3 py-1 text-[11px] font-medium text-white shadow-md transition hover:bg-brand-dark disabled:opacity-50"
            >
              🔍 {analyzing ? "Searching…" : `Narrow search to this window (${fmt(lo).replace(/:\d\d /, " ")} → ${fmt(hi).replace(/:\d\d /, " ")})`}
            </button>
            <div className="mx-auto h-2 w-2 -translate-y-1 rotate-45 border-b border-r border-brand bg-brand" />
          </div>
        )}
      </div>
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
        <span className="font-medium text-gray-700">📅 Change-time window</span>
        <span className="tabular-nums">{fmt(lo)} → {fmt(hi)}</span>
        <span className="rounded-full bg-brand/10 px-2 py-0.5 font-medium text-brand">{shownCount} shown</span>
        <div className="ml-auto flex items-center gap-1">
          {([
            ["1h", hourMs], ["6h", 6 * hourMs], ["24h", dayMs], ["7d", 7 * dayMs],
          ] as const).map(([lbl, ms]) => (
            <button key={lbl} onClick={() => onChange([Math.max(minTs, maxTs - ms), maxTs])} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">{lbl}</button>
          ))}
          <button onClick={() => onChange(null)} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">All</button>
        </div>
      </div>
      {/* histogram */}
      <div className="relative h-10 w-full">
        <div className="flex h-full w-full items-end gap-px">
          {buckets.map((b, i) => {
            const bucketMs = (maxTs - minTs) / buckets.length;
            const t = minTs + (i / buckets.length) * (maxTs - minTs);
            const tEnd = t + bucketMs;
            const inRange = t >= lo - bucketMs && t <= hi;
            const tip = `${fmt(t)} → ${fmt(tEnd)}\n${b} change${b === 1 ? "" : "s"}`;
            // Full-height column so the whole bucket is hoverable (even empty ones), with the bar
            // pinned to the bottom. The title shows the bucket's time range + change count.
            return (
              <div key={i} title={tip} className="group flex h-full flex-1 cursor-default items-end">
                <div
                  className={`w-full rounded-t ${inRange ? "bg-brand/60 group-hover:bg-brand" : "bg-gray-200 group-hover:bg-gray-300"}`}
                  style={{ height: `${(b / maxBar) * 100}%`, minHeight: b > 0 ? "2px" : undefined }}
                />
              </div>
            );
          })}
        </div>
        <div className="pointer-events-none absolute inset-y-0 rounded bg-brand/10" style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
      </div>
      {/* dual range */}
      <div className="relative mt-1 h-4">
        <div className="pointer-events-none absolute top-1/2 h-1 w-full -translate-y-1/2 rounded-full bg-gray-200" />
        <div className="pointer-events-none absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-brand" style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
        <input type="range" min={minTs} max={maxTs} step={step} value={lo} onChange={(e) => setLo(Number(e.target.value))}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
        <input type="range" min={minTs} max={maxTs} step={step} value={hi} onChange={(e) => setHi(Number(e.target.value))}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
      </div>
    </div>
  );
}


// --------------------------------------------------------------- All Changes
function AllChangesTab({ events, onSelect, pinnedIds }: { events: ChangeEvent[]; onSelect: (e: ChangeEvent) => void; pinnedIds?: Set<string> }) {
  if (!events.length) return <Empty />;
  const cols = "grid grid-cols-[120px_84px_90px_1fr_120px_110px_130px_150px_70px_70px] items-center gap-2";
  return (
    <div className="rounded-xl border bg-white">
      <div className={`${cols} sticky top-0 z-10 border-b bg-gray-50 px-3 py-2 text-[11px] uppercase text-gray-400`}>
        <span>Time</span><span>Risk</span><span>Category</span><span>Resource</span><span>Type</span><span>RG</span><span>Operation</span><span>Actor</span><span>Source</span><span>Conf.</span>
      </div>
      <VirtualList items={events} estimateSize={38} render={(e) => (
        <button onClick={() => onSelect(e)} className={`${cols} w-full cursor-pointer border-t px-3 py-1.5 text-left text-sm hover:bg-gray-50`}>
          <span className="truncate text-[11px] tabular-nums text-gray-500">{fmtTime(e.eventTime)}</span>
          <RiskChip label={e.riskLabel} score={e.riskScore} />
          <span className="truncate text-[11px] text-gray-600">{e.category}</span>
          <span className="truncate font-medium text-gray-800">
            {pinnedIds?.has(e.changeId) && <span title="Pinned">📌 </span>}
            {(e.securityFlags?.length ?? 0) > 0 && <span title={e.securityFlags!.map((f) => f.label).join(", ")}>🛡️ </span>}
            {e.resourceName}
          </span>
          <span className="truncate text-[11px] text-gray-500">{shortType(e.resourceType)}</span>
          <span className="truncate text-[11px] text-gray-500">{e.resourceGroup}</span>
          <span className="truncate text-[11px] text-gray-500">{e.operation.split("/").slice(-2).join("/")}</span>
          <span className="truncate text-[11px] text-gray-600">{e.actorDisplay || e.actor}</span>
          <span className="truncate text-[11px] text-gray-400">{e.source}</span>
          <span className="truncate text-[11px] text-gray-400">{e.confidence}</span>
        </button>
      )} />
    </div>
  );
}

// --------------------------------------------------------------- Operations (A1)
function OperationsTab({ run, onSelect }: { run: ChangeAnalysisRun; onSelect: (e: ChangeEvent) => void }) {
  const allOps = run.operations ?? [];
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 150);
  const ops = useMemo(() => {
    const s = dq.trim().toLowerCase();
    if (!s) return allOps;
    return allOps.filter((o) => `${o.actor} ${o.verb} ${o.categories.join(" ")} ${o.resourceNames.join(" ")} ${o.highestRiskLabel}`.toLowerCase().includes(s));
  }, [allOps, dq]);
  const eventsById = useMemo(() => { const m = new Map<string, ChangeEvent>(); for (const e of run.events) m.set(e.changeId, e); return m; }, [run.events]);
  if (!allOps.length) return <StaleDerivedEmpty label="grouped operations" />;
  function toggle(id: string) { setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; }); }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="px-1 text-[11px] text-gray-400">{allOps.length} operation(s) — changes sharing a deployment / correlation id (or one actor in a short burst) are grouped into a single action.</p>
        <TabSearch q={q} setQ={setQ} shown={ops.length} total={allOps.length} placeholder="Search operations…" />
      </div>
      {ops.map((op) => (
        <div key={op.operationId} className="rounded-xl border bg-white">
          <button onClick={() => toggle(op.operationId)} className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-gray-50">
            <span className="text-gray-400">{open.has(op.operationId) ? "▾" : "▸"}</span>
            <RiskChip label={op.highestRiskLabel} score={op.highestRiskScore} />
            <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700">{op.verb}</span>
            <span className="text-sm font-medium text-gray-800">{op.actor}</span>
            <span className="text-[11px] text-gray-500">{op.changeCount} change{op.changeCount === 1 ? "" : "s"} · {op.resourceCount} resource{op.resourceCount === 1 ? "" : "s"}</span>
            {op.securityFlagCount > 0 && <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">🛡️ {op.securityFlagCount}</span>}
            <span className="ml-auto text-[11px] tabular-nums text-gray-400">{fmtTime(op.startTime)}</span>
          </button>
          {open.has(op.operationId) && (
            <div className="border-t px-3 py-2">
              <div className="mb-1 text-[11px] text-gray-400">Categories: {op.categories.join(", ") || "—"}{op.resourceNames.length ? ` · Resources: ${op.resourceNames.join(", ")}` : ""}</div>
              <div className="divide-y">
                {op.changeIds.map((cid) => { const e = eventsById.get(cid); if (!e) return null; return (
                  <button key={cid} onClick={() => onSelect(e)} className="flex w-full items-center gap-2 py-1 text-left text-[12px] hover:bg-gray-50">
                    <RiskChip label={e.riskLabel} score={e.riskScore} />
                    {(e.securityFlags?.length ?? 0) > 0 && <span title={e.securityFlags!.map((f) => f.label).join(", ")}>🛡️</span>}
                    <span className="font-medium text-gray-700">{e.resourceName}</span>
                    <span className="text-gray-400">{e.operation.split("/").slice(-2).join("/")}</span>
                    <span className="ml-auto text-[10px] tabular-nums text-gray-400">{fmtTime(e.eventTime)}</span>
                  </button>
                ); })}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// Empty state shown when a derived view is missing on an older cached run (U5) — offers re-analyze.
function StaleDerivedEmpty({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">
      No {label} for this run. Older cached runs predate this view — click <b>Analyze Changes</b> (or Re-analyze) to populate it.
    </div>
  );
}

// --------------------------------------------------------------- Narrative (A2)
function NarrativeTab({ run, onSelect }: { run: ChangeAnalysisRun; onSelect: (e: ChangeEvent) => void }) {
  const allBeats = run.narrative ?? [];
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 150);
  const beats = useMemo(() => {
    const s = dq.trim().toLowerCase();
    return s ? allBeats.filter((b) => `${b.text} ${b.actor} ${b.categories.join(" ")}`.toLowerCase().includes(s)) : allBeats;
  }, [allBeats, dq]);
  const eventsById = useMemo(() => { const m = new Map<string, ChangeEvent>(); for (const e of run.events) m.set(e.changeId, e); return m; }, [run.events]);
  if (!allBeats.length) return <StaleDerivedEmpty label="narrative" />;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="px-1 text-[11px] text-gray-400">A chronological story of the window (oldest → newest). Click a beat to open its first change.</p>
        <TabSearch q={q} setQ={setQ} shown={beats.length} total={allBeats.length} placeholder="Search narrative…" />
      </div>
      <div className="relative space-y-1 border-l-2 border-gray-100 pl-4">
        {beats.map((b, i) => (
          <button key={i} onClick={() => { const e = eventsById.get(b.changeIds[0]); if (e) onSelect(e); }}
            className="block w-full rounded-lg border bg-white p-2 text-left hover:bg-gray-50">
            <div className="absolute -left-[7px] mt-1 h-3 w-3 rounded-full" style={{ background: RISK_COLOR[b.riskLabel] }} />
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs tabular-nums text-gray-400">{fmtTime(b.time)}</span>
              <RiskChip label={b.riskLabel} score={b.riskScore} />
              {b.securityFlagCount > 0 && <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">🛡️ {b.securityFlagCount}</span>}
            </div>
            <div className="mt-0.5 text-[13px] text-gray-700">{b.text}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------- Security (C)
function SecurityTab({ run, onSelect }: { run: ChangeAnalysisRun; onSelect: (e: ChangeEvent) => void }) {
  const sec = run.security;
  const [secQ, setSecQ] = useState("");
  const dsq = useDebounced(secQ, 150);
  const flagged = useMemo(() => run.events.filter((e) => (e.securityFlags?.length ?? 0) > 0)
    .sort((a, b) => b.riskScore - a.riskScore), [run.events]);
  const flaggedShown = useMemo(() => {
    const s = dsq.trim().toLowerCase();
    return s ? flagged.filter((e) => `${e.resourceName} ${e.actorDisplay || e.actor} ${(e.securityFlags ?? []).map((f) => f.label).join(" ")}`.toLowerCase().includes(s)) : flagged;
  }, [flagged, dsq]);
  const suspicious = (run.insights ?? []).filter((i) => String(i.insightType).startsWith("suspicious_"));
  if (!sec || (sec.flagged_changes === 0 && suspicious.length === 0)) {
    return <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">✅ No security-sensitive changes or suspicious patterns detected in this window.</div>;
  }
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label="Flagged changes" value={sec.flagged_changes} tone="text-red-600" />
        <Kpi label="Critical flags" value={sec.by_severity.critical ?? 0} tone="text-red-700" />
        <Kpi label="High flags" value={sec.by_severity.high ?? 0} tone="text-orange-600" />
        <Kpi label="Suspicious patterns" value={suspicious.length} tone="text-violet-700" />
      </div>
      {suspicious.length > 0 && (
        <div className="rounded-xl border bg-white">
          <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">Suspicious patterns — review first</div>
          <div className="divide-y">
            {suspicious.map((i) => (
              <div key={i.insightId} className="px-4 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <RiskChip label={i.severity} />
                  <span className="text-sm font-medium text-gray-800">{i.title}</span>
                </div>
                <div className="mt-0.5 text-[12px] text-gray-600">{i.summary}</div>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="rounded-xl border bg-white">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
          <span className="text-sm font-medium text-gray-700">Security-flagged changes ({flagged.length})</span>
          <TabSearch q={secQ} setQ={setSecQ} shown={flaggedShown.length} total={flagged.length} placeholder="Search flagged…" />
        </div>
        <div className="divide-y">
          {flaggedShown.map((e) => (
            <button key={e.changeId} onClick={() => onSelect(e)} className="block w-full px-4 py-2 text-left hover:bg-gray-50">
              <div className="flex flex-wrap items-center gap-2">
                <RiskChip label={e.riskLabel} score={e.riskScore} />
                <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
                <span className="text-[11px] text-gray-400">{e.actorDisplay || e.actor}</span>
                <span className="ml-auto text-[10px] tabular-nums text-gray-400">{fmtTime(e.eventTime)}</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-1">{(e.securityFlags ?? []).map((f, idx) => <SecFlagChip key={idx} flag={f} />)}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- Compare (E2)
function CompareTab({ run, history }: { run: ChangeAnalysisRun; history: ChangeRunSummary[] }) {
  const others = useMemo(() => history.filter((h) => h.runId !== run.runId), [history, run.runId]);
  // U7: default the baseline to the most recent OTHER run for this scope.
  const [otherId, setOtherId] = useState("");
  useEffect(() => { setOtherId((cur) => cur || others[0]?.runId || ""); }, [others]);
  const [result, setResult] = useState<ChangeCompareResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function go() {
    if (!otherId) return;
    setBusy(true); setErr(""); setResult(null);
    try { setResult(await api.changeExplorerCompare(otherId, run.runId)); }  // baseline = other (older), B = current
    catch (e) { setErr(formatError(e)); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border bg-white p-3">
        <span className="text-sm font-medium text-gray-700">Compare current run with a baseline:</span>
        <select value={otherId} onChange={(e) => setOtherId(e.target.value)} className="rounded border px-2 py-1 text-sm">
          <option value="">Select a baseline run…</option>
          {others.map((h) => <option key={h.runId} value={h.runId}>{fmtTime(h.startTime)} → {fmtTime(h.endTime)} · {h.totalChanges} changes</option>)}
        </select>
        <button onClick={go} disabled={!otherId || busy} className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">{busy ? "Comparing…" : "Compare"}</button>
      </div>
      {err && <div className="rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">{err}</div>}
      {result && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label="Added resources" value={result.summary.added} tone="text-emerald-600" />
            <Kpi label="Removed resources" value={result.summary.removed} tone="text-gray-600" />
            <Kpi label="Changed in both" value={result.summary.changed} tone="text-amber-600" />
            <Kpi label="Δ Total changes" value={`${result.summary.total_delta >= 0 ? "+" : ""}${result.summary.total_delta}`} tone={result.summary.total_delta >= 0 ? "text-red-600" : "text-emerald-600"} />
          </div>
          <CompareList title="🆕 Added (in current, not baseline)" rows={result.added.map((r) => ({ name: r.resourceName, type: r.resourceType, risk: r.highestRiskLabel, score: r.highestRiskScore }))} />
          <CompareList title="➖ Removed (in baseline, not current)" rows={result.removed.map((r) => ({ name: r.resourceName, type: r.resourceType, risk: r.highestRiskLabel, score: r.highestRiskScore }))} />
          {result.changed.length > 0 && (
            <div className="overflow-auto rounded-xl border bg-white">
              <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">🔁 Changed in both — risk movement</div>
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400"><tr><th className="px-3 py-2">Resource</th><th className="px-2">Baseline</th><th className="px-2">Current</th><th className="px-2 text-right">Δ Risk</th></tr></thead>
                <tbody>{result.changed.map((r) => (
                  <tr key={r.resourceId} className="border-t">
                    <td className="px-3 py-1.5 font-medium text-gray-800">{r.resourceName}</td>
                    <td className="px-2"><RiskChip label={r.riskLabelA} score={r.riskA} /></td>
                    <td className="px-2"><RiskChip label={r.riskLabelB} score={r.riskB} /></td>
                    <td className={`px-2 text-right font-medium tabular-nums ${r.riskDelta > 0 ? "text-red-600" : r.riskDelta < 0 ? "text-emerald-600" : "text-gray-400"}`}>{r.riskDelta > 0 ? "+" : ""}{r.riskDelta}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function CompareList({ title, rows }: { title: string; rows: { name: string; type: string; risk: string; score: number }[] }) {
  return (
    <div className="overflow-auto rounded-xl border bg-white">
      <div className="border-b px-4 py-2 text-sm font-medium text-gray-700">{title} <span className="text-[11px] text-gray-400">({rows.length})</span></div>
      {rows.length === 0 ? <div className="px-4 py-3 text-xs text-gray-400">None.</div> : (
        <table className="w-full text-sm"><tbody>
          {rows.slice(0, 100).map((r, i) => (
            <tr key={i} className="border-t"><td className="px-3 py-1.5 font-medium text-gray-800">{r.name}</td><td className="px-2 text-[11px] text-gray-500">{shortType(r.type)}</td><td className="px-2"><RiskChip label={r.risk} score={r.score} /></td></tr>
          ))}
        </tbody></table>
      )}
    </div>
  );
}

// --------------------------------------------------------------- Per-resource drill-in (A3)
function ResourceHistoryDrawer({ run, resourceId, onClose, onSelect }: {
  run: ChangeAnalysisRun; resourceId: string; onClose: () => void; onSelect: (e: ChangeEvent) => void;
}) {
  const evs = useMemo(() => run.events.filter((e) => e.resourceId === resourceId)
    .sort((a, b) => (a.eventTime < b.eventTime ? 1 : -1)), [run.events, resourceId]);
  const name = evs[0]?.resourceName || resourceId;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="h-full w-full max-w-lg overflow-auto bg-white shadow-xl" onClick={(ev) => ev.stopPropagation()}>
        <div className="sticky top-0 flex items-center gap-2 border-b bg-white px-4 py-3">
          <span className="text-sm font-semibold text-gray-900">{name}</span>
          <span className="text-[11px] text-gray-400">{evs.length} change(s)</span>
          <button onClick={onClose} className="ml-auto text-gray-400 hover:text-gray-700">✕</button>
        </div>
        <div className="p-3 text-xs text-gray-500">{shortType(evs[0]?.resourceType || "")} · {evs[0]?.resourceGroup}</div>
        <div className="relative space-y-2 border-l-2 border-gray-100 px-4 pb-6 pl-6">
          {evs.map((e) => (
            <button key={e.changeId} onClick={() => onSelect(e)} className="block w-full rounded-lg border bg-white p-2 text-left hover:bg-gray-50">
              <div className="absolute -left-[7px] mt-1 h-3 w-3 rounded-full" style={{ background: RISK_COLOR[e.riskLabel] }} />
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] tabular-nums text-gray-400">{fmtTime(e.eventTime)}</span>
                <RiskChip label={e.riskLabel} score={e.riskScore} />
                <span className="text-[11px] text-gray-500">{e.operation.split("/").slice(-2).join("/")}</span>
                <span className="text-[11px] text-gray-400">{e.actorDisplay || e.actor}</span>
              </div>
              <div className="mt-0.5 text-[12px] text-gray-600">{e.plainEnglishSummary}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- Risk Insights
function RiskTab({ run, onSelect }: { run: ChangeAnalysisRun; onSelect: (e: ChangeEvent) => void }) {
  const [q, setQ] = useState("");
  const byCat = useMemo(() => rollup(run.events, (e) => e.category), [run.events]);
  const byActor = useMemo(() => rollup(run.events, (e) => e.actor), [run.events]);
  const ranked = useMemo(() => [...run.events].sort((a, b) => b.riskScore - a.riskScore), [run.events]);
  const top = useMemo(() => {
    const s = q.trim().toLowerCase();
    const base = s
      ? ranked.filter((e) => `${e.resourceName} ${e.category} ${e.whyRisk} ${e.actorDisplay || e.actor} ${e.operation} ${e.riskLabel}`.toLowerCase().includes(s))
      : ranked;
    return s ? base : base.slice(0, 10);
  }, [ranked, q]);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium text-gray-700">Risk by category (max score)</div>
          <ResponsiveContainer width="100%" height={Math.max(120, byCat.length * 30)}>
            <BarChart layout="vertical" data={byCat} margin={{ left: 20, right: 20 }}>
              <XAxis type="number" domain={[0, 100]} fontSize={11} /><YAxis type="category" dataKey="key" width={100} fontSize={11} tickFormatter={axisTrunc} />
              <Tooltip /><Bar dataKey="max" radius={[0, 4, 4, 0]}>{byCat.map((d, i) => <Cell key={i} fill={RISK_COLOR[labelFor(d.max)]} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 text-sm font-medium text-gray-700">Risk by actor (max score)</div>
          <ResponsiveContainer width="100%" height={Math.max(120, byActor.length * 30)}>
            <BarChart layout="vertical" data={byActor} margin={{ left: 20, right: 20 }}>
              <XAxis type="number" domain={[0, 100]} fontSize={11} /><YAxis type="category" dataKey="key" width={140} fontSize={11} tickFormatter={axisTrunc} />
              <Tooltip /><Bar dataKey="max" radius={[0, 4, 4, 0]}>{byActor.map((d, i) => <Cell key={i} fill={RISK_COLOR[labelFor(d.max)]} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="rounded-xl border bg-white">
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
          <span className="text-sm font-medium text-gray-700">{q ? "Matching changes — why they're risky" : "Highest-risk changes — why they're risky"}</span>
          <div className="ml-auto"><TabSearch q={q} setQ={setQ} shown={top.length} total={run.events.length} placeholder="Search risky changes…" /></div>
        </div>
        <div className="divide-y">
          {top.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-gray-500">No changes match “{q}”.</div>
          ) : top.map((e) => (
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
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return run.resources;
    return run.resources.filter((r) =>
      `${r.resourceName} ${r.resourceType} ${r.resourceGroup} ${r.lastActor} ${r.role} ${r.highestRiskLabel}`.toLowerCase().includes(s));
  }, [run.resources, q]);
  if (!run.resources.length) return <Empty />;
  return (
    <div className="space-y-2">
      <TabSearch q={q} setQ={setQ} shown={rows.length} total={run.resources.length} placeholder="Search resources, type, RG, actor…" />
      <div className="overflow-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400">
            <tr><th className="px-3 py-2">Resource</th><th className="px-2">Type</th><th className="px-2">RG</th><th className="px-2 text-right">Changes</th><th className="px-2">Highest risk</th><th className="px-2">Last changed</th><th className="px-2">Last actor</th><th className="px-2">Role</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
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
    </div>
  );
}

// Small inline search bar reused by the grid tabs (Resources / Actors).
function TabSearch({ q, setQ, shown, total, placeholder }: { q: string; setQ: (v: string) => void; shown: number; total: number; placeholder: string }) {
  return (
    <div className="flex items-center gap-2">
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} className="w-72 rounded border px-2 py-1 text-sm" />
      {q && <button onClick={() => setQ("")} className="text-[11px] text-gray-400 hover:text-gray-600">✕ clear</button>}
      <span className="text-[11px] text-gray-400">{shown} / {total}</span>
    </div>
  );
}

// --------------------------------------------------------------- Actors
const ACTOR_KIND_STYLE: Record<string, { label: string; cls: string }> = {
  User: { label: "User", cls: "bg-blue-100 text-blue-700" },
  ServicePrincipal: { label: "App / SPN", cls: "bg-violet-100 text-violet-700" },
  ManagedIdentity: { label: "Managed identity", cls: "bg-teal-100 text-teal-700" },
  AzurePlatform: { label: "Azure platform", cls: "bg-gray-100 text-gray-500" },
  AzurePolicy: { label: "Azure Policy", cls: "bg-gray-100 text-gray-500" },
  System: { label: "System", cls: "bg-gray-100 text-gray-500" },
  Unknown: { label: "Unknown", cls: "bg-amber-100 text-amber-700" },
};

function ActorKindBadge({ kind }: { kind: string }) {
  const s = ACTOR_KIND_STYLE[kind] || ACTOR_KIND_STYLE.Unknown;
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${s.cls}`}>{s.label}</span>;
}

// True when the displayed actor name is still a raw object-id (GUID) rather than a resolved name.
function isRawGuid(s: string): boolean {
  return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test((s || "").trim());
}

function ActorsTab({ run }: { run: ChangeAnalysisRun }) {
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return run.actors;
    return run.actors.filter((a) =>
      `${a.actor} ${a.actorId || ""} ${a.actorType} ${a.categories.join(" ")} ${(a.ips || []).join(" ")} ${(a.onBehalfOf || []).join(" ")} ${a.highestRiskLabel}`.toLowerCase().includes(s));
  }, [run.actors, q]);
  if (!run.actors.length) return <Empty />;
  return (
    <div className="space-y-2">
      <p className="px-1 text-[11px] text-gray-400">
        Identities are resolved to names via Microsoft Graph where the connection has directory read
        access. Object-ids shown as GUIDs could not be resolved (missing Graph permission, deleted, or
        cross-tenant). “Azure platform” rows are Azure-initiated/automation writes with no human caller.
      </p>
      <TabSearch q={q} setQ={setQ} shown={rows.length} total={run.actors.length} placeholder="Search actor, id, type, category, IP…" />
      <div className="overflow-auto rounded-xl border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-[11px] uppercase text-gray-400">
            <tr><th className="px-3 py-2">Actor</th><th className="px-2">Type</th><th className="px-2 text-right">Changes</th><th className="px-2">Highest risk</th><th className="px-2">Categories</th><th className="px-2 text-right">Resources</th><th className="px-2">Source IP</th><th className="px-2">First</th><th className="px-2">Last</th></tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const kind = a.actorType || "Unknown";
              const raw = isRawGuid(a.actor);
              return (
                <tr key={a.actorId || a.actor} className="border-t align-top">
                  <td className="px-3 py-1.5">
                    <div className={`font-medium ${raw ? "font-mono text-[11px] text-gray-500" : "text-gray-800"}`} title={a.actorId || a.actor}>
                      {a.actor}{raw && <span className="ml-1 rounded bg-gray-100 px-1 text-[9px] uppercase text-gray-400">unresolved id</span>}
                    </div>
                    {a.onBehalfOf && a.onBehalfOf.length > 0 && (
                      <div className="text-[10px] text-gray-400">on behalf of {a.onBehalfOf.join(", ")}</div>
                    )}
                  </td>
                  <td className="px-2"><ActorKindBadge kind={kind} /></td>
                  <td className="px-2 text-right tabular-nums">{a.changes}</td>
                  <td className="px-2"><RiskChip label={a.highestRiskLabel} score={a.highestRiskScore} /></td>
                  <td className="px-2 text-[11px] text-gray-500">{a.categories.join(", ")}</td>
                  <td className="px-2 text-right tabular-nums">{a.resources}</td>
                  <td className="px-2 font-mono text-[10px] text-gray-500">{a.ips && a.ips.length ? a.ips.join(", ") : "—"}</td>
                  <td className="px-2 text-[11px] text-gray-500">{fmtTime(a.firstChange)}</td>
                  <td className="px-2 text-[11px] text-gray-500">{fmtTime(a.lastChange)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- Technical Diff
function DiffTab({ events }: { events: ChangeEvent[] }) {
  // P4: only changes with an actual before/after diff are worth this heavy view; cap with a
  // "show more" so a 5,000-change run doesn't render thousands of diff tables.
  const withDiff = useMemo(() => events.filter((e) => e.details.length > 0), [events]);
  const [limit, setLimit] = useState(50);
  useEffect(() => { setLimit(50); }, [events]);
  if (!events.length) return <Empty />;
  if (!withDiff.length) return <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">No before/after property diffs available for these changes (the source feed didn’t include them).</div>;
  const shown = withDiff.slice(0, limit);
  return (
    <div className="space-y-3">
      <p className="px-1 text-[11px] text-gray-400">{withDiff.length} change(s) with property diffs (of {events.length} total). Showing {shown.length}.</p>
      {shown.map((e) => (
        <div key={e.changeId} className="rounded-xl border bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <RiskChip label={e.riskLabel} score={e.riskScore} />
            <span className="text-sm font-medium text-gray-800">{e.resourceName}</span>
            <span className="text-[11px] text-gray-400">{e.operation}</span>
            <span className="ml-auto text-[11px] text-gray-400">corr: {e.correlationId || "—"}</span>
          </div>
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
        </div>
      ))}
      {limit < withDiff.length && (
        <button onClick={() => setLimit((n) => n + 50)} className="w-full rounded-xl border bg-white py-2 text-sm text-gray-600 hover:bg-gray-50">Show {Math.min(50, withDiff.length - limit)} more ({withDiff.length - limit} remaining)</button>
      )}
    </div>
  );
}

// --------------------------------------------------------------- Dependency Impact
function ImpactTab({ run }: { run: ChangeAnalysisRun }) {
  const [q, setQ] = useState("");
  const filteredEvents = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return run.events;
    return run.events.filter((e) =>
      `${e.resourceName} ${e.dependencyRole} ${e.blastRadius} ${e.resourceType}`.toLowerCase().includes(s));
  }, [run.events, q]);
  const byRole = useMemo(() => {
    const m = new Map<string, ChangeEvent[]>();
    for (const e of filteredEvents) { const k = e.dependencyRole || "Workload resource"; (m.get(k) ?? m.set(k, []).get(k))!.push(e); }
    return [...m.entries()].sort((a, b) => Math.max(...b[1].map((e) => e.riskScore)) - Math.max(...a[1].map((e) => e.riskScore)));
  }, [filteredEvents]);
  if (!run.events.length) return <Empty />;
  return (
    <div className="space-y-3">
      <TabSearch q={q} setQ={setQ} shown={filteredEvents.length} total={run.events.length} placeholder="Search role, resource, blast radius…" />
      {byRole.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-gray-50 p-6 text-center text-sm text-gray-500">No changes match “{q}”.</div>
      ) : byRole.map(([role, evs]) => (
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
      <a href={api.changeExplorerReportPdfUrl(run.runId)} target="_blank" rel="noopener noreferrer"
        className="flex items-center gap-3 rounded-xl border-2 border-brand bg-brand/5 p-3 hover:bg-brand/10">
        <span className="text-2xl">🧾</span>
        <div>
          <div className="text-sm font-semibold text-brand">Download incident report (PDF)</div>
          <div className="text-[11px] text-gray-500">Board-ready: window, security flags, suspicious patterns, operation timeline, top changes + case notes.</div>
        </div>
      </a>
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
function ChangeDrawer({ event: e, runId, aiPending, pinned, note, onTogglePin, onSaveNote, onInvestigate, onClose }: {
  event: ChangeEvent; runId?: string; aiPending?: boolean; pinned?: boolean; note?: string;
  onTogglePin?: () => void; onSaveNote?: (t: string) => void; onInvestigate?: () => void; onClose: () => void;
}) {
  const [noteDraft, setNoteDraft] = useState(note ?? "");
  const [section, setSection] = useState<"summary" | "diff" | "raw">("summary");
  const [raw, setRaw] = useState<Record<string, unknown> | null>(e.rawEventJson ?? null);
  const [rawLoading, setRawLoading] = useState(false);
  useEffect(() => { setNoteDraft(note ?? ""); setSection("summary"); setRaw(e.rawEventJson ?? null); }, [note, e.changeId, e.rawEventJson]);
  // Lazy-load raw JSON only when the Raw tab is opened (P2) — the list payload omits it.
  useEffect(() => {
    if (section !== "raw" || raw || !runId || e._hasRaw === false) return;
    let cancelled = false;
    setRawLoading(true);
    api.changeExplorerChangeRaw(runId, e.changeId)
      .then((r) => { if (!cancelled) setRaw(r.rawEventJson ?? {}); })
      .catch(() => { if (!cancelled) setRaw({}); })
      .finally(() => { if (!cancelled) setRawLoading(false); });
    return () => { cancelled = true; };
  }, [section, raw, runId, e.changeId, e._hasRaw]);

  function shareLink() {
    try {
      const u = new URL(window.location.href); u.searchParams.set("change", e.changeId);
      void navigator.clipboard?.writeText(u.toString());
    } catch { /* ignore */ }
  }

  const tabBtn = (id: typeof section, label: string) => (
    <button onClick={() => setSection(id)} className={`rounded-t-lg px-3 py-1.5 text-xs ${section === id ? "border-b-2 border-brand font-medium text-brand" : "text-gray-500 hover:text-gray-700"}`}>{label}</button>
  );

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="h-full w-full max-w-xl overflow-auto bg-white shadow-xl" onClick={(ev) => ev.stopPropagation()}>
        <div className="sticky top-0 z-10 border-b bg-white px-4 py-3">
          <div className="flex items-center gap-2">
            <RiskChip label={e.riskLabel} score={e.riskScore} />
            <span className="truncate text-sm font-semibold text-gray-900">{e.resourceName}</span>
            <button onClick={onClose} className="ml-auto text-gray-400 hover:text-gray-700">✕</button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {onTogglePin && (
              <button onClick={onTogglePin} title={pinned ? "Unpin from case file" : "Pin to case file"}
                className={`rounded border px-1.5 py-0.5 text-[11px] ${pinned ? "border-amber-300 bg-amber-50 text-amber-700" : "text-gray-500 hover:bg-gray-50"}`}>
                {pinned ? "📌 Pinned" : "📌 Pin"}
              </button>
            )}
            {onInvestigate && (
              <button onClick={onInvestigate} title="Open a Deep Investigation seeded with this change"
                className="rounded border border-violet-300 bg-violet-50 px-1.5 py-0.5 text-[11px] text-violet-700 hover:bg-violet-100">🔎 Investigate</button>
            )}
            <button onClick={shareLink} title="Copy a shareable link to this change" className="rounded border px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">🔗 Copy link</button>
          </div>
          <div className="mt-2 flex gap-1 border-b">
            {tabBtn("summary", "Summary")}
            {(e.details.length > 0 || e.rollbackHint) && tabBtn("diff", "Diff & revert")}
            {tabBtn("raw", "Raw")}
          </div>
        </div>
        <div className="space-y-3 p-4 text-sm">
          {aiPending && (
            <div className="flex items-center gap-2 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-800">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500" />
              <span>✨ Running AI analysis to enrich this change’s narrative & risk…</span>
            </div>
          )}
          {section === "summary" && <>
            {e.securityFlags && e.securityFlags.length > 0 && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2">
                <div className="mb-1 text-[11px] font-semibold uppercase text-red-700">🛡️ Security flags</div>
                <div className="flex flex-wrap gap-1">{e.securityFlags.map((f, i) => <SecFlagChip key={i} flag={f} />)}</div>
              </div>
            )}
            <Field label="What happened">{e.plainEnglishSummary}</Field>
            <Field label="Possible impact">{e.possibleImpact}</Field>
            <Field label="Why this risk score">{e.whyRisk}</Field>
            <div className="flex flex-wrap gap-1">
              {e.riskFactors.map((f, i) => <span key={i} className={`rounded px-1.5 py-0.5 text-[10px] ${f.delta >= 0 ? "bg-red-50 text-red-600" : "bg-emerald-50 text-emerald-600"}`}>{f.label} {f.delta >= 0 ? "+" : ""}{f.delta}</span>)}
            </div>
            <div className="grid grid-cols-2 gap-2 text-[12px]">
              <KV k="Category" v={e.category} /><KV k="Confidence" v={e.confidence} />
              <KV k="Operation" v={e.operation} /><KV k="Actor" v={`${e.actorDisplay || e.actor} (${e.actorKind || e.actorType})`} />
              <KV k="Source" v={e.source} /><KV k="Time" v={fmtTime(e.eventTime)} />
              <KV k="Resource group" v={e.resourceGroup} /><KV k="Subscription" v={e.subscriptionId} />
              <KV k="Correlation ID" v={e.correlationId || "—"} /><KV k="Role" v={e.dependencyRole} />
              {e.actorIp && <KV k="Source IP" v={e.actorIp} />}
              {e.actorOnBehalfOf && <KV k="On behalf of" v={e.actorOnBehalfOf} />}
            </div>
            <Field label="Blast radius (inferred)">{e.blastRadius}</Field>
            {onSaveNote && (
              <Field label="Investigator note">
                <textarea value={noteDraft} onChange={(ev) => setNoteDraft(ev.target.value)} rows={2}
                  placeholder="Add a note for the case file…" className="w-full rounded border px-2 py-1 text-xs" />
                <button onClick={() => onSaveNote(noteDraft)} className="mt-1 rounded bg-gray-900 px-2 py-1 text-[11px] text-white hover:bg-gray-800">Save note</button>
              </Field>
            )}
          </>}
          {section === "diff" && <>
            {e.details.length > 0 ? (
              <Field label="Technical diff">
                <table className="w-full text-[12px]">
                  <thead className="text-left text-[10px] uppercase text-gray-400"><tr><th>Property</th><th>Before</th><th>After</th></tr></thead>
                  <tbody>{e.details.map((d) => <tr key={d.detailId} className="border-t"><td className="py-1 font-mono text-[11px]">{d.propertyPath}</td><td className="font-mono text-[11px] text-red-600">{String(d.beforeValue ?? "—")}</td><td className="font-mono text-[11px] text-emerald-700">{String(d.afterValue ?? "—")}</td></tr>)}</tbody>
                </table>
              </Field>
            ) : <div className="text-[11px] text-gray-400">No before/after diff from the source.</div>}
            {e.rollbackHint && (
              <Field label="Inspect / revert (read-only — copy & run yourself)">
                <div className="relative">
                  <pre className="overflow-x-auto rounded bg-gray-900 p-2 pr-16 text-[10px] leading-relaxed text-amber-200">{e.rollbackHint}</pre>
                  <button onClick={() => void navigator.clipboard?.writeText(e.rollbackHint ?? "")}
                    className="absolute right-1 top-1 rounded border border-gray-600 bg-gray-800 px-1.5 py-0.5 text-[10px] text-gray-200 hover:bg-gray-700">⧉ Copy</button>
                </div>
              </Field>
            )}
          </>}
          {section === "raw" && (
            rawLoading ? <Skeleton rows={6} />
            : <pre className="max-h-[70vh] overflow-auto rounded bg-gray-900 p-2 text-[10px] text-emerald-300">{JSON.stringify(raw ?? {}, null, 2)}</pre>
          )}
        </div>
      </div>
    </div>
  );
}

const SEC_SEV_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-700 border-red-300",
  high: "bg-orange-100 text-orange-700 border-orange-300",
  medium: "bg-amber-100 text-amber-700 border-amber-300",
  low: "bg-blue-100 text-blue-700 border-blue-300",
};
function SecFlagChip({ flag }: { flag: { code: string; label: string; severity: string } }) {
  return <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${SEC_SEV_STYLE[flag.severity] || "bg-gray-100 text-gray-600 border-gray-300"}`}>{flag.label}</span>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><div className="text-[11px] font-medium uppercase text-gray-400">{label}</div><div className="mt-0.5 text-gray-700">{children}</div></div>;
}
function KV({ k, v }: { k: string; v: string }) {
  return <div className="truncate"><span className="text-gray-400">{k}:</span> <span className="text-gray-700">{v}</span></div>;
}
function Empty() { return <div className="rounded-xl border border-dashed bg-gray-50 p-8 text-center text-sm text-gray-500">No changes match.</div>; }

// ---- Performance helpers (P1/P3) ------------------------------------------------------------
// Virtualized vertical list: renders only the rows in view. ``estimateSize`` is the row height.
function VirtualList<T>({ items, estimateSize = 40, max = "60vh", render }: {
  items: T[]; estimateSize?: number; max?: string; render: (item: T, index: number) => React.ReactNode;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virt = useVirtualizer({
    count: items.length, getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize, overscan: 12,
  });
  return (
    <div ref={parentRef} className="overflow-auto rounded-xl border bg-white" style={{ maxHeight: max }}>
      <div style={{ height: virt.getTotalSize(), position: "relative" }}>
        {virt.getVirtualItems().map((vi) => (
          <div key={vi.key} ref={virt.measureElement} data-index={vi.index}
            style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}>
            {render(items[vi.index], vi.index)}
          </div>
        ))}
      </div>
    </div>
  );
}
// Debounce a fast-changing value (search inputs) so list re-filtering doesn't run every keystroke.
function useDebounced<T>(value: T, ms = 150): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}
// Lightweight skeleton block while a tab renders / a run loads (U5).
function Skeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-9 animate-pulse rounded-lg bg-gray-100" />
      ))}
    </div>
  );
}

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
