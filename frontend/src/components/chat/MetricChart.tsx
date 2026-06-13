/**
 * MetricChart — renders an interactive Azure Monitor time-series chart inside a chat
 * answer. The assistant emits a fenced ```azchart block whose body is a tiny JSON spec
 * ({ chart_id, title, type, unit }); the `CodeBlock` renderer swaps this component in
 * (exactly like the ```mermaid path). We fetch the actual series back by `chart_id` so
 * the message stays small and the data can't be hallucinated by the model.
 *
 * Lazy-loaded by ChatView so recharts stays out of the initial chat bundle.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type WidgetTableResult } from "../../api";

const PALETTE = [
  "#6366f1",
  "#0ea5e9",
  "#10b981",
  "#f59e0b",
  "#ec4899",
  "#8b5cf6",
  "#14b8a6",
  "#f43f5e",
  "#84cc16",
  "#06b6d4",
];

interface ChartSpec {
  chart_id?: string;
  title?: string;
  type?: string; // line | area | bar
  unit?: string;
  x?: string;
  series?: string[];
  // Optional inline data (lets the component render without a server round-trip).
  result?: WidgetTableResult;
  metrics?: string[];
  resource_ids?: string[];
  timespan?: string;
  interval?: string;
  aggregation?: string;
}

/** Parse the JSON inside the ```azchart fence; null if it isn't valid JSON. */
function parseSpec(raw: string): ChartSpec | null {
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? (v as ChartSpec) : null;
  } catch {
    return null;
  }
}

/** Two-digit pad. */
const p2 = (n: number) => String(n).padStart(2, "0");

/** Compact axis label "MM-DD HH:MM" from an ISO timestamp. */
function fmtAxisTime(v: unknown): string {
  const d = new Date(String(v ?? ""));
  if (Number.isNaN(d.getTime())) return String(v ?? "");
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

/** Full tooltip label "YYYY-MM-DD HH:MM". */
function fmtFullTime(v: unknown): string {
  const d = new Date(String(v ?? ""));
  if (Number.isNaN(d.getTime())) return String(v ?? "");
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

/** Trim a number to at most 2 decimals and group thousands. */
function fmtVal(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return parseFloat(v.toFixed(2)).toLocaleString();
}

/** Turn a normalized {columns, rows} table into recharts rows + series names. */
function buildData(result: WidgetTableResult, xOverride?: string, seriesOverride?: string[]) {
  const cols = result.columns || [];
  if (!cols.length) return { data: [] as Record<string, unknown>[], xKey: "timestamp", series: [] as string[] };
  const xKey = xOverride || cols[0]?.name || "timestamp";
  const xi = cols.findIndex((c) => c.name === xKey);
  let series =
    seriesOverride && seriesOverride.length
      ? seriesOverride
      : cols.filter((c) => c.name !== xKey && c.type === "number").map((c) => c.name);
  if (!series.length) series = cols.filter((c) => c.name !== xKey).map((c) => c.name);
  const seriesIdx = new Map(series.map((s) => [s, cols.findIndex((c) => c.name === s)]));
  const data = (result.rows || []).map((row) => {
    const o: Record<string, unknown> = { [xKey]: row[xi >= 0 ? xi : 0] };
    for (const s of series) {
      const si = seriesIdx.get(s) ?? -1;
      o[s] = si >= 0 ? row[si] : null;
    }
    return o;
  });
  return { data, xKey, series };
}

function ChartTooltip({
  active,
  payload,
  label,
  unit,
}: {
  active?: boolean;
  payload?: { name?: string; value?: unknown; color?: string }[];
  label?: unknown;
  unit?: string;
}) {
  if (!active || !payload || !payload.length) return null;
  const u = unit ? ` ${unit}` : "";
  return (
    <div className="rounded-lg border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow-md backdrop-blur">
      <div className="mb-1 font-medium text-gray-700">{fmtFullTime(label)}</div>
      {payload.map((row, i) => (
        <div key={i} className="flex items-center gap-2 text-gray-600">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: row.color }} />
          <span className="text-gray-500">{row.name}</span>
          <span className="ml-auto font-semibold text-gray-800">
            {fmtVal(row.value)}
            {u}
          </span>
        </div>
      ))}
    </div>
  );
}

