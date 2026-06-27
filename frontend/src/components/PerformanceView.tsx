import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Markdown } from "./LazyMarkdown";
import {
  api,
  streamPerfRefresh,
  type PerfBottleneck,
  type PerfMetricCell,
  type PerfProfile,
  type PerfResourceRow,
  type PerfRunSummary,
} from "../api";
import { formatError } from "../utils/format";
import { useDebounced, Skeleton } from "../utils/perf";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { queryClient } from "../queryClient";
import { TrendChart } from "./TrendChart";
import { AllResourcesTab } from "./AllResourcesTab";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { TimeRangePicker } from "./changeexplorer/TimeRangePicker";
import { RunHistoryShell } from "./RunHistoryShell";
import { PdfGeneratingOverlay } from "./PdfGeneratingOverlay";
import { PerformanceFleet } from "./performance/PerformanceFleet";

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
// Per-scope last error (e.g. Azure throttling exhausted retries) — surfaced by the Fleet view so a
// failed run shows a red "failed — retry" row instead of silently falling back to "never".
const _profileErrors = new Map<string, string>();
let _runVersion = 0;
const _runListeners = new Set<() => void>();

function _bumpRuns() {
  _runVersion += 1;
  for (const l of _runListeners) l();
}

// Subscribe a component to registry changes; returns a monotonically-increasing version
// so effects can depend on "something changed".
export function useProfileRuns(): number {
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

// Read the in-flight profile run for a scope (if any) — used by the Fleet view to show a
// live "Profiling… {resource}" indicator and disable the row's launch button.
export function peekRunningProfile(scopeKey: string): RunningProfile | undefined {
  return _runningProfiles.get(scopeKey);
}

// Plain (non-hook) subscription to run-registry changes — lets the module-level fleet scheduler
// self-drive its queue (re-drain when a run finishes) even while no component is mounted.
export function subscribeProfileRuns(cb: () => void): () => void {
  _runListeners.add(cb);
  return () => {
    _runListeners.delete(cb);
  };
}

// The last error for a scope (if its most recent attempt failed), e.g. Azure throttling. The Fleet
// view reads this to render a red "failed — retry" row + tooltip.
export function peekProfileError(scopeKey: string): string | undefined {
  return _profileErrors.get(scopeKey);
}

// datetime-local helpers for the time-range picker (default = last 24h).
function _pad(n: number): string { return String(n).padStart(2, "0"); }
function FilterChip({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <span className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
      {label}
      <button onClick={onClear} className="text-brand/60 hover:text-brand">✕</button>
    </span>
  );
}
function _toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}
function _perfDefaultEnd(): string { return _toLocalInput(new Date()); }
function _perfDefaultStart(): string { return _toLocalInput(new Date(Date.now() - 24 * 3600_000)); }

