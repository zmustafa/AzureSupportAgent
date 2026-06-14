import { useMemo, useState } from "react";

export interface TrendPoint {
  at: string;
  pct: number | null;
}

interface TrendChartProps {
  points: TrendPoint[];
  current?: number | null;
  previous?: number | null;
  delta?: number | null;
  unit?: string;
  loading?: boolean;
  /** Label shown before the delta, e.g. "vs last scan". */
  deltaLabel?: string;
}

function tone(pct: number | null | undefined): string {
  if (pct == null) return "#9ca3af";
  return pct >= 80 ? "#16a34a" : pct >= 50 ? "#d97706" : "#dc2626";
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * Compact inline-SVG trend sparkline for a 0-100 posture metric (coverage %, score, …),
 * with a delta badge and a hover tooltip. Reused by all four coverage/profile dashboards
 * so a workload's history "moves" visibly scan-over-scan. No chart dependency.
 */
export function TrendChart({ points, current, previous, delta, unit = "%", loading, deltaLabel = "vs last scan" }: TrendChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 150;
  const H = 40;
  const PAD = 4;

  const geom = useMemo(() => {
    const valid = points.filter((p) => typeof p.pct === "number") as { at: string; pct: number }[];
    if (valid.length === 0) return null;
    const vals = valid.map((p) => p.pct);
    let min = Math.min(...vals);
    let max = Math.max(...vals);
    if (max - min < 8) {
      // Pad a near-flat series so it sits mid-height instead of hugging an edge.
      const mid = (min + max) / 2;
      min = Math.max(0, mid - 6);
      max = Math.min(100, mid + 6);
    }
    const span = max - min || 1;
    const n = valid.length;
    const xy = valid.map((p, i) => ({
      x: PAD + (n === 1 ? (W - 2 * PAD) / 2 : (i / (n - 1)) * (W - 2 * PAD)),
      y: PAD + (1 - (p.pct - min) / span) * (H - 2 * PAD),
      pct: p.pct,
      at: p.at,
    }));
    return { xy, min, max };
  }, [points]);

  const last = current ?? (geom ? geom.xy[geom.xy.length - 1].pct : null);
  const lineColor = tone(last);

  if (loading) {
    return <div className="h-10 w-[150px] animate-pulse rounded bg-gray-100" />;
  }
  if (!geom || geom.xy.length < 2) {
    return (
      <div className="flex h-10 w-[150px] items-center justify-center rounded border border-dashed border-gray-200 text-[10px] text-gray-400">
        Run again to chart trend
      </div>
    );
  }

  const { xy } = geom;
  const linePath = xy.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${xy[xy.length - 1].x.toFixed(1)},${H - PAD} L${xy[0].x.toFixed(1)},${H - PAD} Z`;
  const hp = hover != null ? xy[hover] : null;
  const gid = `tg-${lineColor.replace("#", "")}`;

  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="h-10 w-[150px]"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const x = ((e.clientX - rect.left) / rect.width) * W;
            let best = 0;
            let bestD = Infinity;
            xy.forEach((p, i) => {
              const d = Math.abs(p.x - x);
              if (d < bestD) { bestD = d; best = i; }
            });
            setHover(best);
          }}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity="0.22" />
              <stop offset="100%" stopColor={lineColor} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={areaPath} fill={`url(#${gid})`} />
          <path d={linePath} fill="none" stroke={lineColor} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          {hp && <line x1={hp.x} y1={PAD} x2={hp.x} y2={H - PAD} stroke="#9ca3af" strokeWidth="0.75" strokeDasharray="2 2" />}
          {/* Last point + hovered point markers. */}
          <circle cx={xy[xy.length - 1].x} cy={xy[xy.length - 1].y} r="2.4" fill={lineColor} />
          {hp && hover !== xy.length - 1 && <circle cx={hp.x} cy={hp.y} r="2.4" fill="#fff" stroke={lineColor} strokeWidth="1.5" />}
        </svg>
        {hp && (
          <div
            className="pointer-events-none absolute -top-7 z-10 -translate-x-1/2 whitespace-nowrap rounded bg-gray-900 px-1.5 py-0.5 text-[10px] text-white shadow"
            style={{ left: `${(hp.x / W) * 100}%` }}
          >
            {hp.pct}{unit} · {fmtDate(hp.at)}
          </div>
        )}
      </div>
      {typeof delta === "number" && delta !== 0 ? (
        <span
          className={`whitespace-nowrap text-[11px] font-medium ${delta > 0 ? "text-green-600" : "text-red-600"}`}
          title={typeof previous === "number" ? `Previous scan: ${previous}${unit}` : undefined}
        >
          {delta > 0 ? "▲" : "▼"} {Math.abs(delta)}{unit}
          <span className="ml-0.5 font-normal text-gray-400">{deltaLabel}</span>
        </span>
      ) : (
        <span className="whitespace-nowrap text-[11px] text-gray-400">{points.length} scan{points.length === 1 ? "" : "s"}</span>
      )}
    </div>
  );
}
