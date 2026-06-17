import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Markdown } from "./LazyMarkdown";
import {
  api,
  streamPerfRefresh,
  type PerfBottleneck,
  type PerfMetricCell,
  type PerfProfile,
  type PerfResourceRow,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { queryClient } from "../queryClient";
import { TrendChart } from "./TrendChart";
import { AllResourcesTab } from "./AllResourcesTab";
import { SubscriptionScopePicker } from "./SubscriptionScopePicker";

// ---- Background profile-run registry --------------------------------------------
// A profile started from this screen keeps streaming even if the user switches scope or
// navigates away — so it truly runs "in the background". The registry is keyed by
// scopeKey (`${scopeKind}:${scopeId}`) so the optimistic "Running" row and the button
// state are PER-SCOPE: a scope with no active run shows "▶ Run profile", not "Profiling…".
// On completion it invalidates the history-grid query via the shared queryClient so the
// finished run replaces the optimistic row even if the originating component unmounted.
type RunningProfile = {
  scopeKey: string;
  scopeLabel: string;
  windowLabel: string;
  startedAt: number;
  lastResource: string;
  steps: number;
};

const _runningProfiles = new Map<string, RunningProfile>();
let _lastCompleted: { scopeKey: string; snap: PerfProfile; at: number } | null = null;
let _runVersion = 0;
const _runListeners = new Set<() => void>();

function _bumpRuns() {
  _runVersion += 1;
  for (const l of _runListeners) l();
}

// Subscribe a component to registry changes; returns a monotonically-increasing version
// so effects can depend on "something changed".
function useProfileRuns(): number {
  return useSyncExternalStore(
    (cb) => {
      _runListeners.add(cb);
      return () => {
        _runListeners.delete(cb);
      };
    },
    () => _runVersion,
    () => _runVersion,
  );
}

function startBackgroundProfile(opts: {
  scopeKey: string;
  scopeLabel: string;
  windowLabel: string;
  body: { workload_id?: string; subscription_id?: string; window?: string; start_time?: string; end_time?: string };
  runsKey: readonly unknown[];
  trendKey: readonly unknown[];
  onError: (msg: string) => void;
}) {
  if (_runningProfiles.has(opts.scopeKey)) return;
  _runningProfiles.set(opts.scopeKey, {
    scopeKey: opts.scopeKey,
    scopeLabel: opts.scopeLabel,
    windowLabel: opts.windowLabel,
    startedAt: Date.now(),
    lastResource: "",
    steps: 0,
  });
  _bumpRuns();
  void streamPerfRefresh(opts.body, {
    onProgress: (d) => {
      const e = _runningProfiles.get(opts.scopeKey);
      if (e) {
        e.lastResource = d.resource;
        e.steps += 1;
        _bumpRuns();
      }
    },
    onDone: (snap) => {
      _runningProfiles.delete(opts.scopeKey);
      _lastCompleted = { scopeKey: opts.scopeKey, snap, at: Date.now() };
      _bumpRuns();
      // Refresh the history grid + trend wherever they're mounted (originating component
      // may have unmounted). The just-saved run replaces the optimistic "Running" row.
      void queryClient.invalidateQueries({ queryKey: opts.runsKey });
      void queryClient.invalidateQueries({ queryKey: opts.trendKey });
    },
    onError: (m) => {
      _runningProfiles.delete(opts.scopeKey);
      _bumpRuns();
      opts.onError(m);
    },
  }).catch((err) => {
    _runningProfiles.delete(opts.scopeKey);
    _bumpRuns();
    opts.onError(formatError(err));
  });
}

const STATE_TONE: Record<string, string> = {
  breaching: "bg-red-500",
  approaching: "bg-amber-500",
  healthy: "bg-green-500",
  no_data: "bg-gray-300",
};
const STATE_TEXT: Record<string, string> = {
  breaching: "text-red-600",
  approaching: "text-amber-600",
  healthy: "text-green-600",
  no_data: "text-gray-400",
};
const STATE_CELL: Record<string, string> = {
  breaching: "bg-red-100 text-red-700 border-red-200",
  approaching: "bg-amber-100 text-amber-700 border-amber-200",
  healthy: "bg-green-50 text-green-700 border-green-200",
  no_data: "bg-gray-50 text-gray-400 border-gray-200",
};

function fmtTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function scoreTone(score: number): string {
  if (score >= 80) return "text-green-600";
  if (score >= 50) return "text-amber-600";
  return "text-red-600";
}

// Circular performance-score gauge (mirrors the Telemetry Coverage donut). Shows the
// 0-100 score with a green/amber/red ring; a null score renders a muted placeholder.
function ScoreDonut({ score }: { score: number | null | undefined }) {
  const r = 34;
  const c = 2 * Math.PI * r;
  const pct = typeof score === "number" ? Math.max(0, Math.min(100, score)) : null;
  const dash = ((pct ?? 0) / 100) * c;
  const color = pct == null ? "#e5e7eb" : pct >= 80 ? "#16a34a" : pct >= 50 ? "#d97706" : "#dc2626";
  return (
    <svg viewBox="0 0 80 80" className="h-20 w-20 shrink-0">
      <circle cx="40" cy="40" r={r} fill="none" stroke="#e5e7eb" strokeWidth="8" />
      {pct != null && (
        <circle cx="40" cy="40" r={r} fill="none" stroke={color} strokeWidth="8" strokeLinecap="round"
          strokeDasharray={`${dash} ${c - dash}`} transform="rotate(-90 40 40)" />
      )}
      <text x="40" y="45" textAnchor="middle" className="fill-gray-900 text-[18px] font-semibold">{pct ?? "—"}</text>
    </svg>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

// Sparkline with the AMBA threshold drawn as a red line.
function Sparkline({ cell }: { cell: PerfMetricCell }) {
  const W = 260, H = 60, pad = 4;
  const pts = cell.series ?? [];
  if (pts.length < 2) return <div className="text-[11px] text-gray-400">no series</div>;
  const vals = pts.map((p) => p.value);
  const lo = Math.min(...vals, cell.threshold ?? Infinity);
  const hi = Math.max(...vals, cell.threshold ?? -Infinity);
  const span = hi - lo || 1;
  const y = (v: number) => H - pad - ((v - lo) / span) * (H - 2 * pad);
  const x = (i: number) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
  const path = vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const thrY = cell.threshold != null ? y(cell.threshold) : null;
  return (
    <svg width={W} height={H} className="rounded border bg-white">
      {thrY != null && (
        <line x1={pad} y1={thrY} x2={W - pad} y2={thrY} stroke="#ef4444" strokeWidth="1" strokeDasharray="3 2" />
      )}
      <path d={path} fill="none" stroke="#2563eb" strokeWidth="1.5" />
    </svg>
  );
}

export function PerformancePanel() {
  const navigate = useNavigate();
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.performance.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.performance.workloadId", "");
  const [subId, setSubId] = usePersistedState("azsup.performance.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.performance.subName", "");
  const [windowSel, setWindowSel] = useState("P1D");
  const [useRange, setUseRange] = useState(false);
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [drawer, setDrawer] = useState<PerfResourceRow | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [ticketOpen, setTicketOpen] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  // Which sub-tab of the analysis is shown: the metric heatmap or the full resource list.
  const [perfTab, setPerfTab] = useState<"analysis" | "all">("analysis");
  // The run currently shown below the grid (selected from history, or just-completed).
  const [data, setData] = useState<PerfProfile | null>(null);

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );
  const workloads = workloadsQ.data?.workloads ?? [];
  const effWorkloadId =
    scopeKind === "workload"
      ? workloadId || workloads.find((w) => w.id === "demo-amba-coverage")?.id || workloads[0]?.id || ""
      : "";
  const params = scopeKind === "workload" ? { workload_id: effWorkloadId } : { subscription_id: subId };
  const enabled = scopeKind === "workload" ? !!effWorkloadId : !!subId;

  // Per-scope background-run state. A profile is identified by its scope so switching to a
  // scope with no active run shows "▶ Run profile" while the other keeps profiling.
  const scopeKey = `${scopeKind}:${effWorkloadId || subId}`;
  const runsVersion = useProfileRuns();
  const runningEntry = _runningProfiles.get(scopeKey) ?? null;
  const runningHere = runningEntry !== null;

  // History grid — fetched on load + after each run/delete. NO auto-profiling.
  const runsQ = useQuery({
    queryKey: ["perf-runs", scopeKind, effWorkloadId, subId],
    queryFn: () => api.perfRuns(params),
    enabled,
  });
  const runs = runsQ.data?.runs ?? [];

  // Trash — trashed runs for this scope, only fetched when the Trash panel is open.
  const trashQ = useQuery({
    queryKey: ["perf-runs-trash", scopeKind, effWorkloadId, subId],
    queryFn: () => api.perfTrashedRuns(params),
    enabled: enabled && showTrash,
  });
  const trashed = trashQ.data?.runs ?? [];

  // Performance-score trend over time (loads with the scope; refetched after each run).
  const trendQ = useQuery({
    queryKey: ["perf-trend", scopeKind, effWorkloadId, subId],
    queryFn: () => api.coverageTrend("performance", params),
    enabled,
  });

  // Clear the shown run when the scope changes.
  useEffect(() => {
    setData(null);
  }, [scopeKind, effWorkloadId, subId]);

  // Auto-show a run that finished in the background while viewing its scope. Only surfaces
  // completions that happen after this component mounted (older ones stay in the grid only).
  const seenCompletionRef = useRef(Date.now());
  useEffect(() => {
    if (_lastCompleted && _lastCompleted.scopeKey === scopeKey && _lastCompleted.at > seenCompletionRef.current) {
      seenCompletionRef.current = _lastCompleted.at;
      setData(_lastCompleted.snap);
    }
  }, [runsVersion, scopeKey]);

  const WINDOWS = [
    { v: "PT1H", l: "Last 1 hour" },
    { v: "PT6H", l: "Last 6 hours" },
    { v: "PT12H", l: "Last 12 hours" },
    { v: "P1D", l: "Last 1 day" },
    { v: "P3D", l: "Last 3 days" },
    { v: "P7D", l: "Last 7 days" },
    { v: "P30D", l: "Last 30 days" },
  ];

  const metricCols = useMemo(() => {
    const seen = new Set<string>();
    const cols: { metric: string; name: string }[] = [];
    for (const r of data?.resources ?? []) {
      for (const c of r.cells) {
        if (!seen.has(c.metric)) {
          seen.add(c.metric);
          cols.push({ metric: c.metric, name: c.name });
        }
      }
    }
    return cols;
  }, [data]);

  function runProfile() {
    if (!enabled || runningHere) return;
    setMsg(null);
    const windowLabel =
      useRange && startTime && endTime ? "custom range" : WINDOWS.find((w) => w.v === windowSel)?.l ?? windowSel;
    const scopeLabel =
      scopeKind === "workload"
        ? workloads.find((w) => w.id === effWorkloadId)?.name ?? effWorkloadId
        : subName || subId;
    const body = {
      ...params,
      ...(useRange && startTime && endTime
        ? { start_time: new Date(startTime).toISOString(), end_time: new Date(endTime).toISOString() }
        : { window: windowSel }),
    };
    // Fire-and-forget: the run streams in the registry, survives scope switches/navigation,
    // and lands in the history grid on completion (the optimistic row shows meanwhile).
    startBackgroundProfile({
      scopeKey,
      scopeLabel,
      windowLabel,
      body,
      runsKey: ["perf-runs", scopeKind, effWorkloadId, subId],
      trendKey: ["perf-trend", scopeKind, effWorkloadId, subId],
      onError: (m) => setMsg({ text: m, ok: false }),
    });
  }

  async function viewRun(runId: string) {
    setBusy(`view:${runId}`);
    setMsg(null);
    try {
      const r = await api.perfRun(runId);
      if (r.ok && r.run) setData(r.run);
      else setMsg({ text: r.detail || "Run not found.", ok: false });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function deleteRun(runId: string) {
    if (!window.confirm("Move this profile run to the Trash?")) return;
    setBusy(`del:${runId}`);
    setMsg(null);
    try {
      await api.deletePerfRun(runId);
      if (data?.id === runId) setData(null);
      await runsQ.refetch();
      trashQ.refetch();
      setMsg({ text: "Moved to Trash. Restore it from the Trash panel.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function restoreRun(runId: string) {
    setBusy(`restore:${runId}`);
    setMsg(null);
    try {
      await api.restorePerfRun(runId);
      await Promise.all([runsQ.refetch(), trashQ.refetch()]);
      setMsg({ text: "Restored profile run.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function purgeRun(runId: string) {
    if (!window.confirm("Permanently delete this profile run? This cannot be undone.")) return;
    setBusy(`purge:${runId}`);
    setMsg(null);
    try {
      await api.purgePerfRun(runId);
      await trashQ.refetch();
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function emptyTrash() {
    if (!window.confirm("Permanently delete ALL trashed profile runs for this scope? This cannot be undone.")) return;
    setBusy("empty-trash");
    setMsg(null);
    try {
      await api.emptyPerfTrash(params);
      await trashQ.refetch();
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function registerFindings() {
    if (scopeKind !== "workload" || !effWorkloadId || !data) {
      setMsg({ text: "Run a profile on a workload scope first.", ok: false });
      return;
    }
    setBusy("findings");
    setMsg(null);
    try {
      const r = await api.registerPerfFindings({
        workload_id: effWorkloadId,
        workload_name: data.scope_name ?? "",
        bottlenecks: data.bottlenecks ?? [],
      });
      setMsg({ text: `Registered ${r.finding_count} Performance-pillar finding(s).`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function createTicket(b: PerfBottleneck, connectorId: string) {
    setBusy("ticket");
    setMsg(null);
    try {
      const r = await api.createPerfTicket({ connector_id: connectorId, bottleneck: b });
      setMsg({ text: r.ok ? `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.` : r.detail || "Ticket failed.", ok: !!r.ok });
      setTicketOpen(false);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  function investigate(b: PerfBottleneck) {
    const prompt =
      `War Room: investigate the performance bottleneck on "${b.resource_name}" (${b.resource_type}). ` +
      `${b.metric_name} = ${b.observed}${b.unit} vs AMBA threshold ${b.threshold}${b.unit} (${b.pct_of_threshold}% of threshold, ${b.state}). ` +
      `Confirm current load, identify the cause, and recommend scale/tuning before peak load breaches it.`;
    try {
      sessionStorage.setItem("azsup.warRoomHandoff", JSON.stringify({ workloadId: effWorkloadId, prompt }));
    } catch {
      /* ignore */
    }
    navigate("/chat");
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header + controls */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-4">
          <ScoreDonut score={data?.scorecard?.workload_score ?? runs[0]?.workload_score ?? null} />
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">🔥 Performance Profiler</h1>
            <p className="text-xs text-gray-500">
              Profile a workload's metrics against its AMBA thresholds to find the binding bottleneck. Runs are kept as history — pick a window and click Run. Read-only.
            </p>
            {data?.scorecard && (
              <div className="mt-1 flex flex-wrap gap-3 text-xs text-gray-600">
                <span className="text-red-600">{data.scorecard.breaching} breaching</span>
                <span className="text-amber-600">{data.scorecard.approaching} approaching</span>
                <span className="text-green-600">{data.scorecard.healthy} healthy</span>
                <span>· {data.scorecard.resources_profiled} resource(s) profiled</span>
              </div>
            )}
          </div>
          {enabled && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Score trend</span>
              <TrendChart points={trendQ.data?.points ?? []} current={trendQ.data?.current} previous={trendQ.data?.previous} delta={trendQ.data?.delta} loading={trendQ.isLoading} unit="" deltaLabel="vs last run" />
            </div>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              <button
                onClick={() => setScopeKind("workload")}
                className={`rounded-md px-2.5 py-1 ${scopeKind === "workload" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}
              >
                Workload
              </button>
              <button
                onClick={() => setScopeKind("subscription")}
                className={`rounded-md px-2.5 py-1 ${scopeKind === "subscription" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}
              >
                Subscription
              </button>
            </div>
            {scopeKind === "workload" ? (
              <select value={effWorkloadId} onChange={(e) => setWorkloadId(e.target.value)} className="max-w-[220px] rounded-lg border px-2 py-1.5 text-xs">
                {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            ) : (
              <SubscriptionScopePicker
                value={subId}
                valueName={subName}
                onPick={(id, name) => {
                  setSubId(id);
                  setSubName(name);
                }}
              />
            )}
            {!useRange ? (
              <select value={windowSel} onChange={(e) => setWindowSel(e.target.value)} className="rounded-md border px-2 py-1.5 text-sm" title="Metric window">
                {WINDOWS.map((w) => <option key={w.v} value={w.v}>{w.l}</option>)}
              </select>
            ) : (
              <div className="flex items-center gap-1">
                <input type="datetime-local" value={startTime} onChange={(e) => setStartTime(e.target.value)} className="rounded-md border px-2 py-1 text-xs" />
                <span className="text-xs text-gray-400">→</span>
                <input type="datetime-local" value={endTime} onChange={(e) => setEndTime(e.target.value)} className="rounded-md border px-2 py-1 text-xs" />
              </div>
            )}
            <label className="flex items-center gap-1 text-xs text-gray-600">
              <input type="checkbox" checked={useRange} onChange={(e) => setUseRange(e.target.checked)} /> custom range
            </label>
            <button
              onClick={runProfile}
              disabled={runningHere || !enabled || (useRange && (!startTime || !endTime))}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              data-testid="perf-run-button"
            >
              {runningHere ? "Profiling…" : "▶ Run profile"}
            </button>
            <button
              onClick={() => setShowTrash((v) => !v)}
              title="Show trashed profile runs"
              className={`rounded-md border px-3 py-1.5 text-sm font-medium ${showTrash ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
            >
              🗑 Trash
            </button>
          </div>
        </div>
        {runningHere && runningEntry?.lastResource && (
          <div className="mt-1 truncate text-[11px] text-gray-500">profiling… {runningEntry.lastResource}</div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {msg && (
          <div className={`mb-3 rounded-md border px-3 py-2 text-sm ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}

        {/* History grid (top) */}
        <div className="mb-5">
          <div className="mb-2 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-gray-900">Profile history</h2>
            <span className="text-[11px] text-gray-400">{runs.length} run(s) for this scope</span>
            {runningHere && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />1 running
              </span>
            )}
          </div>
          <div className="overflow-x-auto rounded-lg border bg-white">
            <table className="w-full text-[12px]">
              <thead className="bg-gray-50 text-left text-gray-500">
                <tr>
                  <th className="px-3 py-2">Run time</th>
                  <th className="px-3 py-2">Window</th>
                  <th className="px-3 py-2">Score</th>
                  <th className="px-3 py-2">Breach / Approach / Healthy</th>
                  <th className="px-3 py-2">Top bottleneck</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {runningHere && runningEntry && (
                  <tr className="border-t bg-amber-50/50" data-testid="perf-running-row">
                    <td className="px-3 py-2 text-gray-700">{fmtTime(new Date(runningEntry.startedAt).toISOString())}</td>
                    <td className="px-3 py-2 text-gray-500">{runningEntry.windowLabel}</td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />Running
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-400" colSpan={2}>
                      Profiling… {runningEntry.lastResource || "gathering metrics"}
                      {runningEntry.steps ? ` (${runningEntry.steps} step${runningEntry.steps === 1 ? "" : "s"})` : ""}
                    </td>
                    <td className="px-3 py-2 text-right text-gray-300">—</td>
                  </tr>
                )}
                {runsQ.isLoading ? (
                  <tr><td colSpan={6} className="px-3 py-4 text-center text-gray-400">Loading history…</td></tr>
                ) : runs.length === 0 && !runningHere ? (
                  <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">No profiles yet — pick a window and click <b>Run profile</b>.</td></tr>
                ) : (
                  runs.map((r) => (
                    <tr key={r.id} className={`border-t hover:bg-gray-50 ${data?.id === r.id ? "bg-blue-50" : ""}`}>
                      <td className="px-3 py-2 text-gray-700">{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</td>
                      <td className="px-3 py-2 text-gray-500">{r.requested_start && r.requested_end ? "custom range" : r.window}</td>
                      <td className={`px-3 py-2 font-semibold ${r.workload_score != null ? scoreTone(r.workload_score) : ""}`}>{r.workload_score ?? "—"}</td>
                      <td className="px-3 py-2">
                        <span className="text-red-600">{r.breaching}</span> / <span className="text-amber-600">{r.approaching}</span> / <span className="text-green-600">{r.healthy}</span>
                      </td>
                      <td className="px-3 py-2 text-gray-600">
                        {r.top_bottleneck ? `${r.top_bottleneck.resource_name} · ${r.top_bottleneck.metric_name} (${r.top_bottleneck.pct_of_threshold}%)` : "—"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button onClick={() => viewRun(r.id)} disabled={busy === `view:${r.id}`} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">View</button>
                        <button onClick={() => deleteRun(r.id)} disabled={busy === `del:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete</button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Trash panel (soft-deleted runs for this scope) */}
        {showTrash && (
          <div className="mb-5 rounded-lg border bg-white">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <div className="flex items-center gap-2">
                <h2 className="flex items-center gap-2 text-sm font-semibold text-gray-900">🗑 Trash</h2>
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">{trashed.length}</span>
              </div>
              {trashed.length > 0 && (
                <button onClick={() => void emptyTrash()} disabled={busy === "empty-trash"}
                  className="rounded-md border border-red-200 px-2.5 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Empty trash</button>
              )}
            </div>
            <p className="border-b px-3 py-1.5 text-[11px] text-gray-500">Deleted profile runs are kept here until you restore or permanently delete them.</p>
            {trashQ.isLoading ? (
              <div className="px-3 py-4 text-center text-sm text-gray-400">Loading…</div>
            ) : trashed.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-gray-400">Trash is empty.</div>
            ) : (
              <table className="w-full text-[12px]">
                <thead className="bg-gray-50 text-left text-gray-500">
                  <tr>
                    <th className="px-3 py-2">Run time</th>
                    <th className="px-3 py-2">Window</th>
                    <th className="px-3 py-2">Score</th>
                    <th className="px-3 py-2">Top bottleneck</th>
                    <th className="px-3 py-2">Deleted</th>
                    <th className="px-3 py-2 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {trashed.map((r) => (
                    <tr key={r.id} className="border-t hover:bg-gray-50">
                      <td className="px-3 py-2 text-gray-700">{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</td>
                      <td className="px-3 py-2 text-gray-500">{r.requested_start && r.requested_end ? "custom range" : r.window}</td>
                      <td className={`px-3 py-2 font-semibold ${r.workload_score != null ? scoreTone(r.workload_score) : ""}`}>{r.workload_score ?? "—"}</td>
                      <td className="px-3 py-2 text-gray-600">{r.top_bottleneck ? `${r.top_bottleneck.resource_name} · ${r.top_bottleneck.metric_name}` : "—"}</td>
                      <td className="px-3 py-2 text-gray-400">{r.deleted_at ? fmtTime(r.deleted_at) : "—"}</td>
                      <td className="px-3 py-2 text-right">
                        <button onClick={() => void restoreRun(r.id)} disabled={busy === `restore:${r.id}`}
                          className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50">↩ Restore</button>
                        <button onClick={() => void purgeRun(r.id)} disabled={busy === `purge:${r.id}`}
                          className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
        {runningHere && !data ? (
          <div className="flex flex-col items-center justify-center rounded-lg border border-dashed bg-white p-10 text-center">
            <svg className="h-7 w-7 animate-spin text-brand" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8V0C5.4 0 0 5.4 0 12h4z" />
            </svg>
            <div className="mt-3 text-sm font-medium text-gray-700">Profiling workload…</div>
            <div className="mt-1 max-w-md truncate text-xs text-gray-400">
              {runningEntry?.lastResource || "Gathering metrics and evaluating against AMBA thresholds…"}
            </div>
            <div className="mt-1 text-[11px] text-gray-400">{runningEntry?.steps ?? 0} step(s) · runs in the background, you can navigate away</div>
          </div>
        ) : !data ? (
          <div className="rounded-lg border border-dashed bg-white p-8 text-center text-sm text-gray-400">
            {enabled ? "Run a profile or click View on a historic run to see the analysis here." : "Pick a scope to begin."}
          </div>
        ) : (
          <>
            <div className="mb-2 flex items-center gap-2 text-[11px] text-gray-400">
              Showing run from {fmtTime(data.run_at || data.generated_at)} · {data.demo ? "demo data · " : data.connection_configured ? "" : "no Azure connection · "}window {data.window}
              {data.error ? ` · ${data.error}` : ""}
            </div>
            {/* Bottleneck banner */}
            {data.top_bottleneck ? (
              <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
                <div className="text-sm font-semibold text-red-800">
                  ⚠ Binding bottleneck: {data.top_bottleneck.resource_name} · {data.top_bottleneck.metric_name}
                </div>
                <div className="mt-0.5 text-xs text-red-700">
                  {data.top_bottleneck.observed}{data.top_bottleneck.unit} vs threshold {data.top_bottleneck.threshold}{data.top_bottleneck.unit}
                  {" "}({data.top_bottleneck.pct_of_threshold}% of threshold{data.top_bottleneck.trend_pct ? `, trending ${data.top_bottleneck.trend_pct > 0 ? "+" : ""}${data.top_bottleneck.trend_pct}%` : ""}) — {data.top_bottleneck.state}
                </div>
              </div>
            ) : (
              <div className="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800">
                ✓ No bottlenecks — all profiled metrics are within their AMBA thresholds.
              </div>
            )}

            {data.narrative && (
              <div className="mb-4 rounded-lg border bg-white px-3 py-2 text-sm text-gray-700">
                <Markdown>
                  {data.narrative}
                </Markdown>
              </div>
            )}

            <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
              <Stat label="Performance score" value={`${data.scorecard.workload_score}`} tone={scoreTone(data.scorecard.workload_score)} />
              <Stat label="Resources profiled" value={`${data.scorecard.resources_profiled}`} />
              <Stat label="Breaching" value={`${data.scorecard.breaching}`} tone={data.scorecard.breaching ? "text-red-600" : undefined} />
              <Stat label="Approaching" value={`${data.scorecard.approaching}`} tone={data.scorecard.approaching ? "text-amber-600" : undefined} />
              <Stat label="Healthy" value={`${data.scorecard.healthy}`} tone="text-green-600" />
            </div>

            {/* Sub-tabs: the metric heatmap vs the full in-scope resource list */}
            <div className="mb-3 flex gap-1 border-b">
              {([
                ["analysis", "Heatmap"],
                ["all", "All Resources"],
              ] as const).map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setPerfTab(id)}
                  className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-medium transition ${
                    perfTab === id ? "border-brand text-brand" : "border-transparent text-gray-500 hover:text-gray-700"
                  }`}
                >
                  {label}
                  {id === "all" && (data.all_resources?.length ?? 0) > 0 ? (
                    <span className="ml-1 rounded bg-gray-100 px-1.5 text-[10px] text-gray-600">{data.all_resources!.length}</span>
                  ) : null}
                </button>
              ))}
            </div>

            {perfTab === "all" ? (
              <AllResourcesTab resources={data.all_resources ?? []} />
            ) : (
            <>
            <div className="mb-2 flex items-center gap-2">
              <h2 className="text-sm font-semibold text-gray-900">Heatmap — resources × AMBA metrics</h2>
              <span className="text-[11px] text-gray-400">cell = % of its AMBA threshold</span>
              <button onClick={registerFindings} disabled={busy === "findings" || (data.bottlenecks ?? []).length === 0} className="ml-auto rounded-md border bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50">🛡️ Register findings</button>
            </div>

            <div className="overflow-x-auto rounded-lg border bg-white">
              <table className="w-full text-[12px]">
                <thead className="bg-gray-50 text-left text-gray-500">
                  <tr>
                    <th className="sticky left-0 bg-gray-50 px-2 py-2">Resource</th>
                    <th className="px-2 py-2">Score</th>
                    {metricCols.map((c) => (
                      <th key={c.metric} className="px-2 py-2 text-center" title={c.metric}>{c.name}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.resources.map((r) => {
                    const byMetric: Record<string, PerfMetricCell> = {};
                    for (const c of r.cells) byMetric[c.metric] = c;
                    return (
                      <tr key={r.resource_id} className="cursor-pointer border-t hover:bg-gray-50" onClick={() => setDrawer(r)}>
                        <td className="sticky left-0 bg-white px-2 py-1.5">
                          <div className="flex items-center gap-1.5">
                            <span className={`inline-block h-2 w-2 rounded-full ${STATE_TONE[r.state]}`} />
                            <span className="font-medium text-gray-800">{r.resource_name}</span>
                          </div>
                          <div className="text-[10px] text-gray-400">{r.display}</div>
                        </td>
                        <td className={`px-2 py-1.5 font-semibold ${scoreTone(r.score)}`}>{r.score}</td>
                        {metricCols.map((mc) => {
                          const cell = byMetric[mc.metric];
                          if (!cell) return <td key={mc.metric} className="px-2 py-1.5 text-center text-gray-200">·</td>;
                          return (
                            <td key={mc.metric} className="px-1 py-1 text-center">
                              <span className={`inline-block min-w-[44px] rounded border px-1 py-0.5 text-[11px] ${STATE_CELL[cell.state]}`} title={`${cell.observed ?? "?"}${cell.unit} vs ${cell.threshold ?? "—"}${cell.unit}`}>
                                {cell.pct_of_threshold != null ? `${cell.pct_of_threshold}%` : cell.state === "no_data" ? "—" : "ok"}
                              </span>
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {(data.bottlenecks ?? []).length > 0 && (
              <div className="mt-6">
                <h2 className="mb-2 text-sm font-semibold text-gray-900">Ranked bottlenecks</h2>
                <div className="space-y-1.5">
                  {data.bottlenecks.slice(0, 12).map((b, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2 rounded-lg border bg-white px-3 py-2 text-sm">
                      <span className={`inline-block h-2 w-2 rounded-full ${STATE_TONE[b.state]}`} />
                      <span className="font-medium text-gray-800">{b.resource_name}</span>
                      <span className="text-gray-500">{b.metric_name}</span>
                      <span className={STATE_TEXT[b.state]}>{b.observed}{b.unit} / {b.threshold}{b.unit} ({b.pct_of_threshold}%)</span>
                      {b.trend_pct ? <span className="text-[11px] text-gray-400">trend {b.trend_pct > 0 ? "+" : ""}{b.trend_pct}%</span> : null}
                      <div className="ml-auto flex gap-1.5">
                        <button onClick={() => investigate(b)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">🔎 War Room</button>
                        {i === 0 && (
                          <div className="relative">
                            <button onClick={() => setTicketOpen(!ticketOpen)} disabled={ticketConnectors.length === 0} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">🎫 Ticket</button>
                            {ticketOpen && (
                              <div className="absolute right-0 z-10 mt-1 w-48 rounded-md border bg-white shadow-lg">
                                {ticketConnectors.map((c) => (
                                  <button key={c.id} onClick={() => createTicket(b, c.id)} className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-50">{c.name} ({c.type})</button>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            </>
            )}
          </>
        )}
      </div>

      {/* Drill drawer */}
      {drawer && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={() => setDrawer(null)}>
          <div className="h-full w-full max-w-lg overflow-auto bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between border-b px-5 py-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-2.5 w-2.5 rounded-full ${STATE_TONE[drawer.state]}`} />
                  <h3 className="text-base font-semibold text-gray-900">{drawer.resource_name}</h3>
                  <span className={`text-sm font-semibold ${scoreTone(drawer.score)}`}>{drawer.score}/100</span>
                </div>
                <div className="text-[11px] text-gray-400">{drawer.display} · {drawer.region}</div>
              </div>
              <button onClick={() => setDrawer(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
            </div>
            <div className="space-y-4 px-5 py-4">
              {drawer.cells.map((c) => (
                <div key={c.alert_key} className="rounded-lg border p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <span className={`inline-block h-2 w-2 rounded-full ${STATE_TONE[c.state]}`} />
                    <span className="text-sm font-medium text-gray-800">{c.name}</span>
                    <span className={`ml-auto text-[12px] ${STATE_TEXT[c.state]}`}>
                      {c.observed ?? "?"}{c.unit} / {c.threshold ?? "—"}{c.unit}{c.pct_of_threshold != null ? ` (${c.pct_of_threshold}%)` : ""}
                    </span>
                  </div>
                  <Sparkline cell={c} />
                  <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-gray-500">
                    <span>peak {c.peak}{c.unit}</span>
                    <span>avg {c.avg}{c.unit}</span>
                    {c.trend_pct ? <span>trend {c.trend_pct > 0 ? "+" : ""}{c.trend_pct}%</span> : null}
                    {c.headroom_pct != null ? <span>headroom {c.headroom_pct}%</span> : null}
                  </div>
                  {c.why && <div className="mt-1 text-[11px] text-gray-400">{c.why}</div>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
