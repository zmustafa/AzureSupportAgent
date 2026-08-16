// Fleet view for the Performance Profiler: a dense, sortable table summarizing the LATEST
// profile run for every workload, plus a mass-launch bar that profiles the selected
// workloads over ONE shared time window. The queue is owned by the backend and persisted in
// SQL, so progress survives navigation, browser reloads, and server restarts.
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { TimeRangePicker } from "../changeexplorer/TimeRangePicker";

function pad(n: number): string { return String(n).padStart(2, "0"); }
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function defaultStart(): string { return toLocalInput(new Date(Date.now() - 24 * 3600_000)); }
function defaultEnd(): string { return toLocalInput(new Date()); }

function scoreTone(score: number | null): string {
  if (score == null) return "text-gray-400";
  if (score >= 80) return "text-green-600";
  if (score >= 50) return "text-amber-600";
  return "text-red-600";
}

function ScorePill({ score }: { score: number | null }) {
  if (score == null) return <span className="text-xs text-gray-400">—</span>;
  const bg = score >= 80 ? "bg-green-100 text-green-700" : score >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`inline-block min-w-[2.25rem] rounded px-1.5 py-0.5 text-center text-xs font-semibold tabular-nums ${bg}`}>{score}</span>;
}

function relTime(iso: string): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

type SortKey = "worst" | "score" | "resources" | "breaching" | "approaching" | "healthy" | "bottleneck" | "name" | "run_at";
type SortDir = "asc" | "desc";

