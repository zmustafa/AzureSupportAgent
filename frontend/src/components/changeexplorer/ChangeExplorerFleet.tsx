// Fleet view for the Change Explorer: a dense, sortable table summarizing the LATEST change-
// analysis run for every workload, plus a mass-launch bar that analyzes the selected workloads
// over ONE shared time window. Runs stream in the background (parallelism 3) via the shared
// analysis registry, so progress survives tab switches / navigation.
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type ChangeFleetRow } from "../../api";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { TimeRangePicker } from "./TimeRangePicker";
import { peekAnalysis, startAnalysis, useAnalysisVersion } from "../ChangeExplorerView";

const MAX_PARALLEL = 3;

function pad(n: number): string { return String(n).padStart(2, "0"); }
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toIso(local: string): string { return local ? new Date(local).toISOString() : ""; }
function defaultStart(): string { return toLocalInput(new Date(Date.now() - 24 * 3600_000)); }
function defaultEnd(): string { return toLocalInput(new Date()); }

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
function fmtWindow(startIso: string, endIso: string): string {
  if (!startIso || !endIso) return "";
  try {
    const s = new Date(startIso);
    const e = new Date(endIso);
    const opt: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
    return `${s.toLocaleString(undefined, opt)} → ${e.toLocaleString(undefined, opt)}`;
  } catch { return ""; }
}

function CountCell({ n, cls }: { n: number; cls: string }) {
  return <span className={`tabular-nums ${n ? cls : "text-gray-300"}`}>{n || "—"}</span>;
}

type SortKey = "worst" | "changes" | "critical" | "name" | "run_at";