type ChartKind = "line" | "area" | "bar" | "pie" | "donut";

/** Aggregate each series to a single value so it can be shown as one pie slice.
 *  Uses the average of the series' datapoints (works for a single point too); slices
 *  with a non-positive aggregate are dropped since a pie can't render them. */
function buildPieData(
  data: Record<string, unknown>[],
  series: string[],
): { name: string; value: number }[] {
  return series
    .map((s) => {
      const nums = data
        .map((row) => row[s])
        .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
      const avg = nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : 0;
      return { name: s, value: avg };
    })
    .filter((d) => d.value > 0);
}

/** Tooltip for pie/donut slices (shows slice name + value + % of total). */
function PieTooltip({
  active,
  payload,
  unit,
}: {
  active?: boolean;
  payload?: { name?: string; value?: unknown; payload?: { _total?: number } }[];
  unit?: string;
}) {
  if (!active || !payload || !payload.length) return null;
  const row = payload[0];
  const total = row.payload?._total ?? 0;
  const val = typeof row.value === "number" ? row.value : 0;
  const pct = total > 0 ? ((val / total) * 100).toFixed(1) : "";
  const u = unit ? ` ${unit}` : "";
  return (
    <div className="rounded-lg border border-gray-200 bg-white/95 px-3 py-2 text-xs shadow-md backdrop-blur">
      <span className="font-medium text-gray-700">{row.name}</span>
      <span className="ml-2 font-semibold text-gray-800">
        {fmtVal(val)}
        {u}
      </span>
      {pct && <span className="ml-1 text-gray-400">({pct}%)</span>}
    </div>
  );
}

