// Shared "Cleanup" tab — a polished cross-scope run-deletion UX reused by all six screens
// (Performance Profiler, Change Explorer, Assessments, Monitoring/Telemetry/Backup-DR Coverage).
//
// Each run history store accumulates full snapshots (perf matrices + series, change events,
// coverage snapshots, assessment findings) that pile up over time. This tab lists EVERY run
// across EVERY scope with its storage size, offers smart one-click presets (older-than / keep-
// latest-per-scope / demo / empty / trashed) with a live "frees N runs · X MB" preview, and a
// safe bulk action bar (Trash → restorable, Purge → permanent with confirm). Soft-delete first.
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type CleanupRun, type CleanupData } from "../../api";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
function relTime(iso: string): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
function ageDays(iso: string): number {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return 0;
  return (Date.now() - t) / 86400_000;
}

export type CleanupPreset = "older7" | "older30" | "older90" | "keepLatest" | "demo" | "empty";

export function RunCleanup({
  prefix,
  queryKey,
  invalidateKeys = [],
  renderMeta,
  isEmptyRun,
}: {
  /** API prefix, e.g. "/performance", "/changeexplorer", "/amba". */
  prefix: string;
  /** React Query key for this feature's cleanup data. */
  queryKey: readonly unknown[];
  /** Extra query keys to invalidate after a mutation (fleet/runs/history grids). */
  invalidateKeys?: readonly unknown[][];
  /** Per-screen meta cell (score / changes / coverage / findings). */
  renderMeta: (r: CleanupRun) => React.ReactNode;
  /** Identifies "empty/failed" runs for the Empty preset (0 resources / 0 changes / errored). */
  isEmptyRun?: (r: CleanupRun) => boolean;
}) {
  const qc = useQueryClient();
  const cleanupQ = useQuery<CleanupData>({ queryKey, queryFn: () => api.cleanupList(prefix), refetchOnWindowFocus: false });
  const runs = useMemo(() => cleanupQ.data?.runs ?? [], [cleanupQ.data]);
  const stats = cleanupQ.data?.stats;

  const [showTrashed, setShowTrashed] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [confirmPurge, setConfirmPurge] = useState<CleanupRun[] | null>(null);
  // "Retain last N per scope" — keep the newest N runs of each scope, select the rest.
  const [keepN, setKeepN] = useState(2);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = runs.filter((r) => (showTrashed ? !!r.deleted_at : !r.deleted_at));
    if (q) list = list.filter((r) => r.scope_name.toLowerCase().includes(q));
    return list;
  }, [runs, showTrashed, search]);

  // Group by scope for a clean, scannable layout.
  const grouped = useMemo(() => {
    const m = new Map<string, { name: string; runs: CleanupRun[] }>();
    for (const r of visible) {
      const k = `${r.scope_kind}:${r.scope_id}`;
      if (!m.has(k)) m.set(k, { name: r.scope_name || r.scope_id, runs: [] });
      m.get(k)!.runs.push(r);
    }
    return [...m.values()].sort((a, b) => b.runs.length - a.runs.length);
  }, [visible]);

  const toggle = (id: string) =>
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const selectMany = (ids: string[], on: boolean) =>
    setSelected((s) => { const n = new Set(s); ids.forEach((i) => (on ? n.add(i) : n.delete(i))); return n; });

  const allVisibleSelected = visible.length > 0 && visible.every((r) => selected.has(r.id));
  const selectedRuns = runs.filter((r) => selected.has(r.id));
  const selectedBytes = selectedRuns.reduce((a, r) => a + (r.size_bytes || 0), 0);

  // --- Smart presets: compute the matching ACTIVE runs (operate on non-trashed). ---
  function presetRuns(p: CleanupPreset): CleanupRun[] {
    const active = runs.filter((r) => !r.deleted_at);
    switch (p) {
      case "older7": return active.filter((r) => ageDays(r.run_at) > 7);
      case "older30": return active.filter((r) => ageDays(r.run_at) > 30);
      case "older90": return active.filter((r) => ageDays(r.run_at) > 90);
      case "demo": return active.filter((r) => r.demo);
      case "empty": return isEmptyRun ? active.filter(isEmptyRun) : [];
      case "keepLatest": {
        // All but the newest N runs of each scope (retain last N).
        const n = Math.max(0, keepN);
        const byScope = new Map<string, CleanupRun[]>();
        for (const r of active) {
          const k = `${r.scope_kind}:${r.scope_id}`;
          (byScope.get(k) ?? byScope.set(k, []).get(k)!).push(r);
        }
        const out: CleanupRun[] = [];
        for (const list of byScope.values()) {
          const sorted = [...list].sort((a, b) => (b.run_at || "").localeCompare(a.run_at || ""));
          out.push(...sorted.slice(n));
        }
        return out;
      }
    }
  }
  function applyPreset(p: CleanupPreset) {
    const ids = presetRuns(p).map((r) => r.id);
    setShowTrashed(false);
    setSelected(new Set(ids));
  }

  async function run(action: "trash" | "restore" | "purge", ids: string[]) {
    if (ids.length === 0) return;
    setBusy(true);
    setMsg(null);
    try {
      const fn = action === "trash" ? api.cleanupTrash : action === "restore" ? api.cleanupRestore : api.cleanupPurge;
      const r = await fn(prefix, ids);
      const freed = typeof r.freed_bytes === "number" ? ` · freed ${fmtBytes(r.freed_bytes)}` : "";
      const verb = action === "trash" ? "Trashed" : action === "restore" ? "Restored" : "Permanently deleted";
      setMsg({ text: `${verb} ${r.count} run${r.count === 1 ? "" : "s"}${freed}.`, ok: true });
      setSelected(new Set());
      setConfirmPurge(null);
      await qc.invalidateQueries({ queryKey });
      for (const k of invalidateKeys) await qc.invalidateQueries({ queryKey: k });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  const presets: { id: CleanupPreset; label: string; show?: boolean }[] = [
    { id: "older30", label: "Older than 30 days" },
    { id: "older90", label: "Older than 90 days" },
    { id: "demo", label: "Demo runs" },
    { id: "empty", label: "Empty / failed", show: !!isEmptyRun },
  ];

  const reclaimable = stats ? stats.trashed_bytes : 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Header strip */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">🧹 Cleanup</h2>
            <p className="text-[11px] text-gray-500">Delete old analysis runs to reclaim storage. Trash is restorable; purge is permanent.</p>
          </div>
          {stats && (
            <div className="ml-auto flex flex-wrap items-center gap-4 text-[11px] text-gray-600">
              <Stat label="runs" value={String(stats.active_runs)} />
              <Stat label="storage" value={fmtBytes(stats.total_bytes)} />
              <Stat label="scopes" value={String(stats.scopes)} />
              <Stat label="oldest" value={relTime(stats.oldest_run_at)} />
              {stats.trashed_runs > 0 && <Stat label="trashed" value={`${stats.trashed_runs} · ${fmtBytes(reclaimable)}`} tone="text-amber-600" />}
            </div>
          )}
        </div>

        {/* Presets + view toggle */}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-gray-500">Quick select:</span>
          {/* Retain last N per scope — keep the newest N runs of each scope, select the rest. */}
          {(() => {
            const matched = presetRuns("keepLatest");
            const n = matched.length;
            const bytes = matched.reduce((a, r) => a + r.size_bytes, 0);
            return (
              <span className="inline-flex items-center rounded-full border text-[11px]">
                <button
                  onClick={() => applyPreset("keepLatest")}
                  disabled={n === 0 || showTrashed}
                  className="rounded-l-full py-1 pl-2.5 pr-1 text-gray-600 hover:text-brand disabled:opacity-40"
                  title={`Keep newest ${keepN} per scope; select the other ${n} run${n === 1 ? "" : "s"} · ${fmtBytes(bytes)}`}
                >
                  Retain last
                </button>
                <span className="flex items-center">
                  <button onClick={() => setKeepN((v) => Math.max(0, v - 1))} disabled={showTrashed} className="px-1 text-gray-400 hover:text-brand disabled:opacity-40">−</button>
                  <span className="min-w-[1.1rem] text-center font-semibold text-gray-700">{keepN}</span>
                  <button onClick={() => setKeepN((v) => Math.min(30, v + 1))} disabled={showTrashed} className="px-1 text-gray-400 hover:text-brand disabled:opacity-40">+</button>
                </span>
                <button
                  onClick={() => applyPreset("keepLatest")}
                  disabled={n === 0 || showTrashed}
                  className="rounded-r-full py-1 pl-1 pr-2.5 text-gray-400 hover:text-brand disabled:opacity-40"
                  title={`per scope — select ${n} older run${n === 1 ? "" : "s"} for deletion`}
                >
                  per scope <span className="text-gray-400">({n})</span>
                </button>
              </span>
            );
          })()}
          {presets.filter((p) => p.show !== false).map((p) => {
            const n = presetRuns(p.id).length;
            return (
              <button
                key={p.id}
                onClick={() => applyPreset(p.id)}
                disabled={n === 0 || showTrashed}
                className="rounded-full border px-2.5 py-1 text-[11px] text-gray-600 hover:border-brand hover:text-brand disabled:opacity-40"
                title={`${n} run${n === 1 ? "" : "s"} · ${fmtBytes(presetRuns(p.id).reduce((a, r) => a + r.size_bytes, 0))}`}
              >
                {p.label} <span className="text-gray-400">({n})</span>
              </button>
            );
          })}
          <div className="ml-auto flex items-center gap-2">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter scopes…" className="w-40 rounded-md border px-2 py-1 text-xs" />
            <button
              onClick={() => { setShowTrashed((v) => !v); setSelected(new Set()); }}
              className={`rounded-md border px-2.5 py-1 text-xs font-medium ${showTrashed ? "border-amber-400 bg-amber-50 text-amber-700" : "text-gray-600 hover:bg-gray-50"}`}
            >
              🗑 Trash{stats && stats.trashed_runs > 0 ? ` (${stats.trashed_runs})` : ""}
            </button>
          </div>
        </div>

        {/* Bulk action bar */}
        {selected.size > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-brand/30 bg-brand/5 px-3 py-1.5 text-sm">
            <span className="font-medium text-brand">{selected.size} selected · {fmtBytes(selectedBytes)}</span>
            {showTrashed ? (
              <>
                <button onClick={() => void run("restore", [...selected])} disabled={busy} className="rounded-md border bg-white px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50">♻ Restore</button>
                <button onClick={() => setConfirmPurge(selectedRuns)} disabled={busy} className="rounded-md border border-red-300 bg-white px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50">⨯ Purge permanently</button>
              </>
            ) : (
              <>
                <button onClick={() => void run("trash", [...selected])} disabled={busy} className="rounded-md bg-gray-900 px-2.5 py-1 text-xs text-white hover:bg-black disabled:opacity-50">🗑 Trash selected</button>
                <button onClick={() => setConfirmPurge(selectedRuns)} disabled={busy} className="rounded-md border border-red-300 bg-white px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50">⨯ Purge permanently</button>
              </>
            )}
            <button onClick={() => setSelected(new Set())} className="text-xs text-gray-500 hover:text-gray-700">Clear</button>
          </div>
        )}
        {msg && (
          <div className={`mt-2 rounded-md border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}
      </div>

      {/* Table */}
      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {cleanupQ.isLoading ? (
          <Skeleton rows={8} />
        ) : cleanupQ.isError ? (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(cleanupQ.error)}</div>
        ) : visible.length === 0 ? (
          <div className="rounded-xl border border-dashed bg-gray-50 p-10 text-center text-sm text-gray-500">
            {showTrashed ? "Trash is empty." : runs.length === 0 ? "No analysis runs yet." : "No runs match."}
          </div>
        ) : (
          <div className="space-y-3">
            <label className="flex items-center gap-2 text-[11px] text-gray-500">
              <input type="checkbox" checked={allVisibleSelected} onChange={(e) => selectMany(visible.map((r) => r.id), e.target.checked)} />
              Select all shown ({visible.length})
            </label>
            {grouped.map((g) => {
              const ids = g.runs.map((r) => r.id);
              const allSel = ids.every((i) => selected.has(i));
              const groupBytes = g.runs.reduce((a, r) => a + r.size_bytes, 0);
              return (
                <div key={g.name} className="overflow-hidden rounded-xl border bg-white">
                  <div className="flex items-center gap-2 border-b bg-gray-50 px-3 py-1.5">
                    <input type="checkbox" checked={allSel} onChange={(e) => selectMany(ids, e.target.checked)} />
                    <span className="text-xs font-semibold text-gray-800">{g.name}</span>
                    <span className="text-[10px] text-gray-400">{g.runs.length} run{g.runs.length === 1 ? "" : "s"} · {fmtBytes(groupBytes)}</span>
                  </div>
                  <table className="w-full text-[12px]">
                    <tbody>
                      {g.runs.map((r) => (
                        <tr key={r.id} className={`border-b last:border-0 hover:bg-gray-50 ${selected.has(r.id) ? "bg-brand/5" : ""}`}>
                          <td className="w-8 px-3 py-1.5"><input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} /></td>
                          <td className="px-2 py-1.5 text-gray-600" title={r.run_at}>{relTime(r.run_at)}</td>
                          <td className="px-2 py-1.5">{renderMeta(r)}</td>
                          <td className="px-2 py-1.5 text-right tabular-nums text-gray-500">{fmtBytes(r.size_bytes)}</td>
                          {r.demo && <td className="px-2 py-1.5"><span className="rounded bg-violet-50 px-1 text-[10px] text-violet-600">demo</span></td>}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Purge confirmation */}
      {confirmPurge && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => setConfirmPurge(null)}>
          <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-gray-900">Permanently delete {confirmPurge.length} run{confirmPurge.length === 1 ? "" : "s"}?</h3>
            <p className="mt-1 text-sm text-gray-500">
              This frees <b>{fmtBytes(confirmPurge.reduce((a, r) => a + r.size_bytes, 0))}</b> and <b>cannot be undone</b>. Consider Trash (restorable) instead.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setConfirmPurge(null)} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
              <button onClick={() => void run("purge", confirmPurge.map((r) => r.id))} disabled={busy} className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50">{busy ? "Deleting…" : "Delete permanently"}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="flex items-baseline gap-1">
      <span className={`font-semibold ${tone ?? "text-gray-800"}`}>{value}</span>
      <span className="text-gray-400">{label}</span>
    </span>
  );
}
