import type { AssessmentPillarScore } from "../api";

// Per-pillar accent colors — matched to the legend in the product's design language
// (Security indigo · Reliability cyan · Cost green · Operations amber · Performance pink).
const PILLAR_COLOR: Record<string, string> = {
  security: "#6366f1",
  reliability: "#06b6d4",
  cost: "#10b981",
  operations: "#f59e0b",
  performance: "#ec4899",
};

const PILLAR_LABEL: Record<string, string> = {
  security: "Security",
  reliability: "Reliability",
  cost: "Cost",
  operations: "Operations",
  performance: "Performance",
};

// Short axis labels drawn on the chart itself (full names stay in tooltips + pillar cards).
const PILLAR_ABBR: Record<string, string> = {
  security: "Sec",
  reliability: "Rel",
  cost: "Cost",
  operations: "Ops",
  performance: "Perf",
};

// Fixed clockwise order from the top vertex, so the pentagon is stable across runs.
const ORDER = ["security", "reliability", "cost", "operations", "performance"];

function scoreHex(v: number | null): string {
  if (v == null) return "#9ca3af";
  if (v >= 80) return "#16a34a";
  if (v >= 60) return "#d97706";
  return "#dc2626";
}

/**
 * Well-Architected pillar radar (spider chart). Plots each pillar's 0–100 score on its own
 * axis and fills the polygon so a run's posture "shape" is readable at a glance. Pure
 * inline SVG — no chart dependency — with a colored-dot legend showing each pillar's score.
 */
export function PillarRadar({
  scores,
  size = 200,
  className = "",
  showLegend = true,
}: {
  scores: Record<string, AssessmentPillarScore>;
  size?: number;
  className?: string;
  showLegend?: boolean;
}) {
  const axes = ORDER.filter((p) => scores[p] != null).map((p) => ({
    key: p,
    label: PILLAR_LABEL[p] ?? p,
    abbr: PILLAR_ABBR[p] ?? (PILLAR_LABEL[p] ?? p).slice(0, 4),
    value: scores[p]?.score ?? null,
    color: PILLAR_COLOR[p] ?? "#6366f1",
  }));

  if (axes.length < 3) {
    return <p className={`text-xs text-gray-400 ${className}`}>Score 3+ pillars to chart the radar.</p>;
  }

  const cx = size / 2;
  const cy = size / 2;
  const R = size / 2 - 40; // padding for the rings + on-chart axis labels
  const n = axes.length;
  const pt = (i: number, radius: number): [number, number] => {
    const ang = -Math.PI / 2 + (i / n) * 2 * Math.PI;
    return [cx + radius * Math.cos(ang), cy + radius * Math.sin(ang)];
  };
  const valueRadius = (v: number | null) => ((Math.max(0, Math.min(100, v ?? 0)) / 100) * R);
  const poly = axes.map((a, i) => pt(i, valueRadius(a.value)).join(",")).join(" ");

  return (
    <div className={`flex flex-wrap items-center gap-4 ${className}`}>
      <svg viewBox={`0 0 ${size} ${size}`} className="h-44 w-44 shrink-0" role="img" aria-label="Well-Architected pillar radar">
        {/* concentric grid rings */}
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <polygon
            key={f}
            points={axes.map((_, i) => pt(i, R * f).join(",")).join(" ")}
            fill="none"
            stroke="#e5e7eb"
            strokeWidth="1"
          />
        ))}
        {/* spokes (+ on-chart axis labels only when there's no side legend) */}
        {axes.map((a, i) => {
          const [x, y] = pt(i, R);
          const [lx, ly] = pt(i, R + 16);
          const anchor = Math.abs(lx - cx) < 6 ? "middle" : lx > cx ? "start" : "end";
          return (
            <g key={i}>
              <line x1={cx} y1={cy} x2={x} y2={y} stroke="#e5e7eb" strokeWidth="1" />
              {!showLegend && (
                <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle" fontSize="13" fontWeight="700" fill="#4b5563">{a.abbr}</text>
              )}
            </g>
          );
        })}
        {/* score polygon */}
        <polygon points={poly} fill="#6366f1" fillOpacity="0.18" stroke="#6366f1" strokeWidth="2" strokeLinejoin="round" />
        {/* per-axis value dots */}
        {axes.map((a, i) => {
          const [x, y] = pt(i, valueRadius(a.value));
          return (
            <circle key={a.key} cx={x} cy={y} r="3.5" fill={a.color}>
              <title>{`${a.label}: ${a.value ?? "—"}`}</title>
            </circle>
          );
        })}
      </svg>

      {showLegend && (
        <ul className="min-w-[120px] flex-1 space-y-1.5">
          {axes.map((a) => (
            <li key={a.key} className="flex items-center gap-2 text-xs">
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: a.color }} />
              <span className="min-w-0 flex-1 truncate text-gray-600">{a.label}</span>
              <span className="shrink-0 font-semibold tabular-nums" style={{ color: scoreHex(a.value) }}>
                {a.value ?? "—"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
