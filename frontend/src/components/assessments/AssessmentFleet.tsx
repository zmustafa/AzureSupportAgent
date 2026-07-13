import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type AssessmentFleetRow } from "../../api";
import { queryClient } from "../../queryClient";
import { isRefreshing, peekRefreshError, startBackgroundRefresh, subscribeBackgroundRefresh, useBackgroundRefresh } from "../../utils/backgroundRefresh";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { enqueueFleet, fleetOutstanding, fleetQueuedKeys, useFleetQueue } from "../fleetScheduler";

const MAX_PARALLEL = 3;
const STAGGER_MS = 400;
const QUEUE_ID = "assessmentFleet";
const PILLARS = ["security", "reliability", "cost", "operations", "performance"] as const;
const PACKS = [
  { id: "waf", label: "WAF", pillars: [...PILLARS] },
  { id: "wara", label: "WARA", pillars: ["reliability"] },
  { id: "wasa", label: "WASA", pillars: ["security"] },
] as const;
const TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

type SortKey = "worst" | "name" | "score" | "failed" | "resources" | "run_at";
type SortDir = "asc" | "desc";

function refreshKey(row: AssessmentFleetRow): string {
  return `assessment:${row.workload_id}:${row.connection_id || ""}`;
}

function relTime(iso: string): string {
  if (!iso) return "never";
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return "—";
  const seconds = Math.max(0, (Date.now() - time) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function ScorePill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-gray-400">n/a</span>;
  const tone = value >= 80 ? "bg-green-100 text-green-700" : value >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`inline-block min-w-[2.5rem] rounded px-1.5 py-0.5 text-center font-semibold tabular-nums ${tone}`}>{value}</span>;
}

function Status({ row, running, queued, launchError }: { row: AssessmentFleetRow; running: boolean; queued: boolean; launchError?: string }) {
  if (running) return <span className="text-brand">↻ launching…</span>;
  if (queued) return <span className="text-gray-400">queued</span>;
  if (launchError) return <span className="rounded bg-red-50 px-1.5 py-0.5 font-medium text-red-700" title={launchError}>⚠ launch failed</span>;
  const status = row.current_status;
  const tone = status === "failed" ? "bg-red-50 text-red-700" : status === "running" ? "bg-blue-50 text-blue-700" : status === "queued" ? "bg-gray-100 text-gray-600" : status === "cancelled" ? "bg-amber-50 text-amber-700" : status === "succeeded" ? "bg-green-50 text-green-700" : "text-gray-400";
  return <span className={`rounded px-1.5 py-0.5 ${tone}`} title={row.error}>{status}</span>;
}

