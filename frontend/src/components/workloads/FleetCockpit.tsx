// Fleet cockpit — the aggregate strip above the workload list. Summarizes the whole fleet
// from the cache-only profiles: health distribution, composition treemap, environment ×
// criticality matrix, and a risk ticker. Clicking a treemap tile or matrix cell filters the
// list. No cost (cost data is unreliable in this tenant — intentionally omitted).
import type { WorkloadProfile } from "../../api";
import { Treemap, categoryColor, bandColor } from "./viz";

const ENVS = ["production", "staging", "development", "test", "dr", "shared", "unknown"] as const;
const CRITS = ["critical", "high", "medium", "low"] as const;

export function FleetCockpit({
  profiles,
  onFilter,
  activeFilter,
}: {
  profiles: WorkloadProfile[];
  onFilter: (f: string) => void;
  activeFilter: string;
}) {
  if (profiles.length === 0) return null;

  // Health distribution.
  const bands = { good: 0, warn: 0, poor: 0, unknown: 0 };
  for (const p of profiles) bands[p.health.band] = (bands[p.health.band] ?? 0) + 1;
  const analyzed = profiles.filter((p) => p.analyzed).length;
  const scores = profiles.filter((p) => p.health.score != null).map((p) => p.health.score as number);
  const avg = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;

  // Composition treemap (fleet-wide by category).
  const catTotals: Record<string, number> = {};
  for (const p of profiles) for (const c of p.composition.by_category) catTotals[c.category] = (catTotals[c.category] ?? 0) + c.count;
  const treeData = Object.entries(catTotals)
    .map(([label, count]) => ({ label, count, color: categoryColor(label) }))
    .sort((a, b) => b.count - a.count);
  const totalResources = profiles.reduce((a, p) => a + p.composition.total, 0);

  // Environment × criticality matrix.
  const matrix: Record<string, Record<string, number>> = {};
  for (const e of ENVS) matrix[e] = Object.fromEntries(CRITS.map((c) => [c, 0]));
  for (const p of profiles) {
    const e = ENVS.includes(p.classification.environment as typeof ENVS[number]) ? p.classification.environment : "unknown";
    const c = CRITS.includes(p.classification.criticality as typeof CRITS[number]) ? p.classification.criticality : "low";
    matrix[e][c] = (matrix[e][c] ?? 0) + 1;
  }
  const presentEnvs = ENVS.filter((e) => CRITS.some((c) => matrix[e][c] > 0));

  // Risk ticker.
  const retiring = profiles.reduce((a, p) => a + (p.risk.retirements_90d ?? 0), 0);
  const criticals = profiles.reduce((a, p) => a + (p.risk.criticals ?? 0), 0);
  const unowned = profiles.filter((p) => p.health.ownership != null && p.health.ownership < 50).length;
  const notAnalyzed = profiles.length - analyzed;

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
      {/* Health distribution */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Fleet health</div>
        <div className="flex items-center gap-3">
          <div className="text-3xl font-bold tabular-nums" style={{ color: avg == null ? "#94a3b8" : bandColor(avg >= 80 ? "good" : avg >= 50 ? "warn" : "poor") }}>
            {avg ?? "—"}
          </div>
          <div className="flex-1 space-y-1">
            {(["good", "warn", "poor", "unknown"] as const).map((b) => (
              <button key={b} onClick={() => onFilter(activeFilter === `band:${b}` ? "" : `band:${b}`)} className="flex w-full items-center gap-2 text-left">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: bandColor(b) }} />
                <span className="text-[11px] capitalize text-gray-500">{b === "unknown" ? "not analyzed" : b}</span>
                <span className="ml-auto text-[11px] tabular-nums font-medium text-gray-700">{bands[b]}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="mt-2 text-[10px] text-gray-400">{analyzed}/{profiles.length} analyzed</div>
      </div>

      {/* Composition treemap */}
      <div className="rounded-xl border bg-white p-4 lg:col-span-2">
        <div className="mb-2 flex items-center justify-between">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Estate composition</div>
          <div className="text-[11px] text-gray-500"><b className="text-gray-700">{totalResources.toLocaleString()}</b> resources</div>
        </div>
        <Treemap data={treeData} width={460} height={96} onClick={(label) => onFilter(activeFilter === `cat:${label}` ? "" : `cat:${label}`)} />
      </div>

      {/* Env × criticality matrix + risk ticker */}
      <div className="rounded-xl border bg-white p-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Triage</div>
        <div className="overflow-x-auto">
          <table className="w-full text-[10px]">
            <thead>
              <tr className="text-gray-400">
                <th className="text-left font-normal"></th>
                {CRITS.map((c) => <th key={c} className="px-1 font-normal capitalize">{c.slice(0, 4)}</th>)}
              </tr>
            </thead>
            <tbody>
              {presentEnvs.map((e) => (
                <tr key={e}>
                  <td className="pr-1 capitalize text-gray-500">{e.slice(0, 4)}</td>
                  {CRITS.map((c) => {
                    const n = matrix[e][c];
                    const on = n > 0;
                    return (
                      <td key={c} className="p-0.5">
                        <button
                          disabled={!on}
                          onClick={() => onFilter(activeFilter === `env:${e}` ? "" : `env:${e}`)}
                          className={`flex h-6 w-full items-center justify-center rounded tabular-nums ${on ? "font-semibold text-white" : "text-gray-300"}`}
                          style={{ backgroundColor: on ? (c === "critical" ? "#dc2626" : c === "high" ? "#ea580c" : c === "medium" ? "#d97706" : "#94a3b8") : "#f8fafc" }}
                        >
                          {on ? n : ""}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          {retiring > 0 && <button onClick={() => onFilter(activeFilter === "risk:retiring" ? "" : "risk:retiring")} className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">⚠ {retiring} retiring</button>}
          {criticals > 0 && <button onClick={() => onFilter(activeFilter === "risk:critical" ? "" : "risk:critical")} className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700">🔴 {criticals} critical</button>}
          {unowned > 0 && <button onClick={() => onFilter(activeFilter === "risk:unowned" ? "" : "risk:unowned")} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">🪪 {unowned} under-owned</button>}
          {notAnalyzed > 0 && <button onClick={() => onFilter(activeFilter === "band:unknown" ? "" : "band:unknown")} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">○ {notAnalyzed} not analyzed</button>}
        </div>
      </div>
    </div>
  );
}

// Apply a cockpit filter token to a workload's profile.
export function matchesFleetFilter(p: WorkloadProfile | undefined, filter: string): boolean {
  if (!filter || !p) return true;
  const [kind, val] = filter.split(":");
  switch (kind) {
    case "band": return p.health.band === val;
    case "cat": return p.composition.by_category.some((c) => c.category === val);
    case "env": return p.classification.environment === val;
    case "risk":
      if (val === "retiring") return (p.risk.retirements_90d ?? 0) > 0;
      if (val === "critical") return (p.risk.criticals ?? 0) > 0;
      if (val === "unowned") return p.health.ownership != null && p.health.ownership < 50;
      return true;
    default: return true;
  }
}
