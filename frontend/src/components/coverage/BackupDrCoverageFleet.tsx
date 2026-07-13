import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type BackupDrCoverageFleetRow } from "../../api";
import { queryClient } from "../../queryClient";
import { formatError } from "../../utils/format";
import { isRefreshing, peekRefreshError, startBackgroundRefresh, subscribeBackgroundRefresh, useBackgroundRefresh } from "../../utils/backgroundRefresh";
import { Skeleton } from "../../utils/perf";
import { enqueueFleet, fleetOutstanding, fleetQueuedKeys, useFleetQueue } from "../fleetScheduler";

const MAX_PARALLEL = 3;
const STAGGER_MS = 400;
const QUEUE_ID = "backupdrCoverageFleet";

type SortKey = "worst" | "protected_pct" | "total" | "protected" | "offsite" | "recent" | "dr_pairs" | "unhealthy" | "run_at" | "name";
type SortDir = "asc" | "desc";

function refreshKey(row: BackupDrCoverageFleetRow): string {
  return `backupdr:workload:${row.workload_id}:${row.connection_id || ""}`;
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

function PercentPill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-gray-400">n/a</span>;
  const tone = value >= 80 ? "bg-green-100 text-green-700" : value >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`inline-block min-w-[3rem] rounded px-1.5 py-0.5 text-center font-semibold tabular-nums ${tone}`}>{value}%</span>;
}