export function AssessmentFleet({ onOpenReport, onOpenWorkload }: { onOpenReport: (runId: string) => void; onOpenWorkload: (workloadId: string) => void }) {
  useBackgroundRefresh();
  useFleetQueue();
  const fleetQ = useQuery({
    queryKey: ["assessmentFleet"],
    queryFn: api.assessmentFleet,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => (query.state.data?.workloads ?? []).some((row) => row.current_status === "queued" || row.current_status === "running") ? 2500 : false,
  });
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [pack, setPack] = useState("waf");
  const [useAi, setUseAi] = useState(true);
  const [message, setMessage] = useState("");
  const queuedKeys = fleetQueuedKeys(QUEUE_ID);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const list = query ? rows.filter((row) => `${row.name} ${row.environment} ${row.criticality}`.toLowerCase().includes(query)) : rows;
    if (sortKey === "worst") return [...list];
    const direction = sortDir === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      if (sortKey === "name") return direction * a.name.localeCompare(b.name);
      if (sortKey === "run_at") return direction * (a.current_run_at || "").localeCompare(b.current_run_at || "");
      const value = (row: AssessmentFleetRow) => sortKey === "score" ? (row.overall_score ?? 101) : sortKey === "failed" ? (row.failed ?? -1) : (row.resources ?? -1);
      return direction * (value(a) - value(b)) || a.name.localeCompare(b.name);
    });
  }, [rows, search, sortKey, sortDir]);

  const allSelected = filtered.length > 0 && filtered.every((row) => selected.has(row.workload_id));
  const toggleAll = () => setSelected((current) => {
    const next = new Set(current);
    filtered.forEach((row) => allSelected ? next.delete(row.workload_id) : next.add(row.workload_id));
    return next;
  });
  const toggleOne = (id: string) => setSelected((current) => {
    const next = new Set(current);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });

  function enqueueRows(chosen: AssessmentFleetRow[]) {
    const preset = PACKS.find((item) => item.id === pack) ?? PACKS[0];
    enqueueFleet(QUEUE_ID, chosen.map((row) => ({
      key: refreshKey(row),
      run: () => startBackgroundRefresh(refreshKey(row), async () => {
        const created = await api.enqueueAssessments({ workload_ids: [row.workload_id], pillars: [...preset.pillars], pack: preset.id, connection_id: row.connection_id || null, use_ai: useAi });
        const runId = created.runs[0]?.id;
        if (!runId) throw new Error("The assessment was not queued.");
        const deadline = Date.now() + 35 * 60_000;
        while (Date.now() < deadline) {
          const result = await api.assessmentRuns(row.workload_id);
          const run = result.runs.find((item) => item.id === runId);
          queryClient.setQueryData(["assessmentRuns"], (current: { runs?: typeof result.runs } | undefined) => {
            if (!current?.runs) return current;
            return { ...current, runs: current.runs.map((item) => item.id === runId && run ? run : item) };
          });
          await queryClient.invalidateQueries({ queryKey: ["assessmentFleet"] });
          if (run && TERMINAL_STATUSES.has(run.status)) {
            if (run.status === "failed") throw new Error(run.summary || `Assessment failed for ${row.name}.`);
            if (run.status === "cancelled") throw new Error(`Assessment was cancelled for ${row.name}.`);
            break;
          }
          await delay(2500);
        }
        if (Date.now() >= deadline) throw new Error(`Assessment timed out for ${row.name}.`);
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["assessmentFleet"] }),
          queryClient.invalidateQueries({ queryKey: ["assessmentRuns"] }),
          queryClient.invalidateQueries({ queryKey: ["assessmentPortfolio"] }),
          queryClient.invalidateQueries({ queryKey: ["assessmentTrend", row.workload_id] }),
        ]);
      }),
    })), { maxParallel: MAX_PARALLEL, staggerMs: STAGGER_MS, isRunning: isRefreshing, subscribe: subscribeBackgroundRefresh });
  }

  function launch() {
    const chosen = rows.filter((row) => selected.has(row.workload_id));
    if (!chosen.length) return;
    enqueueRows(chosen);
    setMessage(`Launching ${chosen.length} ${pack.toUpperCase()} assessment${chosen.length === 1 ? "" : "s"}, up to ${MAX_PARALLEL} at a time.`);
    setSelected(new Set());
  }

  const failedRows = rows.filter((row) => {
    const key = refreshKey(row);
    return !!peekRefreshError(key) && !isRefreshing(key) && !queuedKeys.has(key);
  });
  const scanned = fleetQ.data?.scanned ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const outstanding = fleetOutstanding(QUEUE_ID);

  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="border-b bg-white px-5 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0"><h2 className="text-sm font-semibold text-gray-900">Assessment fleet</h2><p className="text-[11px] text-gray-500">Latest persisted posture per workload. Opening Fleet never starts an Azure scan.</p></div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-gray-500">{scanned}/{total} assessed{outstanding ? ` · ${outstanding} launching` : ""}</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Filter workloads…" className="w-44 rounded-md border px-2 py-1 text-xs" />
          <select value={sortKey} onChange={(event) => { const key = event.target.value as SortKey; setSortKey(key); setSortDir(key === "name" || key === "score" ? "asc" : "desc"); }} className="rounded-md border px-2 py-1 text-xs text-gray-600"><option value="worst">Sort: worst first</option><option value="score">Sort: lowest score</option><option value="failed">Sort: failed controls</option><option value="resources">Sort: resources</option><option value="run_at">Sort: newest run</option><option value="name">Sort: name</option></select>
          <select value={pack} onChange={(event) => setPack(event.target.value)} className="rounded-md border px-2 py-1 text-xs text-gray-600">{PACKS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
          <label className="flex items-center gap-1 text-xs text-gray-600"><input type="checkbox" checked={useAi} onChange={(event) => setUseAi(event.target.checked)} /> AI summary</label>
          {failedRows.length > 0 && <button onClick={() => enqueueRows(failedRows)} className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700">↻ Retry failed ({failedRows.length})</button>}
          <button onClick={launch} disabled={!selected.size} className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">▶ Run {selected.size || ""} selected</button>
        </div>
      </div>
      {message && <div className="mt-2 rounded-md border border-green-200 bg-green-50 px-3 py-1.5 text-xs text-green-700">{message}</div>}
    </div>
    <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
      {fleetQ.isLoading ? <Skeleton rows={8} /> : fleetQ.isError ? <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(fleetQ.error)}</div> : !rows.length ? <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads exist yet.</div> :
        <table className="w-full min-w-[1050px] text-[12px]"><thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500"><tr className="border-b">
          <th className="w-8 px-2 py-2"><input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all shown workloads" /></th><th className="px-2 py-2 font-medium">Workload</th><th className="px-2 py-2 font-medium">Score</th>{PILLARS.map((pillar) => <th key={pillar} className="px-2 py-2 font-medium capitalize">{pillar === "operations" ? "Operations" : pillar}</th>)}<th className="px-2 py-2 font-medium">Failed</th><th className="px-2 py-2 font-medium">Critical / high</th><th className="px-2 py-2 font-medium">Resources</th><th className="px-2 py-2 font-medium">Completeness</th><th className="px-2 py-2 font-medium">Status</th><th className="px-2 py-2 font-medium">Last run</th><th className="px-2 py-2" />
        </tr></thead><tbody>{filtered.map((row) => {
          const key = refreshKey(row); const running = isRefreshing(key); const queued = queuedKeys.has(key); const launchError = !running && !queued ? peekRefreshError(key) : undefined;
          return <tr key={row.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(row.workload_id) ? "bg-brand/5" : ""}`}>
            <td className="px-2 py-1.5"><input type="checkbox" checked={selected.has(row.workload_id)} onChange={() => toggleOne(row.workload_id)} aria-label={`Select ${row.name}`} /></td>
            <td className="px-2 py-1.5"><button onClick={() => onOpenWorkload(row.workload_id)} className="text-left font-medium text-gray-800 hover:text-brand hover:underline">{row.name}</button><div className="flex gap-1 text-[10px] text-gray-400">{row.environment && <span>{row.environment}</span>}{row.criticality && <span>· {row.criticality}</span>}{row.stale && row.has_scan && <span className="rounded bg-amber-50 px-1 text-amber-600">stale</span>}</div></td>
            <td className="px-2 py-1.5"><ScorePill value={row.overall_score} /></td>{PILLARS.map((pillar) => <td key={pillar} className="px-2 py-1.5"><ScorePill value={row.pillar_scores[pillar] ?? null} /></td>)}
            <td className={`px-2 py-1.5 tabular-nums ${(row.failed ?? 0) > 0 ? "font-medium text-red-600" : "text-gray-500"}`}>{row.failed ?? "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan ? `${row.findings_by_severity.critical ?? 0} / ${row.findings_by_severity.error ?? 0}` : "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.resources ?? "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.completeness_pct == null ? "—" : `${row.completeness_pct}%`}</td><td className="px-2 py-1.5"><Status row={row} running={running} queued={queued} launchError={launchError} /></td><td className="px-2 py-1.5 text-gray-500" title={row.current_run_at}>{relTime(row.current_run_at)}</td><td className="px-2 py-1.5">{row.run_id ? <button onClick={() => onOpenReport(row.run_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Report ▸</button> : <button onClick={() => onOpenWorkload(row.workload_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Assess ▸</button>}</td>
          </tr>;
        })}</tbody></table>}
    </div>
  </div>;
}
