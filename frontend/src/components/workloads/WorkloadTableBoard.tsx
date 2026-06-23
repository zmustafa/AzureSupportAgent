// Power-grid (table) and board (kanban-by-environment) layouts for the workload fleet — the
// two alternate views beside the rich cards. Both are driven by the cache-only profiles.
import type { Workload, WorkloadProfile } from "../../api";
import { ScoreBadge, ClassPills, bandColor, categoryColor } from "./viz";

function pct(v: number | null): string {
  return v == null ? "—" : `${Math.round(v)}%`;
}

const SIGNAL_COLS: { key: keyof WorkloadProfile["health"]; label: string }[] = [
  { key: "monitoring", label: "Mon" },
  { key: "telemetry", label: "Tel" },
  { key: "backupdr", label: "Bkp" },
  { key: "performance", label: "Perf" },
  { key: "ownership", label: "Own" },
];

export function WorkloadTable({
  workloads,
  profileById,
  onOpen,
}: {
  workloads: Workload[];
  profileById: Record<string, WorkloadProfile>;
  onOpen: (id: string) => void;
}) {
  // Default: worst score first (triage).
  const rows = [...workloads].sort((a, b) => {
    const pa = profileById[a.id]?.health.score ?? -1;
    const pb = profileById[b.id]?.health.score ?? -1;
    return pa - pb;
  });
  return (
    <div className="overflow-x-auto rounded-xl border bg-white">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-400">
          <tr>
            <th className="px-3 py-2 font-medium">Workload</th>
            <th className="px-2 py-2 font-medium">Env</th>
            <th className="px-2 py-2 text-right font-medium">Score</th>
            {SIGNAL_COLS.map((s) => <th key={s.label} className="px-2 py-2 text-right font-medium">{s.label}</th>)}
            <th className="px-2 py-2 text-right font-medium">Resources</th>
            <th className="px-2 py-2 text-right font-medium">Risk</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((w) => {
            const p = profileById[w.id];
            const h = p?.health;
            return (
              <tr key={w.id} className="cursor-pointer hover:bg-gray-50" onClick={() => onOpen(w.id)}>
                <td className="px-3 py-2">
                  <div className="font-medium text-gray-800">{w.name}</div>
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {(p?.composition.by_category ?? []).slice(0, 4).map((c) => (
                      <span key={c.category} className="inline-flex items-center gap-1 text-[10px] text-gray-500">
                        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: categoryColor(c.category) }} />
                        {c.count}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-2 py-2">{p && <ClassPills c={p.classification} />}</td>
                <td className="px-2 py-2 text-right">
                  <span className="inline-flex items-center justify-end">
                    <ScoreBadge score={h?.score ?? null} band={h?.band ?? "unknown"} size="sm" />
                  </span>
                </td>
                {SIGNAL_COLS.map((s) => {
                  const v = h?.[s.key] as number | null | undefined;
                  return (
                    <td key={s.label} className="px-2 py-2 text-right tabular-nums" style={{ color: v == null ? "#cbd5e1" : bandColor(v >= 80 ? "good" : v >= 50 ? "warn" : "poor") }}>
                      {pct(v ?? null)}
                    </td>
                  );
                })}
                <td className="px-2 py-2 text-right tabular-nums text-gray-600">{p?.composition.total ?? w.summary?.total_resources ?? 0}</td>
                <td className="px-2 py-2 text-right">
                  {p?.risk.retirements_90d ? <span className="rounded bg-amber-50 px-1 text-[10px] text-amber-700">⚠{p.risk.retirements_90d}</span> : null}
                  {p?.risk.criticals ? <span className="ml-1 rounded bg-red-50 px-1 text-[10px] text-red-700">🔴{p.risk.criticals}</span> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const BOARD_ENVS = ["production", "staging", "development", "test", "dr", "shared", "unknown"];

export function WorkloadBoard({
  workloads,
  profileById,
  onOpen,
}: {
  workloads: Workload[];
  profileById: Record<string, WorkloadProfile>;
  onOpen: (id: string) => void;
}) {
  const byEnv: Record<string, Workload[]> = {};
  for (const w of workloads) {
    const env = profileById[w.id]?.classification.environment || w.environment || "unknown";
    (byEnv[env] ||= []).push(w);
  }
  const cols = BOARD_ENVS.filter((e) => byEnv[e]?.length);
  return (
    <div className="flex gap-3 overflow-x-auto pb-2">
      {cols.map((env) => (
        <div key={env} className="w-64 shrink-0 rounded-xl border bg-gray-50/60 p-2">
          <div className="mb-2 flex items-center justify-between px-1">
            <span className="text-xs font-semibold capitalize text-gray-600">{env}</span>
            <span className="text-[10px] text-gray-400">{byEnv[env].length}</span>
          </div>
          <div className="space-y-2">
            {byEnv[env].map((w) => {
              const p = profileById[w.id];
              return (
                <button key={w.id} onClick={() => onOpen(w.id)} className="block w-full rounded-lg border bg-white p-2.5 text-left transition hover:border-brand/40 hover:shadow-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium text-gray-800">{w.name}</span>
                    <ScoreBadge score={p?.health.score ?? null} band={p?.health.band ?? "unknown"} size="sm" />
                  </div>
                  <div className="mt-1 flex items-center justify-between">
                    <span className="text-[11px] text-gray-500">{p?.composition.total ?? 0} resources</span>
                    {p && <ClassPills c={{ ...p.classification, environment: "" }} />}
                  </div>
                  {(p?.risk.retirements_90d || p?.risk.criticals) ? (
                    <div className="mt-1 flex gap-1">
                      {p?.risk.retirements_90d ? <span className="rounded bg-amber-50 px-1 text-[10px] text-amber-700">⚠{p.risk.retirements_90d}</span> : null}
                      {p?.risk.criticals ? <span className="rounded bg-red-50 px-1 text-[10px] text-red-700">🔴{p.risk.criticals}</span> : null}
                    </div>
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
