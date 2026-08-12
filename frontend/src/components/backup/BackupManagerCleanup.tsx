// Backup Manager — Cleanup.
//
// Two things accumulate here and they need different treatment:
//
//  * **Analysis history** — a small headline record saved every time an analysis finishes.
//    Handled by the shared RunCleanup tab (soft-delete → restore → purge) exactly as the
//    coverage screens do.
//  * **Stored analyses** — the heavy per-scope documents every tab reads. The store keeps a
//    bounded number of scopes and silently evicts the oldest, so an operator who can see what
//    is held (and drop dead scopes) decides what survives instead of discovering the eviction
//    as an empty tab.
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BackupManagerStoredSnapshot } from "../../api";
import { queryKeys } from "../../queryKeys";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { RunCleanup } from "../cleanup/RunCleanup";

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
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function ageDays(iso: string): number {
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? 0 : (Date.now() - t) / 86400_000;
}

function StoredSnapshots({ canPurge }: { canPurge: boolean }) {
  const qc = useQueryClient();
  const storeQ = useQuery({
    queryKey: queryKeys.backupManager.snapshotStore,
    queryFn: api.backupManagerSnapshotStore,
    refetchOnWindowFocus: false,
  });
  const rows = useMemo(() => storeQ.data?.snapshots ?? [], [storeQ.data]);
  const stats = storeQ.data?.stats;
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [confirm, setConfirm] = useState(false);

  const toggle = (key: string) =>
    setSelected((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const selectMany = (list: BackupManagerStoredSnapshot[]) => setSelected(new Set(list.map((r) => r.key)));

  const selectedRows = rows.filter((r) => selected.has(r.key));
  const selectedBytes = selectedRows.reduce((a, r) => a + r.size_bytes, 0);
  const orphans = rows.filter((r) => r.orphan);
  const older30 = rows.filter((r) => ageDays(r.generated_at) > 30);

  async function purge() {
    setBusy(true);
    setConfirm(false);
    try {
      const result = await api.backupManagerPurgeSnapshots([...selected]);
      setMsg({ text: `Purged ${result.count} stored analysis${result.count === 1 ? "" : "es"} · ${fmtBytes(result.freed_bytes)} freed.`, ok: true });
      setSelected(new Set());
      await Promise.all([
        qc.invalidateQueries({ queryKey: queryKeys.backupManager.snapshotStore }),
        qc.invalidateQueries({ queryKey: queryKeys.backupManager.fleet }),
        qc.invalidateQueries({ queryKey: queryKeys.backupManager.snapshotRoot }),
      ]);
    } catch (error) {
      setMsg({ text: formatError(error), ok: false });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border bg-white">
      <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-gray-900">Stored analyses</h3>
          <p className="text-[11px] text-gray-500">
            The full documents every tab reads. The store holds {stats?.max_scopes ?? "a bounded number of"} scopes
            and evicts the oldest when it is full — purge what you no longer need to decide what stays.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-[11px]">
          <span className="text-gray-500">
            {stats?.count ?? 0} stored · {fmtBytes(stats?.total_bytes ?? 0)}
            {stats?.orphans ? ` · ${stats.orphans} orphaned (${fmtBytes(stats.orphan_bytes)})` : ""}
          </span>
          {orphans.length > 0 && (
            <button onClick={() => selectMany(orphans)} className="rounded border px-2 py-1 hover:bg-gray-50">
              Select orphaned ({orphans.length})
            </button>
          )}
          {older30.length > 0 && (
            <button onClick={() => selectMany(older30)} className="rounded border px-2 py-1 hover:bg-gray-50">
              Older than 30 days ({older30.length})
            </button>
          )}
        </div>
      </div>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b bg-amber-50/60 px-4 py-2 text-xs">
          <span className="font-medium text-gray-700">{selected.size} selected · {fmtBytes(selectedBytes)}</span>
          <button onClick={() => setSelected(new Set())} className="text-gray-500 hover:underline">Clear</button>
          <button onClick={() => setConfirm(true)} disabled={busy || !canPurge}
            title={canPurge ? "" : "Requires the Backup Manager approve permission"}
            className="ml-auto rounded-md bg-red-600 px-3 py-1 font-medium text-white disabled:opacity-50">
            ⨯ Purge selected
          </button>
        </div>
      )}
      {confirm && (
        <div className="border-b bg-red-50 px-4 py-2 text-xs text-red-800">
          Purge {selected.size} stored analysis{selected.size === 1 ? "" : "es"}? The scopes have to be analyzed
          again to come back. Nothing in Azure changes.
          <button onClick={purge} className="ml-2 rounded bg-red-600 px-2 py-0.5 font-medium text-white">Yes, purge</button>
          <button onClick={() => setConfirm(false)} className="ml-2 text-red-700 hover:underline">Cancel</button>
        </div>
      )}
      {msg && <div className={`border-b px-4 py-2 text-xs ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</div>}

      <div className="max-h-80 overflow-auto">
        {storeQ.isLoading ? <div className="p-4"><Skeleton rows={4} /></div>
          : storeQ.isError ? <div className="px-4 py-3 text-sm text-red-700">{formatError(storeQ.error)}</div>
            : !rows.length ? <div className="px-4 py-8 text-center text-sm text-gray-500">Nothing stored yet — analyze a scope first.</div>
              : (
                <table className="w-full text-[12px]">
                  <thead className="sticky top-0 bg-gray-50 text-left text-gray-500">
                    <tr className="border-b">
                      <th className="w-8 px-3 py-2" />
                      <th className="px-2 py-2 font-medium">Scope</th>
                      <th className="px-2 py-2 font-medium">Analyzed</th>
                      <th className="px-2 py-2 font-medium">Items</th>
                      <th className="px-2 py-2 font-medium">Gaps</th>
                      <th className="px-2 py-2 font-medium">Size</th>
                      <th className="px-2 py-2 font-medium">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.key} className={`border-b hover:bg-gray-50 ${selected.has(row.key) ? "bg-brand/5" : ""}`}>
                        <td className="px-3 py-1.5">
                          <input type="checkbox" checked={selected.has(row.key)} onChange={() => toggle(row.key)}
                            aria-label={`Select ${row.scope_name}`} />
                        </td>
                        <td className="px-2 py-1.5">
                          <span className="font-medium text-gray-800">{row.scope_name}</span>
                          <span className="ml-1 text-[10px] text-gray-400">{row.scope_kind}</span>
                          {row.scope_kind === "management_group" && (
                            <span className="ml-1 text-[10px] text-gray-400">· {row.subscription_count} subscriptions</span>
                          )}
                        </td>
                        <td className="px-2 py-1.5 text-gray-500" title={row.generated_at}>{relTime(row.generated_at)}</td>
                        <td className="px-2 py-1.5 tabular-nums text-gray-600">{row.protected_items}</td>
                        <td className={`px-2 py-1.5 tabular-nums ${row.gaps ? "text-red-600" : "text-gray-400"}`}>{row.gaps}</td>
                        <td className="px-2 py-1.5 tabular-nums text-gray-600">{fmtBytes(row.size_bytes)}</td>
                        <td className="px-2 py-1.5">
                          {row.orphan
                            ? <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700" title={row.orphan_reasons.join(", ")}>orphaned</span>
                            : row.partial ? <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">partial</span>
                              : row.demo ? <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo</span>
                                : <span className="text-[10px] text-gray-400">ok</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
      </div>
    </section>
  );
}

export function BackupManagerCleanup({ canPurge }: { canPurge: boolean }) {
  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
      <StoredSnapshots canPurge={canPurge} />
      <section className="rounded-xl border bg-white">
        <RunCleanup
          prefix="/backup-manager"
          queryKey={queryKeys.backupManager.cleanup}
          invalidateKeys={[[...queryKeys.backupManager.fleet], [...queryKeys.backupManager.snapshotStore]]}
          isEmptyRun={(run) => (run.resource_count ?? 0) === 0}
          renderMeta={(run) => (
            <span className="text-gray-600">
              {run.scope_name}
              {typeof run.headline === "number" && (
                <span className="ml-1 text-gray-400">· {run.headline}% protected</span>
              )}
            </span>
          )}
        />
      </section>
    </div>
  );
}
