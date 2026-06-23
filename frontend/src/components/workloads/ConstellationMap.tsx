// Constellation estate map — a single-screen bubble view of the whole fleet. Each workload is
// a bubble sized by resource count, colored by health band, clustered into environment columns.
// Hover for details; click to open the workload. A beautiful at-a-glance estate overview.
import { useMemo, useState } from "react";
import type { Workload, WorkloadProfile } from "../../api";
import { bandColor } from "./viz";

const ENV_ORDER = ["production", "staging", "development", "test", "dr", "shared", "unknown"];
const ENV_LABEL: Record<string, string> = {
  production: "Production", staging: "Staging", development: "Development",
  test: "Test", dr: "DR", shared: "Shared", unknown: "Unclassified",
};

export function ConstellationMap({
  workloads,
  profileById,
  onOpen,
}: {
  workloads: Workload[];
  profileById: Record<string, WorkloadProfile>;
  onOpen: (id: string) => void;
}) {
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null);

  // Group workloads into environment columns; scale bubble radius by resource count.
  const cols = useMemo(() => {
    const byEnv: Record<string, Workload[]> = {};
    for (const w of workloads) {
      const env = profileById[w.id]?.classification.environment || w.environment || "unknown";
      (byEnv[env] ||= []).push(w);
    }
    return ENV_ORDER.filter((e) => byEnv[e]?.length).map((env) => ({ env, items: byEnv[env] }));
  }, [workloads, profileById]);

  const maxResources = Math.max(1, ...workloads.map((w) => profileById[w.id]?.composition.total ?? w.summary?.total_resources ?? 0));
  const radius = (n: number) => 14 + Math.sqrt(n / maxResources) * 34; // 14–48px

  if (workloads.length === 0) return null;

  return (
    <div className="relative rounded-xl border bg-gradient-to-b from-slate-50 to-white p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Estate constellation</div>
        <div className="flex items-center gap-3 text-[10px] text-gray-500">
          {(["good", "warn", "poor", "unknown"] as const).map((b) => (
            <span key={b} className="inline-flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: bandColor(b) }} />
              {b === "unknown" ? "not analyzed" : b}
            </span>
          ))}
          <span className="text-gray-400">· bubble size = resources</span>
        </div>
      </div>
      <div className="flex gap-4 overflow-x-auto pb-2">
        {cols.map((col) => (
          <div key={col.env} className="flex min-w-[150px] flex-1 flex-col items-center">
            <div className="mb-2 text-[11px] font-medium text-gray-500">{ENV_LABEL[col.env] ?? col.env} <span className="text-gray-300">·</span> {col.items.length}</div>
            <div className="flex flex-wrap items-center justify-center gap-2">
              {col.items
                .slice()
                .sort((a, b) => (profileById[b.id]?.composition.total ?? 0) - (profileById[a.id]?.composition.total ?? 0))
                .map((w) => {
                  const p = profileById[w.id];
                  const n = p?.composition.total ?? w.summary?.total_resources ?? 0;
                  const r = radius(n);
                  const band = p?.health.band ?? "unknown";
                  const color = bandColor(band);
                  return (
                    <button
                      key={w.id}
                      onClick={() => onOpen(w.id)}
                      onMouseEnter={(e) => setHover({ id: w.id, x: e.clientX, y: e.clientY })}
                      onMouseMove={(e) => setHover({ id: w.id, x: e.clientX, y: e.clientY })}
                      onMouseLeave={() => setHover(null)}
                      className="relative flex items-center justify-center rounded-full font-semibold text-white shadow-sm transition hover:scale-105 hover:shadow"
                      style={{ width: r, height: r, backgroundColor: color, fontSize: Math.max(9, r / 4) }}
                      title={`${w.name} · ${n} resources${p?.health.score != null ? ` · score ${p.health.score}` : ""}`}
                    >
                      {p?.health.score != null ? p.health.score : ""}
                    </button>
                  );
                })}
            </div>
          </div>
        ))}
      </div>
      {hover && (() => {
        const w = workloads.find((x) => x.id === hover.id);
        const p = profileById[hover.id];
        if (!w) return null;
        return (
          <div className="pointer-events-none fixed z-50 rounded-lg border bg-white px-3 py-2 text-xs shadow-lg" style={{ left: hover.x + 12, top: hover.y + 12 }}>
            <div className="font-semibold text-gray-800">{w.name}</div>
            <div className="mt-0.5 text-gray-500">{p?.composition.total ?? 0} resources · {p?.classification.criticality ?? "—"}</div>
            {p?.health.score != null && <div className="text-gray-500">Health {p.health.score} · {p.health.band}</div>}
            {(p?.risk.retirements_90d || p?.risk.criticals) ? (
              <div className="mt-0.5 text-amber-600">{p?.risk.retirements_90d ? `⚠ ${p.risk.retirements_90d} retiring ` : ""}{p?.risk.criticals ? `🔴 ${p.risk.criticals}` : ""}</div>
            ) : null}
          </div>
        );
      })()}
    </div>
  );
}