export function ChangeExplorerFleet({ onOpenWorkload }: { onOpenWorkload: (workloadId: string) => void }) {
  const version = useAnalysisVersion();
  const fleetQ = useQuery({ queryKey: ["changeFleet"], queryFn: api.changeExplorerFleet, refetchOnWindowFocus: false });
  const rows = useMemo(() => fleetQ.data?.workloads ?? [], [fleetQ.data]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("worst");
  const [scopeMode, setScopeMode] = useState<string>("workload");
  const [runAi, setRunAi] = useState<boolean>(false);
  const [start, setStart] = useState(() => defaultStart());
  const [end, setEnd] = useState(() => defaultEnd());
  const [rangeLabel, setRangeLabel] = useState("Last 24 hours");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // Mass-launch scheduler: a pending queue drained at MAX_PARALLEL concurrency. The set of
  // scopeKeys in THIS batch lets us count how many of our launches are still running.
  const [pending, setPending] = useState<ChangeFleetRow[]>([]);
  const batchKeys = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (pending.length === 0) return;
    let running = 0;
    for (const k of batchKeys.current) if (peekAnalysis(k)) running++;
    const slots = MAX_PARALLEL - running;
    if (slots <= 0) return;
    const toStart = pending.slice(0, slots);
    setPending((p) => p.slice(toStart.length));
    for (const row of toStart) {
      startAnalysis(`workload:${row.workload_id}`, {
        workload_id: row.workload_id,
        connection_id: row.connection_id || "",
        start_time: toIso(start),
        end_time: toIso(end),
        scope_mode: scopeMode,
        run_ai: runAi,
      });
    }
    // version drives re-scheduling as each run starts/finishes; refresh the grid too.
    void fleetQ.refetch();
  }, [pending, version, start, end, scopeMode]); // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = rows;
    if (q) list = list.filter((r) => r.name.toLowerCase().includes(q));
    const sorted = [...list];
    sorted.sort((a, b) => {
      switch (sortKey) {
        case "name": return a.name.localeCompare(b.name);
        case "changes": return (b.total_changes ?? 0) - (a.total_changes ?? 0);
        case "critical": return (b.critical_count ?? 0) - (a.critical_count ?? 0);
        case "run_at": return (b.run_at || "").localeCompare(a.run_at || "");
        case "worst":
        default:
          return (Number(a.has_runs) - Number(b.has_runs)) || ((b.critical_count ?? 0) - (a.critical_count ?? 0)) || ((b.high_count ?? 0) - (a.high_count ?? 0)) || ((b.total_changes ?? 0) - (a.total_changes ?? 0));
      }
    });
    return sorted;
  }, [rows, search, sortKey]);

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

  function launch() {
    const chosen = rows.filter((r) => selected.has(r.workload_id));
    if (chosen.length === 0 || !start || !end) return;
    chosen.forEach((r) => batchKeys.current.add(`workload:${r.workload_id}`));
    setPending((p) => [...p, ...chosen.filter((r) => !p.some((x) => x.workload_id === r.workload_id))]);
    setMsg({ text: `Launched ${runAi ? "AI" : "fast"} change analysis on ${chosen.length} workload${chosen.length === 1 ? "" : "s"} (${rangeLabel}). Running ${MAX_PARALLEL} at a time…`, ok: true });
    setSelected(new Set());
  }

  const analyzed = fleetQ.data?.analyzed ?? 0;
  const total = fleetQ.data?.total ?? rows.length;
  const activeRuns = rows.filter((r) => peekAnalysis(`workload:${r.workload_id}`)).length + pending.length;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Mass-launch toolbar */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">Fleet changes</h2>
            <p className="text-[11px] text-gray-500">
              Latest change analysis per workload. Select workloads, pick one window, and analyze them all at once.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-gray-500">{analyzed}/{total} analyzed{activeRuns > 0 ? ` · ${activeRuns} running` : ""}</span>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter workloads…"
              className="w-44 rounded-md border px-2 py-1 text-xs"
            />
            <select value={sortKey} onChange={(e) => setSortKey(e.target.value as SortKey)} className="rounded-md border px-2 py-1 text-xs text-gray-600" title="Sort">
              <option value="worst">Sort: worst first</option>
              <option value="critical">Sort: most critical</option>
              <option value="changes">Sort: most changes</option>
              <option value="run_at">Sort: newest run</option>
              <option value="name">Sort: name</option>
            </select>
            <select value={scopeMode} onChange={(e) => setScopeMode(e.target.value)} className="rounded-md border px-2 py-1 text-xs text-gray-600" title="Scope">
              <option value="workload">Workload only</option>
              <option value="workload_dependencies">Workload + deps</option>
            </select>
            {/* Analysis mode: Fast (deterministic only) vs AI (adds the slower AI enrichment pass). */}
            <div className="flex items-center rounded-md border bg-gray-50 p-0.5 text-xs" title="Fast = deterministic only (quick). AI = adds the AI enrichment pass (narrative + sharper risk), slower.">
              <button
                onClick={() => setRunAi(false)}
                className={`rounded px-2 py-0.5 ${!runAi ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}
              >
                ⚡ Fast
              </button>
              <button
                onClick={() => setRunAi(true)}
                className={`rounded px-2 py-0.5 ${runAi ? "bg-white font-medium text-brand shadow-sm" : "text-gray-500"}`}
              >
                ✨ AI
              </button>
            </div>
            <TimeRangePicker start={start} end={end} label={rangeLabel} onApply={(s, e, lbl) => { setStart(s); setEnd(e); setRangeLabel(lbl); }} />
            <button
              onClick={launch}
              disabled={selected.size === 0 || !start || !end}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
            >
              {runAi ? "✨" : "⚡"} Analyze {selected.size || ""} selected{runAi ? " with AI" : ""}
            </button>
          </div>
        </div>
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
          <div className="rounded-md border border-dashed bg-gray-50 px-4 py-10 text-center text-sm text-gray-500">No workloads yet. Create a workload, then analyze it here.</div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 z-10 bg-gray-50 text-left text-gray-500">
              <tr className="border-b">
                <th className="w-8 px-2 py-2">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll} title="Select all shown" />
                </th>
                <th className="px-2 py-2 font-medium">Workload</th>
                <th className="px-2 py-2 font-medium">Changes</th>
                <th className="px-2 py-2 font-medium">Critical</th>
                <th className="px-2 py-2 font-medium">High</th>
                <th className="px-2 py-2 font-medium">Medium</th>
                <th className="px-2 py-2 font-medium">Low</th>
                <th className="px-2 py-2 font-medium">Last run</th>
                <th className="px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => {
                const scopeKey = `workload:${r.workload_id}`;
                const running = peekAnalysis(scopeKey);
                const queued = pending.some((p) => p.workload_id === r.workload_id);
                return (
                  <tr key={r.workload_id} className={`border-b hover:bg-gray-50 ${selected.has(r.workload_id) ? "bg-brand/5" : ""}`}>
                    <td className="px-2 py-1.5">
                      <input type="checkbox" checked={selected.has(r.workload_id)} onChange={() => toggleOne(r.workload_id)} />
                    </td>
                    <td className="px-2 py-1.5">
                      <button onClick={() => onOpenWorkload(r.workload_id)} className="text-left font-medium text-gray-800 hover:text-brand hover:underline">
                        {r.name}
                      </button>
                      {r.environment && <div className="text-[10px] text-gray-400">{r.environment}</div>}
                    </td>
                    <td className="px-2 py-1.5">
                      {running ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-brand"><span className="animate-spin">↻</span>analyzing…</span>
                      ) : queued ? (
                        <span className="text-[11px] text-gray-400">queued</span>
                      ) : r.has_runs ? (
                        <span className="font-semibold tabular-nums text-gray-800">{r.total_changes}</span>
                      ) : (
                        <span className="text-[11px] text-gray-400">never</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5"><CountCell n={r.has_runs ? r.critical_count : 0} cls="font-semibold text-red-600" /></td>
                    <td className="px-2 py-1.5"><CountCell n={r.has_runs ? r.high_count : 0} cls="text-orange-600" /></td>
                    <td className="px-2 py-1.5"><CountCell n={r.has_runs ? r.medium_count : 0} cls="text-amber-600" /></td>
                    <td className="px-2 py-1.5"><CountCell n={r.has_runs ? r.low_count : 0} cls="text-blue-600" /></td>
                    <td className="px-2 py-1.5 text-gray-500" title={r.run_at || ""}>
                      {running ? (
                        <span className="truncate text-[11px] text-brand">{running.progress?.message || "starting…"}</span>
                      ) : (
                        <>
                          {relTime(r.run_at)}
                          {r.has_runs && <div className="text-[10px] text-gray-400">{fmtWindow(r.start_time, r.end_time)}</div>}
                        </>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <button onClick={() => onOpenWorkload(r.workload_id)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open ▸</button>
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
