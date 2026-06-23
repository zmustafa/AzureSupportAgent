// Workloads command-center visualization primitives — small, dependency-free SVG charts
// reused by the fleet cards, the cockpit strip and the workload detail page.
import type { WorkloadProfile } from "../../api";

// Category → accent color (mirrors backend taxonomy CATEGORIES). Stable + colorblind-aware-ish.
export const CATEGORY_COLOR: Record<string, string> = {
  Compute: "#2563eb",
  Web: "#7c3aed",
  Containers: "#0891b2",
  Data: "#0d9488",
  Storage: "#65a30d",
  Networking: "#d97706",
  Security: "#dc2626",
  Integration: "#db2777",
  "AI / ML": "#9333ea",
  Analytics: "#ca8a04",
  Monitoring: "#0ea5e9",
  Management: "#64748b",
  Other: "#94a3b8",
};

export function categoryColor(cat: string): string {
  return CATEGORY_COLOR[cat] ?? CATEGORY_COLOR.Other;
}

// Health band → color.
export function bandColor(band: string): string {
  return band === "good" ? "#16a34a" : band === "warn" ? "#d97706" : band === "poor" ? "#dc2626" : "#94a3b8";
}
export function bandBg(band: string): string {
  return band === "good" ? "bg-green-100 text-green-700" : band === "warn" ? "bg-amber-100 text-amber-700" : band === "poor" ? "bg-red-100 text-red-700" : "bg-gray-100 text-gray-500";
}

// ---- Composition donut ----------------------------------------------------------
// A donut of resource counts by category. Renders nothing when total is 0.
export function CompositionDonut({
  data,
  size = 96,
  thickness = 14,
  centerLabel,
  centerSub,
}: {
  data: { category: string; count: number }[];
  size?: number;
  thickness?: number;
  centerLabel?: string;
  centerSub?: string;
}) {
  const total = data.reduce((a, d) => a + d.count, 0);
  const r = (size - thickness) / 2;
  const c = size / 2;
  const circ = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      <circle cx={c} cy={c} r={r} fill="none" stroke="#f1f5f9" strokeWidth={thickness} />
      {total > 0 &&
        data.map((d) => {
          const frac = d.count / total;
          const len = frac * circ;
          const el = (
            <circle
              key={d.category}
              cx={c}
              cy={c}
              r={r}
              fill="none"
              stroke={categoryColor(d.category)}
              strokeWidth={thickness}
              strokeDasharray={`${len} ${circ - len}`}
              strokeDashoffset={-offset}
              transform={`rotate(-90 ${c} ${c})`}
            >
              <title>{`${d.category}: ${d.count}`}</title>
            </circle>
          );
          offset += len;
          return el;
        })}
      <text x={c} y={c - 2} textAnchor="middle" dominantBaseline="middle" className="fill-gray-800" style={{ fontSize: size * 0.26, fontWeight: 700 }}>
        {centerLabel ?? total}
      </text>
      {centerSub && (
        <text x={c} y={c + size * 0.17} textAnchor="middle" dominantBaseline="middle" className="fill-gray-400" style={{ fontSize: size * 0.11 }}>
          {centerSub}
        </text>
      )}
    </svg>
  );
}

// ---- Health radar ---------------------------------------------------------------
// A polygon "fingerprint" over the per-signal 0-100 metrics. Missing signals plot at the
// center (0) but are drawn hollow so an un-analyzed axis reads as a gap, not a zero.
const RADAR_AXES: { key: keyof WorkloadProfile["health"]; label: string }[] = [
  { key: "monitoring", label: "Mon" },
  { key: "telemetry", label: "Tel" },
  { key: "backupdr", label: "Bkp" },
  { key: "performance", label: "Perf" },
  { key: "ownership", label: "Own" },
  { key: "policy", label: "Pol" },
  { key: "tags", label: "Tag" },
];

export function HealthRadar({ health, size = 120 }: { health: WorkloadProfile["health"]; size?: number }) {
  const c = size / 2;
  const r = c - 16;
  const n = RADAR_AXES.length;
  const pt = (i: number, frac: number) => {
    const ang = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [c + Math.cos(ang) * r * frac, c + Math.sin(ang) * r * frac] as const;
  };
  const ring = (frac: number) => RADAR_AXES.map((_, i) => pt(i, frac).join(",")).join(" ");
  const valuePts = RADAR_AXES.map((a, i) => {
    const v = health[a.key];
    const frac = typeof v === "number" ? Math.max(0, Math.min(100, v)) / 100 : 0;
    return pt(i, frac).join(",");
  }).join(" ");
  const band = health.band;
  const stroke = bandColor(band);
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <polygon key={f} points={ring(f)} fill="none" stroke="#e2e8f0" strokeWidth={1} />
      ))}
      {RADAR_AXES.map((a, i) => {
        const [x, y] = pt(i, 1);
        return <line key={a.label} x1={c} y1={c} x2={x} y2={y} stroke="#e2e8f0" strokeWidth={1} />;
      })}
      <polygon points={valuePts} fill={`${stroke}22`} stroke={stroke} strokeWidth={1.5} />
      {RADAR_AXES.map((a, i) => {
        const [x, y] = pt(i, 1.16);
        const v = health[a.key];
        return (
          <text key={a.label} x={x} y={y} textAnchor="middle" dominantBaseline="middle" style={{ fontSize: 9 }} className={v == null ? "fill-gray-300" : "fill-gray-500"}>
            {a.label}
          </text>
        );
      })}
    </svg>
  );
}