/** Small segmented control to switch the chart type on the fly. */
function ChartTypeToggle({ value, onChange }: { value: ChartKind; onChange: (k: ChartKind) => void }) {
  // 'donut' shares the 'Pie' button (a donut is just a pie with a hole); clicking Pie
  // from a donut keeps it a donut, otherwise selects a plain pie.
  const active: ChartKind = value === "donut" ? "pie" : value;
  const opts: { k: ChartKind; label: string }[] = [
    { k: "line", label: "Line" },
    { k: "area", label: "Area" },
    { k: "bar", label: "Bar" },
    { k: "pie", label: "Pie" },
  ];
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-gray-200 bg-gray-50 p-0.5">
      {opts.map(({ k, label }) => (
        <button
          key={k}
          type="button"
          onClick={() => onChange(k)}
          className={
            "rounded px-2 py-0.5 text-[11px] font-medium transition " +
            (active === k ? "bg-white text-brand shadow-sm" : "text-gray-500 hover:text-gray-700")
          }
          aria-pressed={active === k}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function Frame({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="my-3 rounded-xl border border-gray-200 bg-white p-3 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          {title && <div className="px-1 text-sm font-semibold text-gray-800">{title}</div>}
          {subtitle && <div className="px-1 pb-1 text-[11px] text-gray-400">{subtitle}</div>}
        </div>
        {action && <div className="shrink-0 pt-0.5">{action}</div>}
      </div>
      <div className="h-[300px] w-full">{children}</div>
    </div>
  );
}

export function MetricChart({ spec: rawSpec }: { spec: string }) {
  const spec = useMemo(() => parseSpec(rawSpec), [rawSpec]);
  const chartId = spec?.chart_id;

  const q = useQuery({
    queryKey: ["chatChart", chartId],
    queryFn: () => api.getChartArtifact(chartId as string),
    enabled: !!chartId && !spec?.result,
    staleTime: Infinity,
    retry: 1,
  });

  const serverSpec = (q.data?.spec ?? {}) as ChartSpec;
  const result = spec?.result ?? q.data?.result;

  const title = spec?.title || serverSpec.title || "Metric";
  const baseType = (spec?.type || serverSpec.type || "line").toLowerCase();
  // User can override the chart type live; default to what the agent chose.
  const [override, setOverride] = useState<ChartKind | null>(null);
  const KINDS = ["line", "area", "bar", "pie", "donut"];
  const type: ChartKind = (override ?? (KINDS.includes(baseType) ? baseType : "line")) as ChartKind;
  const unit = spec?.unit ?? serverSpec.unit ?? "";
  const subtitleParts = [
    serverSpec.aggregation,
    serverSpec.timespan ? `lookback ${serverSpec.timespan}` : "",
    serverSpec.interval ? `grain ${serverSpec.interval}` : "",
  ].filter(Boolean);
  const subtitle = subtitleParts.join(" · ");

  const { data, xKey, series } = useMemo(
    () => (result ? buildData(result, spec?.x, spec?.series) : { data: [], xKey: "timestamp", series: [] }),
    [result, spec?.x, spec?.series],
  );

  // Pie/donut: one slice per metric (aggregated), with the total stashed for the tooltip %.
  const pieData = useMemo(() => {
    const slices = buildPieData(data, series);
    const total = slices.reduce((a, b) => a + b.value, 0);
    return slices.map((s) => ({ ...s, _total: total }));
  }, [data, series]);

  if (!spec) {
    // The JSON is still streaming in (or, rarely, malformed) — show a loading placeholder
    // rather than an error flash. It snaps to the chart once the full block arrives.
    return <div className="my-3 h-[340px] w-full animate-pulse rounded-xl bg-gray-100" />;
  }
  if (chartId && q.isLoading) {
    return <div className="my-3 h-[340px] w-full animate-pulse rounded-xl bg-gray-100" />;
  }
  if (chartId && q.isError) {
    return (
      <Frame title={title}>
        <div className="flex h-full items-center justify-center text-xs text-amber-600">
          This chart is no longer available (it may have expired).
        </div>
      </Frame>
    );
  }
  if (result?.error) {
    return (
      <Frame title={title}>
        <div className="flex h-full items-center justify-center p-3 text-center text-xs text-amber-600">
          {result.error}
        </div>
      </Frame>
    );
  }
  if (!data.length || !series.length) {
    return (
      <Frame title={title} subtitle={subtitle}>
        <div className="flex h-full items-center justify-center text-xs text-gray-400">No data.</div>
      </Frame>
    );
  }

  const axis = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
      <XAxis dataKey={xKey} tick={{ fontSize: 10 }} tickFormatter={fmtAxisTime} minTickGap={28} />
      <YAxis tick={{ fontSize: 10 }} width={44} tickFormatter={(v) => fmtVal(v)} />
      <Tooltip content={<ChartTooltip unit={unit} />} />
      {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
    </>
  );

  return (
    <Frame
      title={title}
      subtitle={subtitle}
      action={<ChartTypeToggle value={type} onChange={setOverride} />}
    >
      {(type === "pie" || type === "donut") && !pieData.length ? (
        <div className="flex h-full items-center justify-center px-3 text-center text-xs text-gray-400">
          A pie needs positive values — these metrics can't be shown as slices. Try Line, Area, or Bar.
        </div>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          {type === "pie" || type === "donut" ? (
            <PieChart>
              <Tooltip content={<PieTooltip unit={unit} />} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={type === "donut" ? "55%" : 0}
                outerRadius="80%"
                paddingAngle={1}
                stroke="#fff"
                strokeWidth={1}
              >
                {pieData.map((_, i) => (
                  <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                ))}
              </Pie>
            </PieChart>
          ) : type === "bar" ? (
            <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              {axis}
              {series.map((s, i) => (
                <Bar key={s} dataKey={s} fill={PALETTE[i % PALETTE.length]} radius={[2, 2, 0, 0]} />
              ))}
            </BarChart>
          ) : type === "area" ? (
            <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <defs>
                {series.map((s, i) => (
                  <linearGradient key={s} id={`mc-grad-${i}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={PALETTE[i % PALETTE.length]} stopOpacity={0.35} />
                    <stop offset="95%" stopColor={PALETTE[i % PALETTE.length]} stopOpacity={0.02} />
                  </linearGradient>
                ))}
              </defs>
              {axis}
              {series.map((s, i) => (
                <Area
                  key={s}
                  type="monotone"
                  dataKey={s}
                  stroke={PALETTE[i % PALETTE.length]}
                  strokeWidth={2}
                  fill={`url(#mc-grad-${i})`}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
              ))}
            </AreaChart>
          ) : (
            <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              {axis}
              {series.map((s, i) => (
                <Line
                  key={s}
                  type="monotone"
                  dataKey={s}
                  stroke={PALETTE[i % PALETTE.length]}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                  connectNulls
                />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      )}
    </Frame>
  );
}