export function BackupDrCoverageFleet({ onOpenWorkload }: { onOpenWorkload: (workloadId: string, connectionId: string) => void }) {
  useBackgroundRefresh();
  useFleetQueue();
  const fleetQ = useQuery({ queryKey: ["backupdrFleet"], queryFn: api.backupDrCoverageFleet, refetchOnWindowFocus: false });
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [message, setMessage] = useState<string | null>(null);
  const queuedKeys = fleetQueuedKeys(QUEUE_ID);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const list = query ? rows.filter((row) => `${row.name} ${row.environment} ${row.criticality}`.toLowerCase().includes(query)) : rows;
    const sorted = [...list];
    const direction = sortDir === "asc" ? 1 : -1;
    sorted.sort((a, b) => {
      if (sortKey === "worst") return Number(b.has_scan) - Number(a.has_scan)
        || (a.pct_protected ?? 999) - (b.pct_protected ?? 999)
        || (b.total - b.protected) - (a.total - a.protected)
        || a.name.localeCompare(b.name);
      if (sortKey === "name") return direction * a.name.localeCompare(b.name);
      if (sortKey === "run_at") return direction * (a.run_at || "").localeCompare(b.run_at || "");
      const value = (row: BackupDrCoverageFleetRow) => {
        if (!row.has_scan) return -1;
        if (sortKey === "protected_pct") return row.pct_protected ?? -1;
        if (sortKey === "total") return row.total;
        if (sortKey === "protected") return row.protected;
        if (sortKey === "offsite") return row.pct_offsite ?? -1;
        if (sortKey === "recent") return row.pct_recent_job ?? -1;
        if (sortKey === "dr_pairs") return row.dr_pairs;
        return row.dr_pairs_unhealthy;
      };
      return direction * (value(a) - value(b));
    });
    return sorted;
  }, [rows, search, sortKey, sortDir]);

  const allSelected = filtered.length > 0 && filtered.every((row) => selected.has(row.workload_id));
  function toggleAll() {
    setSelected((current) => {
      const next = new Set(current);
      filtered.forEach((row) => allSelected ? next.delete(row.workload_id) : next.add(row.workload_id));
      return next;
    });
  }
  function toggleOne(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function enqueueRows(chosen: BackupDrCoverageFleetRow[]) {
    enqueueFleet(QUEUE_ID, chosen.map((row) => ({
      key: refreshKey(row),
      run: () => startBackgroundRefresh(refreshKey(row), async () => {
        const connectionId = row.connection_id || "";
        const snapshot = await api.refreshBackupDr({ workload_id: row.workload_id, connection_id: connectionId || undefined });
        queryClient.setQueryData(["backupdr", "workload", row.workload_id, "", connectionId], snapshot);
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["backupdrFleet"] }),
          queryClient.invalidateQueries({ queryKey: ["backupdr", "workload", row.workload_id] }),
          queryClient.invalidateQueries({ queryKey: ["backupdr-trend", "workload", row.workload_id] }),
          queryClient.invalidateQueries({ queryKey: ["coverage-runs", "backupdr", "workload", row.workload_id] }),
        ]);
      }),
    })), { maxParallel: MAX_PARALLEL, staggerMs: STAGGER_MS, isRunning: isRefreshing, subscribe: subscribeBackgroundRefresh });
  }

  function launch() {
    const chosen = rows.filter((row) => selected.has(row.workload_id));
    if (!chosen.length) return;
    enqueueRows(chosen);
    setMessage(`Launched Backup & DR coverage scans for ${chosen.length} workload${chosen.length === 1 ? "" : "s"}. Running ${MAX_PARALLEL} at a time…`);
    setSelected(new Set());
  }
  const failedRows = rows.filter((row) => {
    const key = refreshKey(row);
    return !!peekRefreshError(key) && !isRefreshing(key) && !queuedKeys.has(key);
  });
  function retryFailed() {
    enqueueRows(failedRows);
    setMessage(`Retrying ${failedRows.length} failed Backup & DR scan${failedRows.length === 1 ? "" : "s"}…`);
  }
  function clickSort(key: SortKey, defaultDirection: SortDir = "desc") {
    if (sortKey === key) setSortDir((current) => current === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(defaultDirection); }
  }
  const SortHeader = ({ label, value, defaultDirection = "desc" }: { label: string; value: SortKey; defaultDirection?: SortDir }) => <th onClick={() => clickSort(value, defaultDirection)} className={`cursor-pointer select-none px-2 py-2 font-medium hover:text-gray-700 ${sortKey === value ? "text-gray-700" : ""}`}>{label}<span className="ml-0.5 text-[9px] text-gray-400">{sortKey === value ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span></th>;

  const scanned = fleetQ.data?.scanned ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const outstanding = fleetOutstanding(QUEUE_ID);

  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="border-b bg-white px-5 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0"><h2 className="text-sm font-semibold text-gray-900">Fleet Backup &amp; DR coverage</h2><p className="text-[11px] text-gray-500">Latest protection scan per workload. Select workloads and scan them as one background fleet.</p></div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-gray-500">{scanned}/{total} scanned{outstanding ? ` · ${outstanding} outstanding` : ""}</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Filter workloads…" className="w-44 rounded-md border px-2 py-1 text-xs" />
          <select value={sortKey} onChange={(event) => { const key = event.target.value as SortKey; setSortKey(key); setSortDir(key === "protected_pct" || key === "name" ? "asc" : "desc"); }} className="rounded-md border px-2 py-1 text-xs text-gray-600"><option value="worst">Sort: worst first</option><option value="protected_pct">Sort: lowest protected</option><option value="offsite">Sort: offsite coverage</option><option value="recent">Sort: recent jobs</option><option value="dr_pairs">Sort: DR pairs</option><option value="unhealthy">Sort: unhealthy pairs</option><option value="run_at">Sort: newest scan</option><option value="name">Sort: name</option></select>
          {failedRows.length > 0 && <button onClick={retryFailed} className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100">↻ Retry failed ({failedRows.length})</button>}
          <button onClick={launch} disabled={!selected.size} className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">▶ Scan {selected.size || ""} selected</button>
        </div>
      </div>
      {message && <div className="mt-2 rounded-md border border-green-200 bg-green-50 px-3 py-1.5 text-xs text-green-700">{message}</div>}
    </div>
    <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
      {fleetQ.isLoading ? <Skeleton rows={8} /> : fleetQ.isError ? <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(fleetQ.error)}</div> : !rows.length ? <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads exist yet.</div> :
        <table className="w-full text-[12px]"><thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500"><tr className="border-b"><th className="w-8 px-2 py-2"><input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all shown workloads" /></th><SortHeader label="Workload" value="name" defaultDirection="asc" /><SortHeader label="Protected" value="protected_pct" defaultDirection="asc" /><SortHeader label="Total" value="total" /><SortHeader label="Protected #" value="protected" /><SortHeader label="Offsite" value="offsite" /><SortHeader label="Recent job" value="recent" /><SortHeader label="DR pairs" value="dr_pairs" /><SortHeader label="Unhealthy" value="unhealthy" /><SortHeader label="Last scan" value="run_at" /><th className="px-2 py-2" /></tr></thead>
        <tbody>{filtered.map((row) => {
          const key = refreshKey(row); const running = isRefreshing(key); const queued = queuedKeys.has(key); const error = !running && !queued ? peekRefreshError(key) : undefined;
          return <tr key={row.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(row.workload_id) ? "bg-brand/5" : ""}`}><td className="px-2 py-1.5"><input type="checkbox" checked={selected.has(row.workload_id)} onChange={() => toggleOne(row.workload_id)} aria-label={`Select ${row.name}`} /></td><td className="px-2 py-1.5"><button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)} className="text-left font-medium text-gray-800 hover:text-brand hover:underline">{row.name}</button><div className="flex gap-1 text-[10px] text-gray-400">{row.environment && <span>{row.environment}</span>}{row.stale && row.has_scan && <span className="rounded bg-amber-50 px-1 text-amber-600">stale</span>}</div></td>
            <td className="px-2 py-1.5">{running ? <span className="inline-flex items-center gap-1 text-brand"><span className="animate-spin">↻</span>scanning…</span> : queued ? <span className="text-gray-400">queued</span> : error ? <span className="rounded bg-red-50 px-1.5 py-0.5 font-medium text-red-700" title={error}>⚠ failed</span> : !row.has_scan ? <span className="text-gray-400">never</span> : <PercentPill value={row.pct_protected} />}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan ? row.total : "—"}</td><td className="px-2 py-1.5 tabular-nums text-green-600">{row.has_scan ? row.protected : "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan && row.pct_offsite != null ? `${row.pct_offsite}%` : "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan && row.pct_recent_job != null ? `${row.pct_recent_job}%` : "—"}</td><td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan ? row.dr_pairs : "—"}</td><td className={`px-2 py-1.5 tabular-nums ${row.dr_pairs_unhealthy ? "font-semibold text-red-600" : "text-gray-400"}`}>{row.has_scan ? row.dr_pairs_unhealthy : "—"}</td><td className="px-2 py-1.5 text-gray-500" title={error || row.run_at}>{running ? "live Azure scan" : error ? "failed — retry" : relTime(row.run_at)}</td><td className="px-2 py-1.5"><button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open ▸</button></td>
          </tr>;
        })}</tbody></table>}
    </div>
  </div>;
}
