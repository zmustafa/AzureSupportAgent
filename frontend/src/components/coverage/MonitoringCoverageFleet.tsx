import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type AmbaCoverageFleetRow } from "../../api";
import { queryClient } from "../../queryClient";
import { formatError } from "../../utils/format";
import { isRefreshing, peekRefreshError, startBackgroundRefresh, subscribeBackgroundRefresh, useBackgroundRefresh } from "../../utils/backgroundRefresh";
import { Skeleton } from "../../utils/perf";
import { enqueueFleet, fleetOutstanding, fleetQueuedKeys, useFleetQueue } from "../fleetScheduler";

const MAX_PARALLEL = 3;
const STAGGER_MS = 400;
const QUEUE_ID = "ambaCoverageFleet";

type SortKey = "worst" | "coverage" | "missing" | "misconfigured" | "resources" | "name" | "run_at";
type SortDir = "asc" | "desc";

function refreshKey(row: AmbaCoverageFleetRow): string {
  return `amba:workload:${row.workload_id}:${row.connection_id || ""}`;
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

function CoveragePill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-gray-400">never</span>;
  const tone = value >= 80 ? "bg-green-100 text-green-700" : value >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`inline-block min-w-[3rem] rounded px-1.5 py-0.5 text-center font-semibold tabular-nums ${tone}`}>{value}%</span>;
}

