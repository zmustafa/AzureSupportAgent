// A rich fleet card for one workload: composition donut + health radar + composite score +
// risk chips + classification + quick actions. Driven by the cache-only WorkloadProfile so
// the whole fleet renders instantly. Falls back gracefully while the profile is loading or
// when a workload has never been analyzed.
import type { Workload, WorkloadProfile } from "../../api";
import { AzureIcon } from "../AzureIcon";
import {
  CompositionDonut,
  HealthRadar,
  ScoreBadge,
  ClassPills,
  categoryColor,
  Sparkline,
  bandColor,
} from "./viz";

function RiskChip({ icon, label, tone }: { icon: string; label: string; tone: "red" | "amber" | "gray" }) {
  const cls = tone === "red" ? "bg-red-50 text-red-700" : tone === "amber" ? "bg-amber-50 text-amber-700" : "bg-gray-100 text-gray-500";
  return <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>{icon} {label}</span>;
}

export function WorkloadCard({
  w,
  profile,
  selected,
  onToggleSelect,
  onOpen,
  onRefresh,
  onEdit,
  onDelete,
  onMission,
  onAssess,
  refreshing,
}: {
  w: Workload;
  profile?: WorkloadProfile;
  selected: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
  onRefresh: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onMission: () => void;
  onAssess: () => void;
  refreshing: boolean;
}) {
  const comp = profile?.composition;
  const health = profile?.health;
  const risk = profile?.risk;
  const topTypes = (comp?.by_type ?? []).slice(0, 5);

  return (
    <div className="group rounded-xl border bg-white p-4 shadow-sm transition hover:border-brand/40 hover:shadow">
      {/* Header */}
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          title="Select for a fleet mission"
          className="mt-1 shrink-0"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <button onClick={onOpen} className="truncate text-left font-semibold text-gray-800 hover:text-brand hover:underline">
              {w.name}
            </button>
            {w.origin?.kind && (
              <span className="shrink-0 rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">autopilot</span>
            )}
          </div>
          {profile && <div className="mt-1"><ClassPills c={profile.classification} /></div>}
          {w.description && <p className="mt-0.5 line-clamp-1 text-xs text-gray-500">{w.description}</p>}
        </div>
        <div className="flex flex-col items-center gap-0.5">
          <ScoreBadge score={health?.score ?? null} band={health?.band ?? "unknown"} />
          {(profile?.score_trend?.points?.length ?? 0) > 1 && (
            <div className="flex items-center gap-0.5" title={`Score trend (${profile!.score_trend.count} points)`}>
              <Sparkline points={profile!.score_trend.points} width={44} height={14} color={bandColor(health?.band ?? "unknown")} />
              {typeof profile?.score_trend.delta === "number" && profile.score_trend.delta !== 0 && (
                <span className={`text-[10px] font-medium ${profile.score_trend.delta > 0 ? "text-green-600" : "text-red-600"}`}>
                  {profile.score_trend.delta > 0 ? "▲" : "▼"}{Math.abs(profile.score_trend.delta)}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Body: donut + types | radar */}
      <div className="mt-3 grid grid-cols-[auto_1fr_auto] items-center gap-3">
        <CompositionDonut
          data={comp?.by_category ?? []}
          size={84}
          centerLabel={String(comp?.total ?? w.summary?.total_resources ?? 0)}
          centerSub="resources"
        />
        <div className="min-w-0 space-y-1">
          {topTypes.length > 0 ? (
            topTypes.map((t) => (
              <div key={t.friendly} className="flex items-center gap-1.5 text-[11px] text-gray-600">
                <AzureIcon kind="resource" type={t.type} className="h-3.5 w-3.5 text-gray-400" />
                <span className="truncate">{t.friendly}</span>
                <span className="ml-auto shrink-0 tabular-nums font-medium text-gray-700">{t.count}</span>
              </div>
            ))
          ) : (
            <div className="text-[11px] text-gray-400">No resources yet</div>
          )}
          {(comp?.by_type.length ?? 0) > 5 && (
            <div className="text-[10px] text-gray-400">+{(comp!.by_type.length - 5)} more types</div>
          )}
        </div>
        {health && <HealthRadar health={health} size={104} />}
      </div>

      {/* Category legend chips */}
      {(comp?.by_category.length ?? 0) > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {comp!.by_category.map((c) => (
            <span key={c.category} className="inline-flex items-center gap-1 rounded bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-600">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: categoryColor(c.category) }} />
              {c.category} {c.count}
            </span>
          ))}
        </div>
      )}

      {/* Risk row */}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {risk?.retirements_90d ? <RiskChip icon="⚠" label={`${risk.retirements_90d} retiring ≤90d`} tone="amber" /> : null}
        {risk?.criticals ? <RiskChip icon="🔴" label={`${risk.criticals} critical`} tone="red" /> : null}
        {health?.extras?.backupdr?.dr_pairs_unhealthy ? <RiskChip icon="🛟" label={`${health.extras.backupdr.dr_pairs_unhealthy} DR unhealthy`} tone="amber" /> : null}
        {health && !profile?.analyzed && <RiskChip icon="○" label="Not analyzed" tone="gray" />}
      </div>

      {/* Actions */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button onClick={onOpen} className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10">
          Open ▸
        </button>
        <button onClick={onRefresh} disabled={refreshing} className="flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60">
          <span className={refreshing ? "inline-block animate-spin" : ""}>↻</span>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
        <button onClick={onMission} title="Open Workload Mission Control" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">🚀 Mission</button>
        <button onClick={onAssess} title="Run a Well-Architected assessment" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">✓ Assess</button>
        <button onClick={onEdit} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Edit</button>
        <button onClick={onDelete} className="ml-auto rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
      </div>
    </div>
  );
}
