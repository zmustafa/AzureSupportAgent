// Backup Manager — Fleet.
//
// One row per workload, showing the headline of its LAST analysis. The SQL batch worker owns
// the complete selected tail and survives navigation, reloads, and process restarts.
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type BackupManagerFleetRow } from "../../api";
import { queryKeys } from "../../queryKeys";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { DurableBatchBar, useDurableBatch } from "../DurableBatch";

type SortKey =
  | "worst" | "name" | "protected" | "gaps" | "failed" | "rpo" | "posture" | "cost" | "run_at";
type SortDir = "asc" | "desc";

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

function fmtMoney(value: number, currency: string): string {
  if (!value) return "—";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency: currency || "USD", maximumFractionDigits: 0,
    }).format(value);
  } catch {
    return `${Math.round(value)}`;
  }
}

function PercentPill({ value }: { value: number | null }) {
  if (value == null) return <span className="text-gray-400">n/a</span>;
  const tone = value >= 90 ? "bg-green-100 text-green-700"
    : value >= 60 ? "bg-amber-100 text-amber-700"
      : "bg-red-100 text-red-700";
  return <span className={`inline-block min-w-[3rem] rounded px-1.5 py-0.5 text-center font-semibold tabular-nums ${tone}`}>{value}%</span>;
}