export function startBackgroundProfile(opts: {
  scopeKey: string;
  scopeLabel: string;
  windowLabel: string;
  body: { workload_id?: string; subscription_id?: string; connection_id?: string; window?: string; start_time?: string; end_time?: string };
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
  _profileErrors.delete(opts.scopeKey);  // a fresh attempt clears any prior failure
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
      _profileErrors.delete(opts.scopeKey);
      _bumpRuns();
      // Refresh the history grid + trend wherever they're mounted (originating component
      // may have unmounted). The just-saved run replaces the optimistic "Running" row.
      void queryClient.invalidateQueries({ queryKey: opts.runsKey });
      void queryClient.invalidateQueries({ queryKey: opts.trendKey });
      // Cross-view freshness: a Fleet launch must update an open single-scope history/trend, and
      // a single-scope run must update the Fleet table. Broad-prefix invalidations cover both.
      void queryClient.invalidateQueries({ queryKey: ["perf-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["perf-trend"] });
      void queryClient.invalidateQueries({ queryKey: ["perfFleet"] });
    },
    onError: (m) => {
      _runningProfiles.delete(opts.scopeKey);
      _profileErrors.set(opts.scopeKey, m);
      _bumpRuns();
      opts.onError(m);
    },
  }).catch((err) => {
    _runningProfiles.delete(opts.scopeKey);
    _profileErrors.set(opts.scopeKey, formatError(err));
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

// Compact "time since" label, e.g. "45s ago", "12m ago", "3h 20m ago", "2d 4h ago".
function fmtAgo(iso: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${m % 60 ? ` ${m % 60}m` : ""} ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d${h % 24 ? ` ${h % 24}h` : ""} ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

// Parse an ISO-8601 duration token (P1D, P7D, PT4H, PT15M, …) to milliseconds.
function parseIsoDurationMs(d: string): number | null {
  const m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(d || "");
  if (!m) return null;
  const [, days, hours, mins, secs] = m;
  if (!days && !hours && !mins && !secs) return null;
  return ((+(days || 0)) * 86400 + (+(hours || 0)) * 3600 + (+(mins || 0)) * 60 + (+(secs || 0))) * 1000;
}

// Render the profiled time range. Prefers the explicit requested start/end; otherwise derives a
// concrete window ending at the run time from the ISO-8601 duration (e.g. P1D -> run_at-24h → run_at).
function windowCell(r: PerfRunSummary): React.ReactNode {
  if (r.requested_start && r.requested_end) {
    return <span className="whitespace-nowrap">{fmtTime(r.requested_start)} → {fmtTime(r.requested_end)}</span>;
  }
  const ms = parseIsoDurationMs(r.window);
  const end = r.run_at ? new Date(r.run_at).getTime() : NaN;
  if (ms && !isNaN(end)) {
    const start = new Date(end - ms).toISOString();
    return <span className="whitespace-nowrap" title={r.window}>{fmtTime(start)} → {fmtTime(r.run_at)}</span>;
  }
  return r.window;
}

function scoreTone(score: number): string {
  if (score >= 80) return "text-green-600";
  if (score >= 50) return "text-amber-600";
  return "text-red-600";
}

// Deep-link an ARM resource id to the Azure Portal overview blade (current tenant).
function portalHref(resourceId: string): string {
  return `https://portal.azure.com/#@/resource${resourceId}/overview`;
}

// A small "open in Azure Portal" arrow link. Stops click propagation so it doesn't also trigger
// a row's onClick (e.g. opening the drill drawer).
function PortalLink({ resourceId, className = "" }: { resourceId: string; className?: string }) {
  if (!resourceId) return null;
  return (
    <a
      href={portalHref(resourceId)}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      title="Open in Azure Portal"
      className={`shrink-0 text-gray-300 transition hover:text-brand ${className}`}
    >
      ↗
    </a>
  );
}

// The percentage label for a metric cell. CRITICAL: for LOWER-IS-WORSE metrics (availability,
// health-probe, available-memory) the backend's pct_of_threshold is the INVERSE ratio
// (threshold/observed), so a WORSE reading shows a BIGGER number (e.g. a breaching 49.5% vs 90%
// floor renders "181.7%") — backwards and confusing. Reframe lower-is-worse as "% of the floor
// achieved" (observed/threshold), so <100% = below floor = bad and >100% = healthy headroom,
// matching intuition. Higher-is-worse keeps "% of threshold" (toward breach).
function metricPctLabel(cell: { higher_is_worse: boolean; pct_of_threshold: number | null; observed: number | null; threshold: number | null }): string | null {
  if (cell.pct_of_threshold == null) return null;
  if (cell.higher_is_worse) return `${cell.pct_of_threshold}%`;
  if (cell.observed != null && cell.threshold) {
    return `${Math.round((cell.observed / cell.threshold) * 100)}%`;
  }
  return `${cell.pct_of_threshold}%`;
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
  const [connId, setConnId] = usePersistedState("azsup.performance.connId", "");
  useWorkloadDeepLink(setScopeKind, setWorkloadId);
  const [startTime, setStartTime] = useState(() => _perfDefaultStart());
  const [endTime, setEndTime] = useState(() => _perfDefaultEnd());
  const [rangeLabel, setRangeLabel] = useState("Last 1 day");
  const [drawer, setDrawer] = useState<PerfResourceRow | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [ticketOpen, setTicketOpen] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  // Which sub-tab of the analysis is shown: the metric heatmap or the full resource list.
  // PU2 — persisted so the chosen sub-tab survives navigation/reload.
  const [perfTab, setPerfTab] = usePersistedState<"analysis" | "all">("azsup.performance.tab", "analysis");
  // Top-level view: the single-scope Profiler vs the all-workloads Fleet overview.
  const [mainView, setMainView] = usePersistedState<"profiler" | "fleet">("azsup.performance.view", "profiler");
  // The run currently shown below the grid (selected from history, or just-completed).
  const [data, setData] = useState<PerfProfile | null>(null);

  // --- Heatmap filters (client-side, applied to the loaded run's resources) -------------
  const [hmPosture, setHmPosture] = useState<"all" | "problems" | "atrisk" | "healthy">("all");
  const [hmHideNoData, setHmHideNoData] = useState(false);
  const [hmTypes, setHmTypes] = useState<string[]>([]); // empty = all types
  const [hmRegion, setHmRegion] = useState("");
  const [hmScore, setHmScore] = useState<"all" | "crit" | "risk" | "healthy">("all");
  const [hmSearch, setHmSearch] = useState("");
  const dHmSearch = useDebounced(hmSearch, 150);
  // PU2 — persist the sort + prune-empty-columns view prefs.
  const [hmSort, setHmSort] = usePersistedState<"score" | "breaching" | "name">("azsup.performance.hmSort", "score");
  const [hmPrune, setHmPrune] = usePersistedState<boolean>("azsup.performance.hmPrune", true);
  // Popover open-state + refs so the Types dropdown (and the ticket menu) close on an
  // outside click or Escape — the native <details> didn't, which felt buggy.
  const [hmTypesOpen, setHmTypesOpen] = useState(false);
  const hmTypesRef = useRef<HTMLDivElement>(null);
  const ticketRef = useRef<HTMLDivElement>(null);
  const pdfAbortRef = useRef<AbortController | null>(null);
  const hmFiltersActive =
    hmPosture !== "all" || hmHideNoData || hmTypes.length > 0 || hmRegion !== "" || hmScore !== "all" || hmSearch.trim() !== "";
  function clearHeatmapFilters() {
    setHmPosture("all");
    setHmHideNoData(false);
    setHmTypes([]);
    setHmRegion("");
    setHmScore("all");
    setHmSearch("");
  }

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
  const params = scopeKind === "workload" ? { workload_id: effWorkloadId, connection_id: connId } : { subscription_id: subId, connection_id: connId };
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
    staleTime: 5 * 60 * 1000,
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
    staleTime: 5 * 60 * 1000,
  });

  // Clear the shown run when the scope changes.
  useEffect(() => {
    setData(null);
  }, [scopeKind, effWorkloadId, subId, connId]);

  // Reset the heatmap filters whenever a different run is loaded (or cleared). Without this,
  // a "Problems" filter set on a degraded run would hide every row when you then view a
  // healthy run — making it look empty/broken rather than "all healthy".
  useEffect(() => {
    setHmPosture("all");
    setHmHideNoData(false);
    setHmTypes([]);
    setHmRegion("");
    setHmScore("all");
    setHmSearch("");
    setHmTypesOpen(false);
  }, [data?.id]);

  // Close the Types dropdown + ticket menu on an outside click or the Escape key.
  useEffect(() => {
    if (!hmTypesOpen && !ticketOpen) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (hmTypesOpen && hmTypesRef.current && !hmTypesRef.current.contains(t)) setHmTypesOpen(false);
      if (ticketOpen && ticketRef.current && !ticketRef.current.contains(t)) setTicketOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setHmTypesOpen(false);
        setTicketOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [hmTypesOpen, ticketOpen]);

  // Auto-show a run that finished in the background while viewing its scope. Only surfaces
  // completions that happen after this component mounted (older ones stay in the grid only).
  const seenCompletionRef = useRef(Date.now());
  useEffect(() => {
    if (_lastCompleted && _lastCompleted.scopeKey === scopeKey && _lastCompleted.at > seenCompletionRef.current) {
      seenCompletionRef.current = _lastCompleted.at;
      setData(_lastCompleted.snap);
    }
  }, [runsVersion, scopeKey]);

  // Distinct resource types + regions in the loaded run (for the filter controls).
  const hmAllTypes = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of data?.resources ?? []) if (!m.has(r.resource_type)) m.set(r.resource_type, r.display);
    return [...m.entries()].map(([type, display]) => ({ type, display })).sort((a, b) => a.display.localeCompare(b.display));
  }, [data]);
  const hmAllRegions = useMemo(() => {
    const s = new Set<string>();
    for (const r of data?.resources ?? []) if (r.region) s.add(r.region);
    return [...s].sort();
  }, [data]);

  const breachCellCount = (r: PerfResourceRow) => r.cells.filter((c) => c.state === "breaching").length;

  // Apply the heatmap filters + sort to the loaded run's resources.
  const filteredResources = useMemo(() => {
    let rows = data?.resources ?? [];
    if (hmPosture === "problems") rows = rows.filter((r) => r.state === "breaching");
    else if (hmPosture === "atrisk") rows = rows.filter((r) => r.state === "breaching" || r.state === "approaching");
    else if (hmPosture === "healthy") rows = rows.filter((r) => r.state === "healthy");
    if (hmHideNoData) rows = rows.filter((r) => r.state !== "no_data");
    if (hmTypes.length) rows = rows.filter((r) => hmTypes.includes(r.resource_type));
    if (hmRegion) rows = rows.filter((r) => (r.region || "") === hmRegion);
    if (hmScore === "crit") rows = rows.filter((r) => r.score < 50);
    else if (hmScore === "risk") rows = rows.filter((r) => r.score >= 50 && r.score < 80);
    else if (hmScore === "healthy") rows = rows.filter((r) => r.score >= 80);
    const q = dHmSearch.trim().toLowerCase();
    if (q) rows = rows.filter((r) => r.resource_name.toLowerCase().includes(q) || r.display.toLowerCase().includes(q) || r.resource_type.toLowerCase().includes(q));
    const sorted = [...rows];
    if (hmSort === "score") sorted.sort((a, b) => a.score - b.score || a.resource_name.localeCompare(b.resource_name));
    else if (hmSort === "breaching") sorted.sort((a, b) => breachCellCount(b) - breachCellCount(a) || a.score - b.score);
    else sorted.sort((a, b) => a.resource_name.localeCompare(b.resource_name));
    return sorted;
  }, [data, hmPosture, hmHideNoData, hmTypes, hmRegion, hmScore, dHmSearch, hmSort]);

  // Group the heatmap columns by resource type so the header can show a resource-type band
  // above the vertical metric labels. metricCols stays a flat, type-ordered list (all of a
  // type's metrics contiguous) keyed by `${type}|${metric}` so same-named metrics on
  // different types don't collapse into one column. When "prune empty columns" is on the
  // columns derive from the FILTERED rows, so hiding rows also drops their now-empty columns.
  const { metricGroups, metricCols } = useMemo(() => {
    const source = hmPrune ? filteredResources : (data?.resources ?? []);
    const order: string[] = [];
    const map = new Map<string, { type: string; display: string; metrics: { key: string; metric: string; name: string }[] }>();
    for (const r of source) {
      let g = map.get(r.resource_type);
      if (!g) {
        g = { type: r.resource_type, display: r.display, metrics: [] };
        map.set(r.resource_type, g);
        order.push(r.resource_type);
      }
      const seen = new Set(g.metrics.map((m) => m.metric));
      for (const c of r.cells) {
        if (!seen.has(c.metric)) {
          seen.add(c.metric);
          g.metrics.push({ key: `${r.resource_type}|${c.metric}`, metric: c.metric, name: c.name });
        }
      }
    }
    const groups = order.map((t) => map.get(t)!);
    const cols = groups.flatMap((g) => g.metrics.map((m) => ({ ...m, type: g.type })));
    return { metricGroups: groups, metricCols: cols };
  }, [data, filteredResources, hmPrune]);

  // PP1 — windowed heatmap body: only the visible rows are live <tr> (the matrix can be ~200
  // resources × 30+ metric columns). Spacer rows preserve the sticky header + first-column panes.
  const matrixScrollRef = useRef<HTMLDivElement>(null);
  const rowVirt = useVirtualizer({
    count: filteredResources.length,
    getScrollElement: () => matrixScrollRef.current,
    estimateSize: () => 37,
    overscan: 12,
  });
  const vRows = rowVirt.getVirtualItems();
  const padTop = vRows.length ? vRows[0].start : 0;
  const padBottom = vRows.length ? rowVirt.getTotalSize() - vRows[vRows.length - 1].end : 0;

  function runProfile() {
    if (!enabled || runningHere) return;
    setMsg(null);
    const windowLabel = rangeLabel || "custom range";
    const scopeLabel =
      scopeKind === "workload"
        ? workloads.find((w) => w.id === effWorkloadId)?.name ?? effWorkloadId
        : subName || subId;
    const body = {
      ...params,
      start_time: new Date(startTime).toISOString(),
      end_time: new Date(endTime).toISOString(),
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

  // PU1 — deep-link the viewed run: load `?run=` once on mount, and reflect the loaded run id
  // back into the URL so a profile is shareable / restored on reload.
  const [, setSp] = useSearchParams();
  const runDeepLinked = useRef(false);
  useEffect(() => {
    if (runDeepLinked.current) return;
    runDeepLinked.current = true;
    const rid = new URLSearchParams(window.location.search).get("run");
    if (rid) void viewRun(rid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    if (data?.id) next.set("run", data.id); else next.delete("run");
    setSp(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.id]);

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

  // Branded PDF for the currently-viewed run (by id), or the scope's latest if unsaved.
  async function downloadPdf(runId?: string) {
    if (busy === "pdf") return;
    const controller = new AbortController();
    pdfAbortRef.current = controller;
    setBusy("pdf");
    setMsg(null);
    try {
      const id = runId ?? data?.id;
      const blob = id
        ? await api.perfRunPdf(id, controller.signal)
        : await api.perfLatestPdf(params, controller.signal);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `performance-profile-${data?.scope_name || "report"}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      if ((e as { name?: string } | null)?.name !== "AbortError") setMsg({ text: formatError(e), ok: false });
    } finally {
      pdfAbortRef.current = null;
      setBusy("");
    }
  }

  function cancelPdf() {
    pdfAbortRef.current?.abort();
  }

  // Capture the currently-viewed run as an immutable Evidence Locker snapshot.
  async function saveEvidence() {
    if (busy === "evidence") return;
    setBusy("evidence");
    setMsg(null);
    try {
      const r = data?.id
        ? await api.perfRunEvidence(data.id)
        : await api.perfLatestEvidence(params);
      setMsg({ text: `Saved to Evidence Locker: ${r.snapshot.name}`, ok: true });
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
      {/* Top-level view tabs: Profiler (single scope) vs Fleet (all workloads). */}
      <div className="flex items-center gap-1 border-b bg-white px-5 pt-2">
        {(["profiler", "fleet"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setMainView(v)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${mainView === v ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}
          >
            {v === "profiler" ? "🔥 Profiler" : "🚀 Fleet"}
          </button>
        ))}
      </div>
      {mainView === "fleet" ? (
        <PerformanceFleet
          onOpenWorkload={(id) => { setScopeKind("workload"); setWorkloadId(id); setMainView("profiler"); }}
        />
      ) : (
      <>
      {/* Header + controls */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-4">
          <ScoreDonut score={data?.scorecard?.workload_score ?? runs[0]?.workload_score ?? null} />
          <div className="min-w-0 max-w-md">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">🔥 Performance Profiler</h1>
            <p className="text-xs text-gray-500">
              Reads live Azure Monitor metrics for every resource in a workload and lays them out in a single matrix — so you can see the whole workload's performance holistically and spot the binding bottleneck against its AMBA thresholds. Pick a window and click Run. Read-only.
            </p>
          </div>
          {enabled && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Score trend</span>
              <TrendChart points={trendQ.data?.points ?? []} current={trendQ.data?.current} previous={trendQ.data?.previous} delta={trendQ.data?.delta} loading={trendQ.isLoading} unit="" deltaLabel="vs last run" />
            </div>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <ConnectionScopePicker value={connId} onChange={(id) => { setConnId(id); if (scopeKind === "subscription") { setSubId(""); setSubName(""); } }} />
            <ScopePicker
              scopeKind={scopeKind}
              onScopeKindChange={setScopeKind}
              workloads={workloads}
              workloadId={effWorkloadId}
              onWorkloadChange={setWorkloadId}
              subId={subId}
              subName={subName}
              connectionId={connId}
              onSubPick={(id, name) => {
                setSubId(id);
                setSubName(name);
              }}
            />
            <TimeRangePicker
              start={startTime}
              end={endTime}
              label={rangeLabel}
              onApply={(s, e, lbl) => { setStartTime(s); setEndTime(e); setRangeLabel(lbl); }}
            />
            <button
              onClick={runProfile}
              disabled={runningHere || !enabled || !startTime || !endTime}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              data-testid="perf-run-button"
            >
              {runningHere ? "Profiling…" : "▶ Run profile"}
            </button>
          </div>
        </div>
        {data?.scorecard && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Stat label="Performance score" value={`${data.scorecard.workload_score}`} tone={scoreTone(data.scorecard.workload_score)} />
            <Stat label="Resources profiled" value={`${data.scorecard.resources_profiled}`} />
            <Stat label="Breaching" value={`${data.scorecard.breaching}`} tone={data.scorecard.breaching ? "text-red-600" : undefined} />
            <Stat label="Approaching" value={`${data.scorecard.approaching}`} tone={data.scorecard.approaching ? "text-amber-600" : undefined} />
            <Stat label="Healthy" value={`${data.scorecard.healthy}`} tone="text-green-600" />
          </div>
        )}
        {runningHere && runningEntry?.lastResource && (
          <div className="mt-1 truncate text-[11px] text-gray-500">profiling… {runningEntry.lastResource}</div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {msg && (
          <div className={`mb-3 rounded-md border px-3 py-2 text-sm ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}

        {/* Run history (shared shell) */}
        <RunHistoryShell<PerfRunSummary>
          title="Profile history"
          countText={`${runs.length} run(s) for this scope`}
          headerExtra={runningHere ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />1 running
            </span>
          ) : undefined}
          rows={runs}
          loading={runsQ.isLoading}
          suppressEmpty={runningHere}
          emptyHint={<>No profiles yet — pick a window and click <b>Run profile</b>.</>}
          rowClassName={(r) => (data?.id === r.id ? "bg-blue-50" : "")}
          testId="perf-history"
          prependRow={runningHere && runningEntry ? (
            <tr className="border-t bg-amber-50/50" data-testid="perf-running-row">
              <td className="px-3 py-2 text-gray-700"><div className="leading-tight"><div className="whitespace-nowrap">{fmtTime(new Date(runningEntry.startedAt).toISOString())}</div><div className="text-[11px] text-gray-400">{fmtAgo(new Date(runningEntry.startedAt).toISOString())}</div></div></td>
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
          ) : undefined}
          showTrash={showTrash}
          onToggleTrash={() => setShowTrash((v) => !v)}
          trashedCount={trashed.length}
          trashNote="Deleted profile runs are kept here until you restore or permanently delete them."
          trashedRows={trashed}
          trashLoading={trashQ.isLoading}
          onEmptyTrash={() => void emptyTrash()}
          emptyingTrash={busy === "empty-trash"}
          columns={[
            { header: "Run time", className: "text-gray-700", render: (r) => <div className="leading-tight"><div className="whitespace-nowrap">{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</div><div className="text-[11px] text-gray-400">{fmtAgo(r.run_at)}</div></div> },
            { header: "Window", className: "text-gray-500", render: (r) => windowCell(r) },
            { header: "Score", render: (r) => <span className={`font-semibold ${r.workload_score != null ? scoreTone(r.workload_score) : ""}`}>{r.workload_score ?? "—"}</span> },
            { header: "Breach / Approach / Healthy", render: (r) => <><span className="text-red-600">{r.breaching}</span> / <span className="text-amber-600">{r.approaching}</span> / <span className="text-green-600">{r.healthy}</span></> },
            { header: "Top bottleneck", className: "text-gray-600", render: (r) => (r.top_bottleneck ? `${r.top_bottleneck.resource_name} · ${r.top_bottleneck.metric_name} (${r.top_bottleneck.pct_of_threshold}%)` : "—") },
            {
              header: "Actions", align: "right", render: (r) => (
                <>
                  <button onClick={() => viewRun(r.id)} disabled={busy === `view:${r.id}`} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">View</button>
                  <button onClick={() => void downloadPdf(r.id)} disabled={busy === "pdf"} title="Download a branded PDF report for this run" className="ml-1 rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">📄 PDF</button>
                  <button onClick={() => deleteRun(r.id)} disabled={busy === `del:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete</button>
                </>
              ),
            },
          ]}
          trashColumns={[
            { header: "Run time", className: "text-gray-700", render: (r) => <>{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</> },
            { header: "Window", className: "text-gray-500", render: (r) => windowCell(r) },
            { header: "Score", render: (r) => <span className={`font-semibold ${r.workload_score != null ? scoreTone(r.workload_score) : ""}`}>{r.workload_score ?? "—"}</span> },
            { header: "Top bottleneck", className: "text-gray-600", render: (r) => (r.top_bottleneck ? `${r.top_bottleneck.resource_name} · ${r.top_bottleneck.metric_name}` : "—") },
            { header: "Deleted", className: "text-gray-400", render: (r) => (r.deleted_at ? fmtTime(r.deleted_at) : "—") },
            {
              header: "Actions", align: "right", render: (r) => (
                <>
                  <button onClick={() => void restoreRun(r.id)} disabled={busy === `restore:${r.id}`} className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50">↩ Restore</button>
                  <button onClick={() => void purgeRun(r.id)} disabled={busy === `purge:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
                </>
              ),
            },
          ]}
        />
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
          busy.startsWith("view:") ? (
            // PU3 — skeleton while loading a historic run (instead of the bare empty card).
            <div className="rounded-lg border bg-white p-4"><Skeleton rows={10} /></div>
          ) : (
          <div className="rounded-lg border border-dashed bg-white p-8 text-center text-sm text-gray-400">
            {enabled ? "Run a profile or click View on a historic run to see the analysis here." : "Pick a scope to begin."}
          </div>
          )
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
            {(data.bottlenecks ?? []).length > 0 && (
              <div className="mb-6">
                <h2 className="mb-2 text-sm font-semibold text-gray-900">Ranked bottlenecks</h2>
                <div className="space-y-1.5">
                  {data.bottlenecks.slice(0, 12).map((b, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2 rounded-lg border bg-white px-3 py-2 text-sm">
                      <span className={`inline-block h-2 w-2 rounded-full ${STATE_TONE[b.state]}`} />
                      <span className="font-medium text-gray-800">{b.resource_name}</span>
                      <PortalLink resourceId={b.resource_id} />
                      <span className="text-gray-500">{b.metric_name}</span>
                      <span className={STATE_TEXT[b.state]}>{b.observed}{b.unit} / {b.threshold}{b.unit} ({b.pct_of_threshold}%)</span>
                      {b.trend_pct ? <span className="text-[11px] text-gray-400">trend {b.trend_pct > 0 ? "+" : ""}{b.trend_pct}%</span> : null}
                      <div className="ml-auto flex gap-1.5">
                        <button onClick={() => investigate(b)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">🔎 War Room</button>
                        {i === 0 && (
                          <div className="relative" ref={ticketRef}>
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

            {/* Sticky bounded panel: toolbar (always visible) + internally-scrolling table.
                Because they live in one flex column, the sticky table header always pins
                directly below the toolbar regardless of browser zoom (no magic offset). */}
            <div className="sticky top-0 z-30 flex max-h-[82vh] min-h-[420px] flex-col">
            <div className="shrink-0 border-b bg-white pb-2 pt-1">
            <div className="mb-2 flex items-center gap-2">
              <h2 className="text-sm font-semibold text-gray-900">Heatmap — resources × AMBA metrics</h2>
              <span className="text-[11px] text-gray-400">cell = % of its AMBA threshold</span>
              <div className="ml-auto flex items-center gap-1.5">
                <button onClick={registerFindings} disabled={busy === "findings" || (data.bottlenecks ?? []).length === 0} className="rounded-md border bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50">🛡️ Register findings</button>
                <button onClick={() => void downloadPdf()} disabled={busy === "pdf"} title="Download a branded PDF performance report for this run" className="rounded-md border bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50">{busy === "pdf" ? "…" : "📄 PDF"}</button>
                <button onClick={() => void saveEvidence()} disabled={busy === "evidence"} title="Capture this profile run as an immutable Evidence Locker snapshot" className="rounded-md border bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50">{busy === "evidence" ? "Saving…" : "🗄 Evidence"}</button>
              </div>
            </div>

            {/* Heatmap filter toolbar */}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {/* Posture segmented chips */}
              <div className="inline-flex overflow-hidden rounded-md border">
                {([
                  ["all", "All"],
                  ["problems", "Problems"],
                  ["atrisk", "At-risk"],
                  ["healthy", "Healthy"],
                ] as const).map(([id, label]) => (
                  <button
                    key={id}
                    onClick={() => setHmPosture(id)}
                    className={`px-2.5 py-1 font-medium transition ${hmPosture === id ? "bg-brand text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {/* Resource-type multiselect (controlled so it closes on outside click / Esc) */}
              <div className="relative" ref={hmTypesRef}>
                <button
                  type="button"
                  onClick={() => setHmTypesOpen((v) => !v)}
                  className="flex cursor-pointer items-center gap-1 rounded-md border bg-white px-2.5 py-1 hover:bg-gray-50"
                >
                  <span className="text-gray-600">Types</span>
                  {hmTypes.length > 0 && <span className="rounded bg-brand/10 px-1.5 text-[10px] font-medium text-brand">{hmTypes.length}</span>}
                  <span className="text-gray-400">▾</span>
                </button>
                {hmTypesOpen && (
                <div className="absolute z-50 mt-1 max-h-72 w-60 overflow-auto rounded-md border bg-white p-1.5 shadow-lg">
                  {hmAllTypes.length === 0 ? (
                    <div className="px-2 py-1 text-gray-400">No types</div>
                  ) : (
                    hmAllTypes.map((t) => (
                      <label key={t.type} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-gray-50">
                        <input
                          type="checkbox"
                          checked={hmTypes.includes(t.type)}
                          onChange={(e) => setHmTypes((prev) => (e.target.checked ? [...prev, t.type] : prev.filter((x) => x !== t.type)))}
                        />
                        <span className="text-gray-700">{t.display}</span>
                      </label>
                    ))
                  )}
                  {hmTypes.length > 0 && (
                    <button onClick={() => setHmTypes([])} className="mt-1 w-full rounded border px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-50">Clear types</button>
                  )}
                </div>
                )}
              </div>

              {/* Region */}
              {hmAllRegions.length > 1 && (
                <select value={hmRegion} onChange={(e) => setHmRegion(e.target.value)} className="rounded-md border bg-white px-2 py-1 text-gray-600">
                  <option value="">All regions</option>
                  {hmAllRegions.map((rg) => (
                    <option key={rg} value={rg}>{rg}</option>
                  ))}
                </select>
              )}

              {/* Score bucket */}
              <select value={hmScore} onChange={(e) => setHmScore(e.target.value as typeof hmScore)} className="rounded-md border bg-white px-2 py-1 text-gray-600">
                <option value="all">Any score</option>
                <option value="crit">Critical &lt;50</option>
                <option value="risk">At-risk 50–79</option>
                <option value="healthy">Healthy 80–100</option>
              </select>

              {/* Sort */}
              <select value={hmSort} onChange={(e) => setHmSort(e.target.value as typeof hmSort)} className="rounded-md border bg-white px-2 py-1 text-gray-600">
                <option value="score">Sort: worst score</option>
                <option value="breaching">Sort: most breaching</option>
                <option value="name">Sort: name</option>
              </select>

              {/* Search */}
              <input
                value={hmSearch}
                onChange={(e) => setHmSearch(e.target.value)}
                placeholder="Search resource…"
                className="w-40 rounded-md border bg-white px-2 py-1 text-gray-700 placeholder:text-gray-400"
              />

              {/* Hide no-data toggle */}
              <label className="inline-flex items-center gap-1.5 text-gray-600">
                <input type="checkbox" checked={hmHideNoData} onChange={(e) => setHmHideNoData(e.target.checked)} />
                Hide no-data
              </label>

              {/* Prune empty columns toggle */}
              <label className="inline-flex items-center gap-1.5 text-gray-600" title="Drop metric columns that have no values in the filtered rows">
                <input type="checkbox" checked={hmPrune} onChange={(e) => setHmPrune(e.target.checked)} />
                Trim empty columns
              </label>

              {/* Result counter + clear */}
              <span className="ml-auto text-gray-400">
                Showing {filteredResources.length} of {data.resources.length} resource(s)
              </span>
              {hmFiltersActive && (
                <button onClick={clearHeatmapFilters} className="rounded border px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-50">Clear filters</button>
              )}
            </div>
            {/* PU4 — active heatmap filter chips. */}
            {hmFiltersActive && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                {hmPosture !== "all" && <FilterChip label={`Posture: ${hmPosture}`} onClear={() => setHmPosture("all")} />}
                {hmScore !== "all" && <FilterChip label={`Score: ${hmScore}`} onClear={() => setHmScore("all")} />}
                {hmRegion !== "" && <FilterChip label={`Region: ${hmRegion}`} onClear={() => setHmRegion("")} />}
                {hmTypes.length > 0 && <FilterChip label={`${hmTypes.length} type(s)`} onClear={() => setHmTypes([])} />}
                {hmHideNoData && <FilterChip label="Hide no-data" onClear={() => setHmHideNoData(false)} />}
                {hmSearch.trim() !== "" && <FilterChip label={`“${hmSearch.trim()}”`} onClear={() => setHmSearch("")} />}
              </div>
            )}
            </div>

            {filteredResources.length === 0 ? (
              <div className="mt-2 flex flex-1 flex-col items-center justify-center rounded-lg border border-dashed bg-white py-12 text-center text-sm text-gray-400">
                <div>No resources match the current filters.</div>
                {hmFiltersActive && (
                  <button onClick={clearHeatmapFilters} className="mt-2 rounded border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear filters</button>
                )}
              </div>
            ) : (
            <div ref={matrixScrollRef} className="mt-2 min-h-0 flex-1 overflow-auto rounded-lg border bg-white">
              <table className="w-full text-[12px]">
                <thead className="bg-gray-50 text-left text-gray-500">
                  <tr>
                    <th rowSpan={2} className="sticky left-0 top-0 z-30 bg-gray-50 px-2 align-bottom py-2">Resource</th>
                    <th rowSpan={2} className="sticky top-0 z-20 bg-gray-50 px-2 align-bottom py-2">Score</th>
                    {metricGroups.map((g) => (
                      <th key={g.type} colSpan={g.metrics.length} className="sticky top-0 z-20 box-border h-[26px] truncate border-l border-gray-200 bg-gray-100 px-1 py-1 text-center text-[10px] font-semibold text-gray-600" title={g.type}>
                        {g.display}
                      </th>
                    ))}
                    {/* spacer soaks up slack width (so Resource/Score don't stretch) */}
                    <th rowSpan={2} className="sticky top-0 z-20 w-full min-w-[16px] bg-gray-50" aria-hidden="true" />
                  </tr>
                  <tr>
                    {metricCols.map((c, i) => {
                      const firstInGroup = i === 0 || metricCols[i - 1].type !== c.type;
                      return (
                        <th key={c.key} className={`sticky top-[26px] z-20 h-[176px] w-[34px] min-w-[34px] bg-gray-50 p-0 align-bottom ${firstInGroup ? "border-l border-gray-200" : ""}`} title={c.metric}>
                          <div className="flex h-full items-end justify-center pb-1.5">
                            <span className="[writing-mode:vertical-rl] rotate-180 whitespace-nowrap text-[10px] font-normal leading-none text-gray-500">{c.name}</span>
                          </div>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {padTop > 0 && <tr style={{ height: padTop }} aria-hidden />}
                  {vRows.map((vr) => {
                    const r = filteredResources[vr.index];
                    const byKey: Record<string, PerfMetricCell> = {};
                    for (const c of r.cells) byKey[`${r.resource_type}|${c.metric}`] = c;
                    return (
                      <tr key={r.resource_id} ref={rowVirt.measureElement} data-index={vr.index} className="group cursor-pointer border-t hover:bg-gray-50" onClick={() => setDrawer(r)}>
                        <td className="sticky left-0 z-10 max-w-[220px] bg-white px-2 py-1.5">
                          <div className="flex items-center gap-1.5">
                            <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${STATE_TONE[r.state]}`} />
                            <span className="truncate font-medium text-gray-800" title={r.resource_name}>{r.resource_name}</span>
                            <PortalLink resourceId={r.resource_id} className="opacity-0 group-hover:opacity-100" />
                          </div>
                          <div className="truncate text-[10px] text-gray-400">{r.display}</div>
                        </td>
                        <td className={`px-2 py-1.5 font-semibold ${scoreTone(r.score)}`}>{r.score}</td>
                        {metricCols.map((mc, i) => {
                          const firstInGroup = i === 0 || metricCols[i - 1].type !== mc.type;
                          const border = firstInGroup ? "border-l border-gray-100" : "";
                          const cell = byKey[mc.key];
                          if (!cell) return <td key={mc.key} className={`px-2 py-1.5 text-center text-gray-200 ${border}`}>·</td>;
                          const floorWord = cell.higher_is_worse ? "threshold" : "floor";
                          return (
                            <td key={mc.key} className={`px-1 py-1 text-center ${border}`}>
                              <span className={`inline-block min-w-[38px] rounded border px-1 py-0.5 text-[11px] ${STATE_CELL[cell.state]}`} title={`${cell.observed ?? "?"}${cell.unit} vs ${cell.threshold ?? "—"}${cell.unit} ${floorWord}`}>
                                {metricPctLabel(cell) ?? (cell.state === "no_data" ? "—" : "ok")}
                              </span>
                            </td>
                          );
                        })}
                        <td aria-hidden="true" className="w-full" />
                      </tr>
                    );
                  })}
                  {padBottom > 0 && <tr style={{ height: padBottom }} aria-hidden />}
                </tbody>
              </table>
            </div>
            )}
            </div>
            </>
            )}
          </>
        )}
      </div>

      <PdfGeneratingOverlay open={busy === "pdf"} onCancel={cancelPdf} />

      {/* Drill drawer */}
      {drawer && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={() => setDrawer(null)}>
          <div className="h-full w-full max-w-lg overflow-auto bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between border-b px-5 py-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-2.5 w-2.5 rounded-full ${STATE_TONE[drawer.state]}`} />
                  <h3 className="text-base font-semibold text-gray-900">{drawer.resource_name}</h3>
                  <PortalLink resourceId={drawer.resource_id} className="text-base text-gray-400" />
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
                      {c.observed ?? "?"}{c.unit} / {c.threshold ?? "—"}{c.unit} {c.higher_is_worse ? "threshold" : "floor"}
                      {metricPctLabel(c) != null ? ` (${metricPctLabel(c)} of ${c.higher_is_worse ? "threshold" : "floor"})` : ""}
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
      </>
      )}
    </div>
  );
}
