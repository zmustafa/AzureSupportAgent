// Workload Overlaps — a dedicated report of resources that belong to MORE THAN ONE workload.
//
// Tier 1 (instant): resources explicitly listed in 2+ workloads. Tier 2 ("Deep scan"): also
// resources pulled into a workload via its whole RG/subscription. Lets the user SEE the
// duplicates (three groupings: by resource / by workload pair / by type), export CSV, and
// de-dupe explicit memberships in one click ("keep in one, remove from the others").
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type Workload,
  type WorkloadOverlaps,
  type WorkloadOverlapRow,
  type TenantOption,
} from "../../api";
import { formatError } from "../../utils/format";
import { Skeleton } from "../../utils/perf";
import { AzureIcon, friendlyResourceType } from "../AzureIcon";

type Group = "resource" | "pair" | "type";

function viaLabel(via: string): string {
  return via === "explicit" ? "" : via === "resource_group" ? "via RG" : via === "subscription" ? "via Sub" : "via MG";
}

export function WorkloadOverlapsView() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  // Connection scope: default to the configured DEFAULT connection; "" = all connections.
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections, retry: false });
  const conns: TenantOption[] = connQ.data?.connections ?? [];
  const defaultConn = conns.find((c) => c.is_default)?.id ?? "";
  // null = not yet initialised (fall back to default once connections load).
  const [connId, setConnId] = useState<string | null>(null);
  const effConn = connId ?? defaultConn;

  const [deep, setDeep] = useState(false);
  const [group, setGroup] = useState<Group>("resource");
  const [search, setSearch] = useState("");
  const [pairFilter, setPairFilter] = useState<{ a: string; b: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const ovQ = useQuery<WorkloadOverlaps>({
    queryKey: ["workloadOverlaps", effConn, deep],
    queryFn: () => api.workloadOverlaps(effConn, deep),
    retry: false,
  });
  // Full workloads (with nodes) — needed for the de-dupe remove action.
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloadById = useMemo(() => {
    const m = new Map<string, Workload>();
    for (const w of wlQ.data?.workloads ?? []) m.set(w.id, w);
    return m;
  }, [wlQ.data]);

  const data = ovQ.data;
  const overlaps = data?.overlaps ?? [];

  const filtered = useMemo(() => {
    let rows = overlaps;
    const q = search.trim().toLowerCase();
    if (q) {
      rows = rows.filter(
        (r) =>
          r.name.toLowerCase().includes(q) ||
          r.friendly_type.toLowerCase().includes(q) ||
          r.resource_group.toLowerCase().includes(q) ||
          r.workloads.some((w) => w.name.toLowerCase().includes(q)),
      );
    }
    if (pairFilter) {
      rows = rows.filter((r) => {
        const ids = new Set(r.workloads.map((w) => w.id));
        return ids.has(pairFilter.a) && ids.has(pairFilter.b);
      });
    }
    return rows;
  }, [overlaps, search, pairFilter]);

  async function removeFromOthers(row: WorkloadOverlapRow, keepId: string) {
    const others = row.workloads.filter((w) => w.id !== keepId && w.via === "explicit");
    if (others.length === 0) return;
    const keepName = row.workloads.find((w) => w.id === keepId)?.name ?? "the chosen workload";
    if (!window.confirm(`Remove “${row.name}” from ${others.length} workload${others.length === 1 ? "" : "s"}, keeping it only in “${keepName}”?`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const ridLc = row.id.toLowerCase();
      for (const o of others) {
        const wl = workloadById.get(o.id);
        if (!wl) continue;
        const nodes = wl.nodes.filter((n) => !(n.kind === "resource" && (n.id ?? "").toLowerCase() === ridLc));
        await api.upsertWorkload({ ...wl, nodes });
      }
      setMsg({ text: `Removed “${row.name}” from ${others.length} workload${others.length === 1 ? "" : "s"} (kept in “${keepName}”).`, ok: true });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["workloads"] }),
        qc.invalidateQueries({ queryKey: ["workloadProfiles"] }),
        qc.invalidateQueries({ queryKey: ["workloadOverlaps"] }),
      ]);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    const head = ["resource", "type", "resource_group", "subscription", "workload_count", "workloads"];
    const lines = [head.join(",")];
    for (const r of filtered) {
      const wls = r.workloads.map((w) => `${w.name}${w.via !== "explicit" ? ` (${w.via})` : ""}`).join(" | ");
      const cells = [r.name, r.friendly_type, r.resource_group, r.subscription_id, String(r.count), wls];
      lines.push(cells.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "workload-overlaps.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  const s = data?.summary;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="border-b bg-white px-6 py-4">
        <button onClick={() => navigate("/workloads")} className="text-xs text-gray-400 hover:text-gray-600">← Workloads</button>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">🧩 Overlapping resources</h1>
            <p className="mt-0.5 max-w-2xl text-sm text-gray-500">
              Resources that belong to more than one workload. Duplicates can double-count cost, skew
              health scores and cause conflicting actions. Review and keep each resource in a single
              workload.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={effConn}
              onChange={(e) => setConnId(e.target.value)}
              className="rounded-lg border px-2.5 py-1.5 text-xs text-gray-700"
              title="Scope to one Azure connection, or all"
            >
              <option value="">All connections</option>
              {conns.map((c) => (
                <option key={c.id} value={c.id}>{c.display_name}{c.is_default ? " (default)" : ""}</option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs text-gray-600" title="Also detect resources pulled into a workload via its whole resource group / subscription (queries Azure)">
              <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
              Deep scan
            </label>
            <button onClick={() => void ovQ.refetch()} disabled={ovQ.isFetching} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
              {ovQ.isFetching ? "Scanning…" : "↻ Refresh"}
            </button>
            <button onClick={exportCsv} disabled={filtered.length === 0} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">⬇ CSV</button>
          </div>
        </div>

        {/* KPIs */}
        {s && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Kpi label="Duplicated resources" value={s.duplicated_resources} tone={s.duplicated_resources ? "text-amber-600" : "text-green-600"} />
            <Kpi label="Workloads involved" value={s.workloads_involved} />
            <Kpi label="Removable memberships" value={s.total_extra_memberships} tone={s.total_extra_memberships ? "text-amber-600" : undefined} />
            <Kpi label="Distinct types" value={s.by_type.length} />
          </div>
        )}
        {data?.truncated && (
          <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-800">
            ⚠ Deep scan hit the 5,000-resource cap — some scope-implied overlaps may be missing. Narrow by connection.
          </div>
        )}
        {msg && (
          <div className={`mt-2 rounded-lg border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto bg-gray-50 p-6">
        {ovQ.isLoading ? (
          <Skeleton rows={8} />
        ) : ovQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{formatError(ovQ.error)}</div>
        ) : overlaps.length === 0 ? (
          <div className="rounded-xl border border-dashed bg-white p-10 text-center">
            <div className="text-3xl">✅</div>
            <p className="mt-2 text-sm font-medium text-gray-700">No duplicate resources.</p>
            <p className="mt-1 text-xs text-gray-500">Every resource belongs to at most one workload{deep ? "" : " (explicitly)"}.{!deep && " Try a Deep scan to also catch scope-implied overlaps."}</p>
          </div>
        ) : (
          <div className="space-y-3">
            {/* Toolbar: grouping + search + active pair filter */}
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex items-center rounded-lg border bg-white p-0.5 text-xs">
                {(["resource", "pair", "type"] as const).map((g) => (
                  <button key={g} onClick={() => setGroup(g)} className={`rounded-md px-2.5 py-1 capitalize ${group === g ? "bg-brand/10 font-medium text-brand" : "text-gray-500"}`}>
                    {g === "resource" ? "By resource" : g === "pair" ? "By workload pair" : "By type"}
                  </button>
                ))}
              </div>
              {group === "resource" && (
                <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter resources / workloads…" className="w-56 rounded-lg border px-2.5 py-1 text-xs" />
              )}
              {pairFilter && (
                <button onClick={() => setPairFilter(null)} className="inline-flex items-center gap-1 rounded-full bg-brand/10 px-2.5 py-1 text-xs font-medium text-brand">
                  Pair filter ✕
                </button>
              )}
              <span className="ml-auto text-xs text-gray-400">{filtered.length} of {overlaps.length} duplicated</span>
            </div>

            {group === "resource" && (
              <ResourceTable rows={filtered} busy={busy} onOpenWorkload={(id) => navigate(`/workloads/${id}`)} onRemoveOthers={removeFromOthers} />
            )}
            {group === "pair" && (
              <PairList pairs={data?.by_pair ?? []} onPick={(a, b) => { setPairFilter({ a, b }); setGroup("resource"); }} />
            )}
            {group === "type" && (
              <TypeList rows={overlaps} byType={s?.by_type ?? []} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

function WorkloadChip({ name, via, onClick }: { name: string; via: string; onClick: () => void }) {
  const lbl = viaLabel(via);
  return (
    <button onClick={onClick} className="inline-flex items-center gap-1 rounded-full border bg-white px-2 py-0.5 text-[11px] text-gray-700 hover:border-brand/40 hover:text-brand" title={lbl ? `Member ${lbl}` : "Explicit member"}>
      {name}
      {lbl && <span className="rounded bg-amber-50 px-1 text-[9px] text-amber-600">{lbl}</span>}
    </button>
  );
}

function ResourceTable({
  rows,
  busy,
  onOpenWorkload,
  onRemoveOthers,
}: {
  rows: WorkloadOverlapRow[];
  busy: boolean;
  onOpenWorkload: (id: string) => void;
  onRemoveOthers: (row: WorkloadOverlapRow, keepId: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border bg-white">
      <table className="w-full text-[12px]">
        <thead className="bg-gray-50 text-left text-gray-500">
          <tr className="border-b">
            <th className="px-3 py-2 font-medium">Resource</th>
            <th className="px-3 py-2 font-medium">Type</th>
            <th className="px-3 py-2 font-medium">Resource group</th>
            <th className="px-3 py-2 font-medium">In workloads</th>
            <th className="px-3 py-2 font-medium">Keep in…</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b align-top hover:bg-gray-50">
              <td className="px-3 py-2">
                <div className="flex items-center gap-1.5">
                  <AzureIcon kind="resource" type={r.resource_type} className="h-4 w-4 text-gray-400" />
                  <span className="font-medium text-gray-800" title={r.id}>{r.name}</span>
                  <span className="rounded-full bg-amber-100 px-1.5 text-[10px] font-semibold text-amber-700">×{r.count}</span>
                </div>
                {r.subscription_id && <div className="mt-0.5 text-[10px] text-gray-400">sub {r.subscription_id}</div>}
              </td>
              <td className="px-3 py-2 text-gray-600">{r.friendly_type || friendlyResourceType(r.resource_type)}</td>
              <td className="px-3 py-2 text-gray-600">{r.resource_group || "—"}</td>
              <td className="px-3 py-2">
                <div className="flex flex-wrap gap-1">
                  {r.workloads.map((w) => (
                    <WorkloadChip key={w.id} name={w.name} via={w.via} onClick={() => onOpenWorkload(w.id)} />
                  ))}
                </div>
              </td>
              <td className="px-3 py-2">
                <KeepInControl row={r} busy={busy} onRemoveOthers={onRemoveOthers} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KeepInControl({
  row,
  busy,
  onRemoveOthers,
}: {
  row: WorkloadOverlapRow;
  busy: boolean;
  onRemoveOthers: (row: WorkloadOverlapRow, keepId: string) => void;
}) {
  const explicit = row.workloads.filter((w) => w.via === "explicit");
  const [keep, setKeep] = useState<string>(explicit[0]?.id ?? "");
  if (explicit.length < 2) {
    // Nothing to prune by node-removal (it's scope-implied — fix via the scope/excludes instead).
    return <span className="text-[11px] text-gray-400" title="This resource is pulled in by a whole RG/subscription — remove it by editing that scope or adding an exclude.">scope-implied</span>;
  }
  return (
    <div className="flex items-center gap-1.5">
      <select value={keep} onChange={(e) => setKeep(e.target.value)} className="rounded border px-1.5 py-1 text-[11px]">
        {explicit.map((w) => (
          <option key={w.id} value={w.id}>{w.name}</option>
        ))}
      </select>
      <button
        onClick={() => onRemoveOthers(row, keep)}
        disabled={busy || !keep}
        className="rounded-lg border border-red-200 px-2 py-1 text-[11px] text-red-600 hover:bg-red-50 disabled:opacity-50"
        title="Remove this resource from every OTHER workload"
      >
        Remove from {explicit.length - 1} other{explicit.length - 1 === 1 ? "" : "s"}
      </button>
    </div>
  );
}

function PairList({ pairs, onPick }: { pairs: WorkloadOverlaps["by_pair"]; onPick: (a: string, b: string) => void }) {
  if (pairs.length === 0) return <p className="rounded-lg border border-dashed bg-white p-6 text-center text-xs text-gray-400">No shared pairs.</p>;
  const max = pairs[0]?.shared_count || 1;
  return (
    <div className="space-y-1.5 rounded-xl border bg-white p-3">
      {pairs.map((p, i) => (
        <button key={i} onClick={() => onPick(p.a.id, p.b.id)} className="flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-gray-50">
          <span className="min-w-0 flex-1 truncate text-gray-700">
            <span className="font-medium">{p.a.name}</span> <span className="text-gray-400">↔</span> <span className="font-medium">{p.b.name}</span>
          </span>
          <span className="h-2 rounded bg-amber-300" style={{ width: `${Math.max(8, (p.shared_count / max) * 160)}px` }} />
          <span className="w-16 text-right tabular-nums text-xs text-gray-600">{p.shared_count} shared</span>
        </button>
      ))}
    </div>
  );
}

function TypeList({ rows, byType }: { rows: WorkloadOverlapRow[]; byType: { friendly_type: string; count: number }[] }) {
  const [open, setOpen] = useState<string>(byType[0]?.friendly_type ?? "");
  return (
    <div className="space-y-2">
      {byType.map((t) => {
        const isOpen = open === t.friendly_type;
        const items = rows.filter((r) => (r.friendly_type || friendlyResourceType(r.resource_type)) === t.friendly_type);
        return (
          <div key={t.friendly_type} className="overflow-hidden rounded-xl border bg-white">
            <button onClick={() => setOpen(isOpen ? "" : t.friendly_type)} className="flex w-full items-center justify-between px-4 py-2 text-sm hover:bg-gray-50">
              <span className="font-medium text-gray-800">{t.friendly_type || "Other"}</span>
              <span className="text-xs text-gray-500">{t.count} duplicated · {isOpen ? "▾" : "▸"}</span>
            </button>
            {isOpen && (
              <ul className="divide-y border-t">
                {items.map((r) => (
                  <li key={r.id} className="flex items-center gap-2 px-4 py-1.5 text-[12px]">
                    <AzureIcon kind="resource" type={r.resource_type} className="h-3.5 w-3.5 text-gray-400" />
                    <span className="font-medium text-gray-700">{r.name}</span>
                    <span className="text-gray-400">{r.resource_group}</span>
                    <span className="ml-auto rounded-full bg-amber-100 px-1.5 text-[10px] font-semibold text-amber-700">×{r.count}</span>
                    <span className="text-[11px] text-gray-500">{r.workloads.map((w) => w.name).join(", ")}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}