export function PerformanceFleet({ onOpenWorkload }: { onOpenWorkload: (workloadId: string, connectionId: string) => void }) {
  const queryClient = useQueryClient();
  const fleetQ = useQuery({ queryKey: ["perfFleet"], queryFn: api.perfFleet, refetchOnWindowFocus: false });
  const batchQ = useQuery({
    queryKey: ["perfFleetBatchLatest"],
    queryFn: () => api.perfFleetLatestBatch(false),
    refetchInterval: (query) => {
      const status = query.state.data?.batch?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
    refetchOnWindowFocus: true,
  });
  const batch = batchQ.data?.batch ?? null;
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [startTime, setStartTime] = useState(() => defaultStart());
  const [endTime, setEndTime] = useState(() => defaultEnd());
  const [rangeLabel, setRangeLabel] = useState("Last 1 day");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState<"" | "launch" | "retry" | "cancel">("");

  const batchItems = useMemo(
    () => new Map((batch?.items ?? []).map((item) => [item.workload_id, item])),
    [batch],
  );
  const batchActive = batch?.status === "queued" || batch?.status === "running";

  useEffect(() => {
    if (batch && !batchActive) {
      void queryClient.invalidateQueries({ queryKey: ["perfFleet"] });
      void queryClient.invalidateQueries({ queryKey: ["perf-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["perf-trend"] });
    }
  }, [batch?.id, batch?.status, batchActive, queryClient]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = rows;
    if (q) list = list.filter((r) => r.name.toLowerCase().includes(q) || (r.top_bottleneck?.resource_name ?? "").toLowerCase().includes(q));
    const sorted = [...list];
    const dir = sortDir === "asc" ? 1 : -1;
    // Numeric value for a row under the active column ("never run" sinks via -1).
    const numVal = (r: (typeof rows)[number]): number => {
      switch (sortKey) {
        case "score": return r.has_runs ? (r.workload_score ?? -1) : -1;
        case "resources": return r.has_runs ? (r.resources_profiled ?? -1) : -1;
        case "breaching": return r.has_runs ? (r.breaching ?? -1) : -1;
        case "approaching": return r.has_runs ? (r.approaching ?? -1) : -1;
        case "healthy": return r.has_runs ? (r.healthy ?? -1) : -1;
        case "bottleneck": return typeof r.top_bottleneck?.pct_of_threshold === "number" ? r.top_bottleneck.pct_of_threshold : -1;
        default: return 0;
      }
    };
    sorted.sort((a, b) => {
      switch (sortKey) {
        case "worst":
          // Default composite triage (direction-independent): run rows first, then most
          // breaching, then lowest score.
          return (Number(b.has_runs) - Number(a.has_runs)) || ((b.breaching ?? 0) - (a.breaching ?? 0)) || ((a.workload_score ?? 999) - (b.workload_score ?? 999));
        case "name": return dir * a.name.localeCompare(b.name);
        case "run_at": return dir * (a.run_at || "").localeCompare(b.run_at || "");
        default: return dir * (numVal(a) - numVal(b));
      }
    });
    return sorted;
  }, [rows, search, sortKey, sortDir]);

  const allSelected = filtered.length > 0 && filtered.every((r) => selected.has(r.workload_id));
  const toggleAll = () =>
    setSelected((s) => {
      const n = new Set(s);
      if (allSelected) filtered.forEach((r) => n.delete(r.workload_id));
      else filtered.forEach((r) => n.add(r.workload_id));
      return n;
    });
  const toggleOne = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  // Click a column header to sort by it; click again to flip direction. The composite
  // "worst" default lives only in the dropdown.
  const clickSort = (key: SortKey, defDir: SortDir = "desc") => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(defDir); }
  };
  const SortTh = ({ label, sk, defDir = "desc", cls = "" }: { label: string; sk: SortKey; defDir?: SortDir; cls?: string }) => (
    <th
      onClick={() => clickSort(sk, defDir)}
      className={`cursor-pointer select-none px-2 py-2 font-medium hover:text-gray-700 ${sortKey === sk ? "text-gray-700" : ""} ${cls}`}
      title={`Sort by ${label}`}
    >
      {label}
      <span className="ml-0.5 text-[9px] text-gray-400">{sortKey === sk ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
    </th>
  );

  function idempotencyKey(prefix: string): string {
    return `${prefix}:${crypto.randomUUID()}`;
  }

  async function launchRows(chosen: typeof rows, kind: "launch" | "retry") {
    if (chosen.length === 0 || !startTime || !endTime) return;
    setBusy(kind);
    setMsg(null);
    try {
      const result = await api.createPerfFleetBatch({
        workload_ids: chosen.map((row) => row.workload_id),
        start_time: new Date(startTime).toISOString(),
        end_time: new Date(endTime).toISOString(),
        window: rangeLabel,
        idempotency_key: idempotencyKey(`perf-${kind}`),
      });
      queryClient.setQueryData(["perfFleetBatchLatest"], result);
      setMsg({
        text: `${kind === "retry" ? "Retrying" : "Queued"} ${chosen.length} workload${chosen.length === 1 ? "" : "s"} (${rangeLabel}). The server will continue if this browser closes.`,
        ok: true,
      });
      setSelected(new Set());
    } catch (error) {
      setMsg({ text: formatError(error), ok: false });
    } finally {
      setBusy("");
    }
  }

  function launch() {
    const chosen = rows.filter((r) => selected.has(r.workload_id));
    void launchRows(chosen, "launch");
  }

  const failedRows = rows.filter((row) => {
    const item = batchActive ? batchItems.get(row.workload_id) : undefined;
    if (item?.status === "queued" || item?.status === "running") return false;
    const status = item?.status || row.last_attempt_status;
    return status === "failed" || status === "partial" || status === "cancelled";
  });
  async function retryFailed() {
    if (failedRows.length === 0 || !startTime || !endTime) return;
    const retryableItems = batch?.items.filter((item) => ["failed", "partial", "cancelled"].includes(item.status)) ?? [];
    if (batch && retryableItems.length === failedRows.length) {
      setBusy("retry");
      setMsg(null);
      try {
        const result = await api.retryPerfFleetBatch(batch.id, idempotencyKey("perf-retry"));
        queryClient.setQueryData(["perfFleetBatchLatest"], result);
        setMsg({ text: `Queued ${retryableItems.length} failed/partial workload${retryableItems.length === 1 ? "" : "s"} for a durable retry.`, ok: true });
      } catch (error) {
        setMsg({ text: formatError(error), ok: false });
      } finally {
        setBusy("");
      }
      return;
    }
    await launchRows(failedRows, "retry");
  }

  async function cancelBatch() {
    if (!batch || !batchActive) return;
    setBusy("cancel");
    try {
      const result = await api.cancelPerfFleetBatch(batch.id);
      queryClient.setQueryData(["perfFleetBatchLatest"], result);
      setMsg({ text: "Cancelled all profiler workloads that had not started. The active workload will finish safely.", ok: true });
    } catch (error) {
      setMsg({ text: formatError(error), ok: false });
    } finally {
      setBusy("");
    }
  }

  const profiled = fleetQ.data?.profiled ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const activeRuns = batchActive && batch ? batch.total - batch.completed : 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Mass-launch toolbar */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">Fleet performance</h2>
            <p className="text-[11px] text-gray-500">
              Latest profile per workload. Select workloads, pick one window, and launch the profiler across all of them.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-gray-500">{profiled}/{total} profiled{activeRuns > 0 ? ` · ${activeRuns} outstanding` : ""}</span>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter workloads…"
              className="w-44 rounded-md border px-2 py-1 text-xs"
            />
            <select
              value={sortKey}
              onChange={(e) => {
                const k = e.target.value as SortKey;
                setSortKey(k);
                // Match each preset's intent: lowest score / name ascending, the rest descending.
                setSortDir(k === "score" || k === "name" ? "asc" : "desc");
              }}
              className="rounded-md border px-2 py-1 text-xs text-gray-600"
              title="Sort"
            >
              <option value="worst">Sort: worst first</option>
              <option value="score">Sort: lowest score</option>
              <option value="breaching">Sort: most breaching</option>
              <option value="run_at">Sort: newest run</option>
              <option value="name">Sort: name</option>
            </select>
            <TimeRangePicker start={startTime} end={endTime} label={rangeLabel} onApply={(s, e, lbl) => { setStartTime(s); setEndTime(e); setRangeLabel(lbl); }} />
            {failedRows.length > 0 && (
              <button onClick={() => void retryFailed()} disabled={batchActive || busy !== ""} className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 disabled:opacity-50" title="Re-run workloads whose last profile failed or was partial">
                {busy === "retry" ? "Retrying…" : `↻ Retry failed/partial (${failedRows.length})`}
              </button>
            )}
            <button
              onClick={launch}
              disabled={selected.size === 0 || !startTime || !endTime || batchActive || busy !== ""}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >
              {busy === "launch" ? "Queuing…" : `▶ Run profiler on ${selected.size || ""} selected`}
            </button>
          </div>
        </div>
        {batch && (
          <div data-testid="perf-fleet-batch" className={`mt-2 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-xs ${batch.status === "failed" ? "border-red-200 bg-red-50 text-red-700" : batch.status === "partial" ? "border-amber-200 bg-amber-50 text-amber-800" : "border-blue-200 bg-blue-50 text-blue-800"}`}>
            <span className="font-medium">Batch {batch.status}</span>
            <span>{batch.completed}/{batch.total} complete</span>
            <span>{batch.succeeded} succeeded · {batch.partial} partial · {batch.failed} failed{batch.cancelled ? ` · ${batch.cancelled} cancelled` : ""}</span>
            {batchActive && (
              <button onClick={() => void cancelBatch()} disabled={busy !== ""} className="ml-auto rounded border border-current/30 px-2 py-0.5 font-medium disabled:opacity-50">
                {busy === "cancel" ? "Cancelling…" : "Cancel pending"}
              </button>
            )}
          </div>
        )}
        {msg && (
          <div className={`mt-2 rounded-md border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}
      </div>

      {/* Summary table */}
      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {fleetQ.isLoading ? (
          <Skeleton rows={8} />
        ) : fleetQ.isError ? (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(fleetQ.error)}</div>
        ) : rows.length === 0 ? (
          <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads yet. Create a workload, then profile it here.</div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500">
              <tr className="border-b">
                <th className="w-8 px-2 py-2">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll} title="Select all shown" />
                </th>
                <SortTh label="Workload" sk="name" defDir="asc" />
                <SortTh label="Score" sk="score" defDir="asc" />
                <SortTh label="Resources" sk="resources" />
                <SortTh label="Breaching" sk="breaching" />
                <SortTh label="Approaching" sk="approaching" />
                <SortTh label="Healthy" sk="healthy" />
                <SortTh label="Top bottleneck" sk="bottleneck" />
                <SortTh label="Last run" sk="run_at" />
                <th className="px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => {
                const item = batchActive ? batchItems.get(r.workload_id) : undefined;
                const running = item?.status === "running";
                const queued = item?.status === "queued";
                const attemptStatus = item?.status || r.last_attempt_status;
                const partial = attemptStatus === "partial";
                const failed = attemptStatus === "failed" || attemptStatus === "cancelled";
                const err = item?.error || r.last_attempt_error || r.last_attempt_warning || "";
                return (
                  <tr key={r.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(r.workload_id) ? "bg-brand/5" : ""}`}>
                    <td className="px-2 py-1.5">
                      <input type="checkbox" checked={selected.has(r.workload_id)} onChange={() => toggleOne(r.workload_id)} />
                    </td>
                    <td className="px-2 py-1.5">
                      <button onClick={() => onOpenWorkload(r.workload_id, r.connection_id)} className="text-left font-medium text-gray-800 hover:text-brand hover:underline">
                        {r.name}
                      </button>
                      <div className="flex items-center gap-1">
                        {r.environment && <span className="text-[10px] text-gray-400">{r.environment}</span>}
                        {r.stale && r.has_runs && <span className="rounded bg-amber-50 px-1 text-[10px] text-amber-600" title="Older than the cache window">stale</span>}
                      </div>
                    </td>
                    <td className="px-2 py-1.5">
                      {running ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-brand"><span className="animate-spin">↻</span>profiling…</span>
                      ) : queued ? (
                        <span className="text-[11px] text-gray-400">queued</span>
                      ) : partial ? (
                        <span className="inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700" title={err}>⚠ partial</span>
                      ) : failed ? (
                        <span className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-700" title={err}>⚠ failed</span>
                      ) : r.has_runs ? (
                        <ScorePill score={r.workload_score} />
                      ) : (
                        <span className="text-[11px] text-gray-400">never</span>
                      )}
                    </td>
                    <td className={`px-2 py-1.5 tabular-nums ${scoreTone(r.workload_score)}`}>{r.has_runs ? r.resources_profiled : "—"}</td>
                    <td className={`px-2 py-1.5 tabular-nums ${r.breaching ? "font-semibold text-red-600" : "text-gray-400"}`}>{r.has_runs ? r.breaching : "—"}</td>
                    <td className={`px-2 py-1.5 tabular-nums ${r.approaching ? "text-amber-600" : "text-gray-400"}`}>{r.has_runs ? r.approaching : "—"}</td>
                    <td className="px-2 py-1.5 tabular-nums text-green-600">{r.has_runs ? r.healthy : "—"}</td>
                    <td className="max-w-[16rem] truncate px-2 py-1.5 text-gray-600" title={r.top_bottleneck ? `${r.top_bottleneck.resource_name} · ${r.top_bottleneck.metric_name}` : ""}>
                      {r.top_bottleneck ? (
                        <span><span className="font-medium">{r.top_bottleneck.resource_name}</span> <span className="text-gray-400">{r.top_bottleneck.metric_name}</span>{typeof r.top_bottleneck.pct_of_threshold === "number" ? <span className="text-red-600"> {r.top_bottleneck.pct_of_threshold}%</span> : null}</span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-gray-500" title={err || r.run_at || ""}>
                      {running ? (
                        <span className="truncate text-[11px] text-brand">{item?.resources_completed ? `${item.resources_completed}${item.resources_total ? `/${item.resources_total}` : ""} resources` : "starting…"}</span>
                      ) : queued ? (
                        <span className="text-[11px] text-gray-400">waiting on server</span>
                      ) : partial ? (
                        <span className="text-[11px] text-amber-700">partial — retry</span>
                      ) : failed ? (
                        <span className="text-[11px] text-red-600">failed — retry</span>
                      ) : (
                        relTime(r.run_at)
                      )}
                      {r.has_runs && r.window && !running && !queued && !partial && !failed && <div className="text-[10px] text-gray-400">{r.window}</div>}
                    </td>
                    <td className="px-2 py-1.5">
                      <button onClick={() => onOpenWorkload(r.workload_id, r.connection_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open ▸</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
