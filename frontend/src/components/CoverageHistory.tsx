import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type CoverageFeature, type CoverageRunSummary } from "../api";
import { formatError } from "../utils/format";

function fmtTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function tone(pct: number | null): string {
  if (pct == null) return "text-gray-400";
  if (pct >= 80) return "text-green-600";
  if (pct >= 50) return "text-amber-600";
  return "text-red-600";
}

/** Stable react-query key for a scope's run history — the owning view invalidates the same
 *  key after a refresh so the new scan shows up immediately. */
export function coverageRunsKey(feature: CoverageFeature, scopeKind: string, workloadId: string, subId: string) {
  return ["coverage-runs", feature, scopeKind, workloadId, subId] as const;
}

// Shared scan-history panel for the three coverage dashboards (Monitoring / Telemetry /
// Backup-DR), mirroring the Performance Profiler's run history: a table of saved scans with
// View (re-opens the snapshot) + Delete (soft-delete to Trash), plus a Trash panel with
// restore / delete-forever / empty.
export function CoverageHistory<T>({
  feature,
  scopeKind,
  workloadId,
  subId,
  enabled,
  headlineLabel,
  onView,
}: {
  feature: CoverageFeature;
  scopeKind: "workload" | "subscription";
  workloadId: string;
  subId: string;
  enabled: boolean;
  headlineLabel: string;
  onView: (snap: T) => void;
}) {
  const params = scopeKind === "workload" ? { workload_id: workloadId } : { subscription_id: subId };
  const [showTrash, setShowTrash] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const runsQ = useQuery({
    queryKey: coverageRunsKey(feature, scopeKind, workloadId, subId),
    queryFn: () => api.coverageRuns(feature, params),
    enabled,
  });
  const runs = runsQ.data?.runs ?? [];

  const trashQ = useQuery({
    queryKey: ["coverage-runs-trash", feature, scopeKind, workloadId, subId],
    queryFn: () => api.coverageTrashedRuns(feature, params),
    enabled: enabled && showTrash,
  });
  const trashed = trashQ.data?.runs ?? [];

  async function view(id: string) {
    setBusy(`view:${id}`);
    setMsg(null);
    try {
      const r = await api.coverageRun<T>(feature, id);
      if (r.ok && r.run) onView(r.run);
      else setMsg({ text: r.detail || "Run not found.", ok: false });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function del(id: string) {
    if (!window.confirm("Move this scan to the Trash?")) return;
    setBusy(`del:${id}`);
    setMsg(null);
    try {
      await api.deleteCoverageRun(feature, id);
      await runsQ.refetch();
      trashQ.refetch();
      setMsg({ text: "Moved to Trash.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function restore(id: string) {
    setBusy(`restore:${id}`);
    try {
      await api.restoreCoverageRun(feature, id);
      await Promise.all([runsQ.refetch(), trashQ.refetch()]);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function purge(id: string) {
    if (!window.confirm("Permanently delete this scan? This cannot be undone.")) return;
    setBusy(`purge:${id}`);
    try {
      await api.purgeCoverageRun(feature, id);
      await trashQ.refetch();
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function emptyTrash() {
    if (!window.confirm("Permanently delete ALL trashed scans for this scope? This cannot be undone.")) return;
    setBusy("empty");
    try {
      await api.emptyCoverageTrash(feature, params);
      await trashQ.refetch();
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  if (!enabled) return null;

  return (
    <div className="mb-5">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-sm font-semibold text-gray-900">Scan history</h2>
        <span className="text-[11px] text-gray-400">{runs.length} scan(s) for this scope</span>
        <button
          onClick={() => setShowTrash((v) => !v)}
          title="Show trashed scans"
          className={`ml-auto rounded-md border px-2.5 py-1 text-[11px] font-medium ${showTrash ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
        >
          🗑 Trash{trashed.length ? ` (${trashed.length})` : ""}
        </button>
      </div>

      {msg && (
        <div className={`mb-2 rounded-md border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}

      <div className="overflow-x-auto rounded-lg border bg-white" data-testid="coverage-history">
        <table className="w-full text-[12px]">
          <thead className="bg-gray-50 text-left text-gray-500">
            <tr>
              <th className="px-3 py-2">Scan time</th>
              <th className="px-3 py-2">Scope</th>
              <th className="px-3 py-2">{headlineLabel}</th>
              <th className="px-3 py-2">Resources</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {runsQ.isLoading ? (
              <tr><td colSpan={5} className="px-3 py-4 text-center text-gray-400">Loading history…</td></tr>
            ) : runs.length === 0 ? (
              <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-400">No saved scans yet — click <b>↻ Refresh now</b> to record one.</td></tr>
            ) : (
              runs.map((r: CoverageRunSummary) => (
                <tr key={r.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2 text-gray-700">{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</td>
                  <td className="px-3 py-2 text-gray-500">{r.scope_name || r.scope_id}</td>
                  <td className={`px-3 py-2 font-semibold ${tone(r.headline)}`}>{r.headline != null ? `${r.headline}%` : "—"}</td>
                  <td className="px-3 py-2 text-gray-600">{r.resource_count}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => view(r.id)} disabled={busy === `view:${r.id}`} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">View</button>
                    <button onClick={() => del(r.id)} disabled={busy === `del:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete</button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {showTrash && (
        <div className="mt-3 rounded-lg border bg-white">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <div className="flex items-center gap-2">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">🗑 Trash</h3>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">{trashed.length}</span>
            </div>
            {trashed.length > 0 && (
              <button onClick={() => void emptyTrash()} disabled={busy === "empty"} className="rounded-md border border-red-200 px-2.5 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Empty trash</button>
            )}
          </div>
          {trashQ.isLoading ? (
            <div className="px-3 py-4 text-center text-sm text-gray-400">Loading…</div>
          ) : trashed.length === 0 ? (
            <div className="px-3 py-6 text-center text-sm text-gray-400">Trash is empty.</div>
          ) : (
            <table className="w-full text-[12px]">
              <thead className="bg-gray-50 text-left text-gray-500">
                <tr>
                  <th className="px-3 py-2">Scan time</th>
                  <th className="px-3 py-2">{headlineLabel}</th>
                  <th className="px-3 py-2">Deleted</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {trashed.map((r: CoverageRunSummary) => (
                  <tr key={r.id} className="border-t hover:bg-gray-50">
                    <td className="px-3 py-2 text-gray-700">{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</td>
                    <td className={`px-3 py-2 font-semibold ${tone(r.headline)}`}>{r.headline != null ? `${r.headline}%` : "—"}</td>
                    <td className="px-3 py-2 text-gray-400">{r.deleted_at ? fmtTime(r.deleted_at) : "—"}</td>
                    <td className="px-3 py-2 text-right">
                      <button onClick={() => void restore(r.id)} disabled={busy === `restore:${r.id}`} className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50">↩ Restore</button>
                      <button onClick={() => void purge(r.id)} disabled={busy === `purge:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
