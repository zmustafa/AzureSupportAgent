import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type CoverageFeature, type CoverageRunSummary } from "../api";
import { formatError } from "../utils/format";
import { RunHistoryShell } from "./RunHistoryShell";

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
    <RunHistoryShell<CoverageRunSummary>
      countText={`${runs.length} scan(s) for this scope`}
      message={msg}
      rows={runs}
      loading={runsQ.isLoading}
      emptyHint={<>No saved scans yet — click <b>↻ Refresh now</b> to record one.</>}
      testId="coverage-history"
      showTrash={showTrash}
      onToggleTrash={() => setShowTrash((v) => !v)}
      trashedCount={trashed.length}
      trashedRows={trashed}
      trashLoading={trashQ.isLoading}
      onEmptyTrash={() => void emptyTrash()}
      emptyingTrash={busy === "empty"}
      columns={[
        { header: "Scan time", className: "text-gray-700", render: (r) => <>{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</> },
        { header: "Scope", className: "text-gray-500", render: (r) => r.scope_name || r.scope_id },
        { header: headlineLabel, render: (r) => <span className={`font-semibold ${tone(r.headline)}`}>{r.headline != null ? `${r.headline}%` : "—"}</span> },
        { header: "Resources", className: "text-gray-600", render: (r) => r.resource_count },
        {
          header: "Actions", align: "right", render: (r) => (
            <>
              <button onClick={() => view(r.id)} disabled={busy === `view:${r.id}`} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">View</button>
              <button onClick={() => del(r.id)} disabled={busy === `del:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete</button>
            </>
          ),
        },
      ]}
      trashColumns={[
        { header: "Scan time", className: "text-gray-700", render: (r) => <>{fmtTime(r.run_at)}{r.demo ? " · demo" : ""}</> },
        { header: headlineLabel, render: (r) => <span className={`font-semibold ${tone(r.headline)}`}>{r.headline != null ? `${r.headline}%` : "—"}</span> },
        { header: "Deleted", className: "text-gray-400", render: (r) => (r.deleted_at ? fmtTime(r.deleted_at) : "—") },
        {
          header: "Actions", align: "right", render: (r) => (
            <>
              <button onClick={() => void restore(r.id)} disabled={busy === `restore:${r.id}`} className="rounded border border-brand/40 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50">↩ Restore</button>
              <button onClick={() => void purge(r.id)} disabled={busy === `purge:${r.id}`} className="ml-1 rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
            </>
          ),
        },
      ]}
    />
  );
}