export function BackupManagerFleet({ onOpenWorkload }: {
  onOpenWorkload: (workloadId: string, connectionId: string) => void;
}) {
  const durable = useDurableBatch("backup_manager", [queryKeys.backupManager.fleet, queryKeys.backupManager.snapshotRoot, queryKeys.backupManager.cleanup]);

  const fleetQ = useQuery({
    queryKey: queryKeys.backupManager.fleet,
    queryFn: api.backupManagerFleet,
    refetchOnWindowFocus: false,
  });
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "launch" | "retry" | "cancel">("");

  const stateOf = (row: BackupManagerFleetRow): { state: "running" | "queued" | "failed" | "idle"; error?: string } => {
    const item = durable.itemsByWorkload.get(row.workload_id);
    if (item?.status === "running") return { state: "running" };
    if (item?.status === "queued") return { state: "queued" };
    if (item && ["failed", "partial", "cancelled"].includes(item.status)) return { state: "failed", error: item.error || item.message };
    return { state: "idle" };
  };

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const list = query
      ? rows.filter((row) => `${row.name} ${row.environment} ${row.criticality}`.toLowerCase().includes(query))
      : rows;
    if (sortKey === "worst") return list; // the server already orders worst-first
    const direction = sortDir === "asc" ? 1 : -1;
    const value = (row: BackupManagerFleetRow): number => {
      if (!row.has_analysis) return -1;
      switch (sortKey) {
        case "protected": return row.pct_protected ?? -1;
        case "gaps": return row.gaps;
        case "failed": return row.failed_jobs;
        case "rpo": return row.rpo_attainment_pct ?? -1;
        case "posture": return row.posture_score;
        case "cost": return row.monthly_cost;
        default: return 0;
      }
    };
    return [...list].sort((a, b) => {
      if (sortKey === "name") return direction * a.name.localeCompare(b.name);
      if (sortKey === "run_at") return direction * (a.run_at || "").localeCompare(b.run_at || "");
      return direction * (value(a) - value(b));
    });
  }, [rows, search, sortKey, sortDir]);

  const launchable = filtered.filter((row) => !row.demo);
  const allSelected = launchable.length > 0 && launchable.every((row) => selected.has(row.workload_id));
  function toggleAll() {
    setSelected((current) => {
      const next = new Set(current);
      launchable.forEach((row) => (allSelected ? next.delete(row.workload_id) : next.add(row.workload_id)));
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

  function launch() {
    const chosen = rows.filter((row) => selected.has(row.workload_id));
    if (!chosen.length) return;
    setBusy("launch");
    void durable.launch(chosen.filter((row) => !row.demo).map((row) => row.workload_id))
      .then(() => setMessage(`Queued ${chosen.length} backup analysis${chosen.length === 1 ? "" : "es"}. The SQL worker owns the complete batch.`))
      .catch((error) => setMessage(formatError(error))).finally(() => setBusy(""));
    setSelected(new Set());
  }

  const failedRows = rows.filter((row) => stateOf(row).state === "failed");
  function retryFailed() {
    setBusy("retry");
    void durable.retry().then(() => setMessage(`Queued ${failedRows.length} failed/partial analysis${failedRows.length === 1 ? "" : "es"} for retry.`))
      .catch((error) => setMessage(formatError(error))).finally(() => setBusy(""));
  }

  function clickSort(key: SortKey, defaultDirection: SortDir = "desc") {
    if (sortKey === key) setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(defaultDirection); }
  }
  const SortHeader = ({ label, value, defaultDirection = "desc" }: {
    label: string; value: SortKey; defaultDirection?: SortDir;
  }) => (
    <th onClick={() => clickSort(value, defaultDirection)}
      className={`cursor-pointer select-none px-2 py-2 font-medium hover:text-gray-700 ${sortKey === value ? "text-gray-700" : ""}`}>
      {label}<span className="ml-0.5 text-[9px] text-gray-400">{sortKey === value ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
    </th>
  );

  const analyzed = fleetQ.data?.analyzed ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const outstanding = durable.active && durable.batch ? durable.batch.total - durable.batch.completed : 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">Fleet backup posture</h2>
            <p className="text-[11px] text-gray-500">
              The last analysis of every workload, from cache. Select workloads to analyze them as one
              durable background sweep with server-side Azure admission control.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-gray-500">
              {analyzed}/{total} analyzed{outstanding ? ` · ${outstanding} outstanding` : ""}
            </span>
            <input value={search} onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter workloads…" aria-label="Filter workloads"
              className="w-44 rounded-md border px-2 py-1 text-xs" />
            <select value={sortKey} aria-label="Sort fleet"
              onChange={(event) => {
                const key = event.target.value as SortKey;
                setSortKey(key);
                setSortDir(key === "protected" || key === "name" || key === "rpo" || key === "posture" ? "asc" : "desc");
              }}
              className="rounded-md border px-2 py-1 text-xs text-gray-600">
              <option value="worst">Sort: worst first</option>
              <option value="protected">Sort: lowest protected</option>
              <option value="gaps">Sort: most gaps</option>
              <option value="failed">Sort: failing jobs</option>
              <option value="rpo">Sort: lowest RPO attainment</option>
              <option value="posture">Sort: weakest vault posture</option>
              <option value="cost">Sort: highest cost</option>
              <option value="run_at">Sort: newest analysis</option>
              <option value="name">Sort: name</option>
            </select>
            {failedRows.length > 0 && (
              <button onClick={retryFailed} disabled={durable.active || busy !== ""}
                className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 disabled:opacity-50">
                ↻ Retry failed ({failedRows.length})
              </button>
            )}
            <button onClick={launch} disabled={!selected.size || durable.active || busy !== ""}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">
              ▶ Analyze {selected.size || ""} selected
            </button>
          </div>
        </div>
        <DurableBatchBar batch={durable.batch} cancelling={busy === "cancel"} onCancel={() => { setBusy("cancel"); void durable.cancel().finally(() => setBusy("")); }} />
        {message && <div className="mt-2 rounded-md border border-green-200 bg-green-50 px-3 py-1.5 text-xs text-green-700">{message}</div>}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {fleetQ.isLoading ? <Skeleton rows={8} />
          : fleetQ.isError ? <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(fleetQ.error)}</div>
            : !rows.length ? <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads exist yet.</div>
              : (
                <table className="w-full text-[12px]">
                  <thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500">
                    <tr className="border-b">
                      <th className="w-8 px-2 py-2">
                        <input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all shown workloads" />
                      </th>
                      <SortHeader label="Workload" value="name" defaultDirection="asc" />
                      <SortHeader label="Protected" value="protected" defaultDirection="asc" />
                      <SortHeader label="Items" value="gaps" />
                      <SortHeader label="Gaps" value="gaps" />
                      <SortHeader label="Failing jobs" value="failed" />
                      <SortHeader label="RPO" value="rpo" defaultDirection="asc" />
                      <SortHeader label="Vault posture" value="posture" defaultDirection="asc" />
                      <SortHeader label="Est. cost / mo" value="cost" />
                      <SortHeader label="Last analysis" value="run_at" />
                      <th className="px-2 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((row) => {
                      const { state, error } = stateOf(row);
                      const item = durable.itemsByWorkload.get(row.workload_id);
                      return (
                        <tr key={row.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(row.workload_id) ? "bg-brand/5" : ""}`}>
                          <td className="px-2 py-1.5">
                            <input type="checkbox" checked={selected.has(row.workload_id)} disabled={row.demo}
                              onChange={() => toggleOne(row.workload_id)} aria-label={`Select ${row.name}`} />
                          </td>
                          <td className="px-2 py-1.5">
                            <button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)}
                              className="text-left font-medium text-gray-800 hover:text-brand hover:underline">{row.name}</button>
                            <div className="flex gap-1 text-[10px] text-gray-400">
                              {row.environment && <span>{row.environment}</span>}
                              {row.demo && <span className="rounded bg-indigo-50 px-1 text-indigo-600">demo</span>}
                              {row.partial && <span className="rounded bg-amber-50 px-1 text-amber-600" title={row.errors.join(", ")}>partial</span>}
                            </div>
                          </td>
                          <td className="px-2 py-1.5">
                            {state === "running" ? <span className="inline-flex items-center gap-1 text-brand"><span className="animate-spin">↻</span>analyzing…</span>
                              : state === "queued" ? <span className="text-gray-400">queued</span>
                                : state === "failed" ? <span className="rounded bg-red-50 px-1.5 py-0.5 font-medium text-red-700" title={error}>⚠ failed</span>
                                  : !row.has_analysis ? <span className="text-gray-400">never</span>
                                    : <PercentPill value={row.pct_protected} />}
                          </td>
                          <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_analysis ? row.protected_items : "—"}</td>
                          <td className={`px-2 py-1.5 tabular-nums ${row.gaps ? "font-semibold text-red-600" : "text-gray-400"}`}>{row.has_analysis ? row.gaps : "—"}</td>
                          <td className={`px-2 py-1.5 tabular-nums ${row.failed_jobs ? "font-semibold text-red-600" : "text-gray-400"}`}>{row.has_analysis ? row.failed_jobs : "—"}</td>
                          <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_analysis && row.rpo_attainment_pct != null ? `${row.rpo_attainment_pct}%` : "—"}</td>
                          <td className="px-2 py-1.5 tabular-nums text-gray-600">
                            {row.has_analysis ? <span title={`${row.red_vaults} vault(s) at risk`}>{row.posture_score}{row.red_vaults ? <span className="ml-1 text-red-600">· {row.red_vaults} red</span> : null}</span> : "—"}
                          </td>
                          <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.has_analysis ? fmtMoney(row.monthly_cost, row.currency) : "—"}</td>
                          <td className="px-2 py-1.5 text-gray-500" title={item?.message || row.run_at}>
                            {state === "running" ? (item?.message ? item.message.slice(0, 42) : "starting…")
                              : state === "failed" ? "failed — retry"
                                : relTime(row.run_at)}
                          </td>
                          <td className="px-2 py-1.5">
                            <button onClick={() => onOpenWorkload(row.workload_id, row.connection_id)}
                              className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open ▸</button>
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