// ---- Score badge ----------------------------------------------------------------
export function ScoreBadge({ score, band, size = "md" }: { score: number | null; band: string; size?: "sm" | "md" | "lg" }) {
  const dim = size === "lg" ? "h-14 w-14 text-xl" : size === "sm" ? "h-8 w-8 text-xs" : "h-11 w-11 text-base";
  const color = bandColor(band);
  return (
    <div
      className={`flex ${dim} items-center justify-center rounded-full font-bold tabular-nums`}
      style={{ color, backgroundColor: `${color}1a`, border: `2px solid ${color}55` }}
      title={score == null ? "Not analyzed yet" : `Health score ${score}/100`}
    >
      {score == null ? "—" : score}
    </div>
  );
}

// ---- Mini metric bar ------------------------------------------------------------
export function MetricBar({ label, value }: { label: string; value: number | null }) {
  const v = typeof value === "number" ? Math.max(0, Math.min(100, value)) : null;
  const color = v == null ? "#cbd5e1" : v >= 80 ? "#16a34a" : v >= 50 ? "#d97706" : "#dc2626";
  return (
    <div className="flex items-center gap-2">
      <span className="w-16 shrink-0 text-[11px] text-gray-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
        {v != null && <div className="h-full rounded-full" style={{ width: `${v}%`, backgroundColor: color }} />}
      </div>
      <span className="w-9 shrink-0 text-right text-[11px] tabular-nums text-gray-500">{v == null ? "—" : `${Math.round(v)}%`}</span>
    </div>
  );
}

// ---- Sparkline ------------------------------------------------------------------
export function Sparkline({ points, width = 80, height = 22, color = "#2563eb" }: { points: number[]; width?: number; height?: number; color?: string }) {
  if (!points.length) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / Math.max(1, points.length - 1);
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(height - ((p - min) / span) * height).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} className="shrink-0">
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ---- Treemap (simple row-based squarify-lite) -----------------------------------
// Good enough for a compact composition treemap on the cockpit strip.
export function Treemap({
  data,
  width,
  height,
  onClick,
}: {
  data: { label: string; count: number; color: string }[];
  width: number;
  height: number;
  onClick?: (label: string) => void;
}) {
  const total = data.reduce((a, d) => a + d.count, 0) || 1;
  // Lay out in horizontal rows, wrapping. Each tile area ∝ count.
  const area = width * height;
  let x = 0;
  let y = 0;
  let rowH = 0;
  const tiles: { label: string; count: number; color: string; x: number; y: number; w: number; h: number }[] = [];
  for (const d of data) {
    const a = (d.count / total) * area;
    const w = Math.max(28, Math.min(width, Math.sqrt(a * 1.6)));
    const h = Math.max(22, a / w);
    if (x + w > width) {
      x = 0;
      y += rowH;
      rowH = 0;
    }
    tiles.push({ ...d, x, y, w, h });
    x += w;
    rowH = Math.max(rowH, h);
  }
  return (
    <svg width={width} height={Math.max(height, y + rowH)} className="overflow-visible">
      {tiles.map((t) => (
        <g key={t.label} onClick={() => onClick?.(t.label)} style={{ cursor: onClick ? "pointer" : "default" }}>
          <rect x={t.x + 1} y={t.y + 1} width={Math.max(0, t.w - 2)} height={Math.max(0, t.h - 2)} rx={4} fill={t.color} opacity={0.85}>
            <title>{`${t.label}: ${t.count}`}</title>
          </rect>
          {t.w > 44 && t.h > 26 && (
            <>
              <text x={t.x + 6} y={t.y + 16} className="fill-white" style={{ fontSize: 10, fontWeight: 600 }}>{t.count}</text>
              <text x={t.x + 6} y={t.y + 28} className="fill-white/90" style={{ fontSize: 8 }}>{t.label.length > 14 ? t.label.slice(0, 13) + "…" : t.label}</text>
            </>
          )}
        </g>
      ))}
    </svg>
  );
}

// ---- Classification pills -------------------------------------------------------
export const ENV_STYLE: Record<string, string> = {
  production: "bg-red-50 text-red-700 ring-red-200",
  staging: "bg-amber-50 text-amber-700 ring-amber-200",
  development: "bg-sky-50 text-sky-700 ring-sky-200",
  test: "bg-violet-50 text-violet-700 ring-violet-200",
  dr: "bg-orange-50 text-orange-700 ring-orange-200",
  shared: "bg-teal-50 text-teal-700 ring-teal-200",
  unknown: "bg-gray-50 text-gray-500 ring-gray-200",
};
export const CRIT_STYLE: Record<string, string> = {
  critical: "bg-red-600 text-white",
  high: "bg-orange-500 text-white",
  medium: "bg-amber-400 text-amber-900",
  low: "bg-gray-200 text-gray-600",
};

export function ClassPills({ c }: { c: WorkloadProfile["classification"] }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {c.environment && c.environment !== "unknown" && (
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ring-1 ${ENV_STYLE[c.environment] ?? ENV_STYLE.unknown}`}>{c.environment}</span>
      )}
      {c.criticality && (
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${CRIT_STYLE[c.criticality] ?? CRIT_STYLE.low}`}>{c.criticality}</span>
      )}
      {c.data_classification && c.data_classification !== "unknown" && (
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600">{c.data_classification}</span>
      )}
    </div>
  );
}