export function MonitoringCoverageFleet({ onOpenWorkload }: { onOpenWorkload: (workloadId: string, connectionId: string) => void }) {
  useBackgroundRefresh();
  useFleetQueue();
  const fleetQ = useQuery({ queryKey: ["ambaFleet"], queryFn: api.ambaCoverageFleet, refetchOnWindowFocus: false });
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const queuedKeys = fleetQueuedKeys(QUEUE_ID);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const list = query ? rows.filter((row) => `${row.name} ${row.environment} ${row.criticality}`.toLowerCase().includes(query)) : rows;
    const sorted = [...list];
    const direction = sortDir === "asc" ? 1 : -1;
    sorted.sort((a, b) => {
      if (sortKey === "worst") {
        return Number(b.has_scan) - Number(a.has_scan)
          || ((a.coverage_pct ?? 999) - (b.coverage_pct ?? 999))
          || (b.missing - a.missing)
          || a.name.localeCompare(b.name);
      }
      if (sortKey === "name") return direction * a.name.localeCompare(b.name);
      if (sortKey === "run_at") return direction * (a.run_at || "").localeCompare(b.run_at || "");
      const value = (row: AmbaCoverageFleetRow) => {
        if (!row.has_scan) return -1;
        if (sortKey === "coverage") return row.coverage_pct ?? -1;
        if (sortKey === "missing") return row.missing;
        if (sortKey === "misconfigured") return row.misconfigured;
        return row.resources;
      };
      return direction * (value(a) - value(b));
    });
    return sorted;
  }, [rows, search, sortKey, sortDir]);

  const allSelected = filtered.length > 0 && filtered.every((row) => selected.has(row.workload_id));
  function toggleAll() {
    setSelected((current) => {
      const next = new Set(current);
      if (allSelected) filtered.forEach((row) => next.delete(row.workload_id));
      else filtered.forEach((row) => next.add(row.workload_id));
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

  function enqueueRows(chosen: AmbaCoverageFleetRow[]) {
    enqueueFleet(
      QUEUE_ID,
      chosen.map((row) => ({
        key: refreshKey(row),
        run: () => startBackgroundRefresh(refreshKey(row), async () => {
          const snapshot = await api.refreshAmba({ workload_id: row.workload_id, connection_id: row.connection_id || undefined });
          queryClient.setQueryData(["amba", "workload", row.workload_id, "", row.connection_id || ""], snapshot);
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["ambaFleet"] }),
            queryClient.invalidateQueries({ queryKey: ["amba-trend", "workload", row.workload_id] }),
            queryClient.invalidateQueries({ queryKey: ["coverage-runs", "amba", "workload", row.workload_id] }),
          ]);
        }),
      })),
      { maxParallel: MAX_PARALLEL, staggerMs: STAGGER_MS, isRunning: isRefreshing, subscribe: subscribeBackgroundRefresh },
    );
  }

  function launch() {
    const chosen = rows.filter((row) => selected.has(row.workload_id));
    if (!chosen.length) return;
    enqueueRows(chosen);
    setMessage({ text: `Launched monitoring coverage scans for ${chosen.length} workload${chosen.length === 1 ? "" : "s"}. Running ${MAX_PARALLEL} at a time…`, ok: true });
    setSelected(new Set());
  }

  const failedRows = rows.filter((row) => {
    const key = refreshKey(row);
    return !!peekRefreshError(key) && !isRefreshing(key) && !queuedKeys.has(key);
  });
  function retryFailed() {
    enqueueRows(failedRows);
    setMessage({ text: `Retrying ${failedRows.length} failed coverage scan${failedRows.length === 1 ? "" : "s"}…`, ok: true });
  }

  function clickSort(key: SortKey, defaultDirection: SortDir = "desc") {
    if (sortKey === key) setSortDir((current) => current === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(defaultDirection); }
  }
  const SortHeader = ({ label, value, defaultDirection = "desc" }: { label: string; value: SortKey; defaultDirection?: SortDir }) => (
    <th onClick={() => clickSort(value, defaultDirection)} className={`cursor-pointer select-none px-2 py-2 font-medium hover:text-gray-700 ${sortKey === value ? "text-gray-700" : ""}`}>
      {label}<span className="ml-0.5 text-[9px] text-gray-400">{sortKey === value ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
    </th>
  );

  const scanned = fleetQ.data?.scanned ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const outstanding = fleetOutstanding(QUEUE_ID);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">Fleet monitoring coverage</h2>
            <p className="text-[11px] text-gray-500">Latest AMBA scan per workload. Select workloads and refresh their monitoring baselines as one background fleet.</p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-gray-500">{scanned}/{total} scanned{outstanding ? ` · ${outstanding} outstanding` : ""}</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Filter workloads…" className="w-44 rounded-md border px-2 py-1 text-xs" />
            <select value={sortKey} onChange={(event) => { const key = event.target.value as SortKey; setSortKey(key); setSortDir(key === "coverage" || key === "name" ? "asc" : "desc"); }} className="rounded-md border px-2 py-1 text-xs text-gray-600">
              <option value="worst">Sort: worst first</option>
              <option value="coverage">Sort: lowest coverage</option>
              <option value="missing">Sort: most missing</option>
              <option value="misconfigured">Sort: most misconfigured</option>
              <option value="run_at">Sort: newest scan</option>
              <option value="name">Sort: name</option>
            </select>
            {failedRows.length > 0 && <button onClick={retryFailed} className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100">↻ Retry failed ({failedRows.length})</button>}
            <button onClick={launch} disabled={!selected.size} className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">▶ Scan {selected.size || ""} selected</button>
          </div>
        </div>
        {message && <div className={`mt-2 rounded-md border px-3 py-1.5 text-xs ${message.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{message.text}</div>}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {fleetQ.isLoading ? <Skeleton rows={8} /> : fleetQ.isError ? (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(fleetQ.error)}</div>
        ) : !rows.length ? (
          <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads exist yet.</div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500"><tr className="border-b">
              <th className="w-8 px-2 py-2"><input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all shown workloads" /></th>
              <SortHeader label="Workload" value="name" defaultDirection="asc" />
              <SortHeader label="Coverage" value="coverage" defaultDirection="asc" />
              <SortHeader label="Resources" value="resources" />
              <th className="px-2 py-2 font-medium">Recommended</th>
              <th className="px-2 py-2 font-medium">Present</th>
              <SortHeader label="Missing" value="missing" />
              <SortHeader label="Misconfigured" value="misconfigured" />
              <SortHeader label="Last scan" value="run_at" />
              <th className="px-2 py-2" />
            </tr></thead>
            <tbody>{filtered.map((row) => {
              const key = refreshKey(row);
              const running = isRefreshing(key);
              const queued = queuedKeys.has(key);
              const error = !running && !queued ? peekRefreshError(key) : undefined;
              return <tr key={row.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(row.workload_id) ? "bg-brand/5" : ""}`}>
                <td className="px-2 py-1.5"><input type="checkbox" checked={selected.has(row.workload_id)} onChange={() => toggleOne(row.workload_id)} aria-label={`Select ${row.name}`} /></td>
                <td className="px-2 py-1.5"><button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)} className="text-left font-medium text-gray-800 hover:text-brand hover:underline">{row.name}</button><div className="flex gap-1 text-[10px] text-gray-400">{row.environment && <span>{row.environment}</span>}{row.stale && row.has_scan && <span className="rounded bg-amber-50 px-1 text-amber-600">stale</span>}</div></td>
                <td className="px-2 py-1.5">{running ? <span className="inline-flex items-center gap-1 text-brand"><span className="animate-spin">↻</span>scanning…</span> : queued ? <span className="text-gray-400">queued</span> : error ? <span className="rounded bg-red-50 px-1.5 py-0.5 font-medium text-red-700" title={error}>⚠ failed</span> : <CoveragePill value={row.has_scan ? row.coverage_pct : null} />}</td>
                <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan ? row.resources : "—"}</td>
                <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_scan ? row.recommended : "—"}</td>
                <td className="px-2 py-1.5 tabular-nums text-green-600">{row.has_scan ? row.present : "—"}</td>
                <td className={`px-2 py-1.5 tabular-nums ${row.missing ? "font-semibold text-red-600" : "text-gray-400"}`}>{row.has_scan ? row.missing : "—"}</td>
                <td className={`px-2 py-1.5 tabular-nums ${row.misconfigured ? "text-amber-600" : "text-gray-400"}`}>{row.has_scan ? row.misconfigured : "—"}</td>
                <td className="px-2 py-1.5 text-gray-500" title={error || row.run_at}>{running ? "live Azure scan" : error ? "failed — retry" : relTime(row.run_at)}</td>
                <td className="px-2 py-1.5"><button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open ▸</button></td>
              </tr>;
            })}</tbody>
          </table>
        )}
      </div>
    </div>
  );
}
