/**
 * Monitor 2.0 — generic, data-bound widget renderers.
 *
 * Every widget fetches a normalized {columns, rows, meta} table from the backend via
 * `api.runMonitorWidget(dataSource)` and renders it with a type-specific component
 * (chart / table / stat / gauge / availability / map / markdown / clock / list). The
 * transform layer turns the flat table into the shape each renderer needs, so the same
 * Resource-Graph / Log-Analytics / metrics / ping data can drive any visualization.
 */
import { useEffect, useMemo, useState } from "react";
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
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type MonitorWidget, type WidgetTableResult } from "../../api";

const PALETTE = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6", "#f43f5e", "#84cc16", "#06b6d4"];

// ---------------------------------------------------------------------------
// Data hook — per-widget query with its own refresh cadence.
// ---------------------------------------------------------------------------
export function useWidgetData(widget: MonitorWidget, params?: Record<string, unknown>, live = true) {
  const needsData = widget.dataSource?.kind && widget.dataSource.kind !== "none";
  const intervalMs = widget.refresh?.mode === "live" ? (widget.refresh.intervalSec || 60) * 1000 : false;
  return useQuery({
    queryKey: ["monitorWidget", widget.id, widget.dataSource, params],
    queryFn: () => api.runMonitorWidget(widget.dataSource, params).then((r) => r.result),
    enabled: !!needsData,
    refetchInterval: live ? intervalMs : false,
    staleTime: 5000,
  });
}

// ---------------------------------------------------------------------------
// Transform helpers — table -> chart/series shape.
// ---------------------------------------------------------------------------
function colIndex(result: WidgetTableResult, name: string): number {
  return result.columns.findIndex((c) => c.name === name);
}

function firstColOfType(result: WidgetTableResult, types: string[], exclude?: string): string | null {
  for (const c of result.columns) {
    if (c.name === exclude) continue;
    if (types.includes(c.type)) return c.name;
  }
  return null;
}

/** Build recharts row objects [{x, series1, series2,...}] from the table + transform. */
function toChartData(result: WidgetTableResult, transform: Record<string, unknown>) {
  const cols = result.columns;
  if (cols.length === 0) return { data: [] as Record<string, unknown>[], xKey: "x", series: [] as string[] };
  const xKey =
    (transform.x as string) ||
    firstColOfType(result, ["datetime"]) ||
    firstColOfType(result, ["string", "category"]) ||
    cols[0].name;
  let series = (transform.series as string[]) || [];
  if (!series.length) {
    series = cols.filter((c) => c.name !== xKey && c.type === "number").map((c) => c.name);
    if (!series.length) series = cols.filter((c) => c.name !== xKey).map((c) => c.name).slice(0, 1);
  }
  const xi = colIndex(result, xKey);
  const data = result.rows.map((row) => {
    const o: Record<string, unknown> = { [xKey]: row[xi] };
    for (const s of series) {
      const si = colIndex(result, s);
      o[s] = si >= 0 ? row[si] : null;
    }
    return o;
  });
  return { data, xKey, series };
}

function fmtAxisTime(v: unknown): string {
  const s = String(v ?? "");
  if (s.length >= 16 && s.includes("T")) return s.slice(11, 16); // HH:MM
  if (s.length >= 10 && s[4] === "-") return s.slice(5, 10); // MM-DD
  return s.length > 10 ? s.slice(0, 10) : s;
}

// ---------------------------------------------------------------------------
// Shared frame
// ---------------------------------------------------------------------------
function WidgetBody({
  q,
  children,
  empty,
}: {
  q: { isLoading: boolean; isError: boolean; data?: WidgetTableResult };
  children: React.ReactNode;
  empty?: boolean;
}) {
  if (q.isLoading) return <div className="flex h-full items-center justify-center text-xs text-gray-400">Loading…</div>;
  if (q.isError) return <div className="flex h-full items-center justify-center text-xs text-red-500">Failed to load.</div>;
  if (q.data?.error) return <div className="flex h-full items-center justify-center p-3 text-center text-xs text-amber-600">{q.data.error}</div>;
  if (empty) return <div className="flex h-full items-center justify-center text-xs text-gray-400">No data.</div>;
  return <>{children}</>;
}

// ---------------------------------------------------------------------------
// Renderers
// ---------------------------------------------------------------------------
function ChartWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const chartType = (widget.viz?.chartType as string) || "line";
  const { data, xKey, series } = useMemo(
    () => (q.data ? toChartData(q.data, widget.transform || {}) : { data: [], xKey: "x", series: [] }),
    [q.data, widget.transform],
  );
  const empty = !data.length || !series.length;
  return (
    <WidgetBody q={q} empty={empty}>
      <ResponsiveContainer width="100%" height="100%">
        {renderChart(chartType, data, xKey, series)}
      </ResponsiveContainer>
    </WidgetBody>
  );
}

function renderChart(type: string, data: Record<string, unknown>[], xKey: string, series: string[]) {
  const axis = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
      <XAxis dataKey={xKey} tick={{ fontSize: 10 }} tickFormatter={fmtAxisTime} minTickGap={24} />
      <YAxis tick={{ fontSize: 10 }} width={36} />
      <Tooltip contentStyle={{ fontSize: 12 }} />
      {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
    </>
  );
  if (type === "pie" || type === "donut") {
    const nameKey = xKey;
    const valueKey = series[0];
    return (
      <PieChart>
        <Tooltip contentStyle={{ fontSize: 12 }} />
        <Pie
          data={data}
          dataKey={valueKey}
          nameKey={nameKey}
          innerRadius={type === "donut" ? "55%" : 0}
          outerRadius="80%"
          paddingAngle={1}
        >
          {data.map((_, i) => (
            <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Pie>
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    );
  }
  if (type === "bar" || type === "stackedBar") {
    return (
      <BarChart data={data}>
        {axis}
        {series.map((s, i) => (
          <Bar key={s} dataKey={s} stackId={type === "stackedBar" ? "a" : undefined} fill={PALETTE[i % PALETTE.length]} radius={[2, 2, 0, 0]} />
        ))}
      </BarChart>
    );
  }
  if (type === "area") {
    return (
      <AreaChart data={data}>
        {axis}
        {series.map((s, i) => (
          <Area key={s} type="monotone" dataKey={s} stroke={PALETTE[i % PALETTE.length]} fill={PALETTE[i % PALETTE.length]} fillOpacity={0.18} strokeWidth={2} />
        ))}
      </AreaChart>
    );
  }
  if (type === "scatter") {
    return (
      <ScatterChart>
        {axis}
        {series.map((s, i) => (
          <Scatter key={s} data={data.map((d) => ({ [xKey]: d[xKey], [s]: d[s] }))} dataKey={s} fill={PALETTE[i % PALETTE.length]} />
        ))}
      </ScatterChart>
    );
  }
  return (
    <LineChart data={data}>
      {axis}
      {series.map((s, i) => (
        <Line key={s} type="monotone" dataKey={s} stroke={PALETTE[i % PALETTE.length]} strokeWidth={2} dot={false} />
      ))}
    </LineChart>
  );
}

function TableWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const result = q.data;
  return (
    <WidgetBody q={q} empty={!result || result.rows.length === 0}>
      {result && (
        <div className="h-full overflow-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="sticky top-0 bg-gray-50 text-gray-500">
              <tr>
                {result.columns.map((c) => (
                  <th key={c.name} className="px-2 py-1 font-semibold">{c.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.slice(0, 200).map((row, ri) => (
                <tr key={ri} className="border-t border-gray-100 hover:bg-gray-50">
                  {row.map((cell, ci) => (
                    <td key={ci} className="px-2 py-1 tabular-nums text-gray-700">{fmtCell(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </WidgetBody>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
  return String(v);
}

function StatWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const result = q.data;
  const { value, label } = useMemo(() => computeStat(result, widget), [result, widget]);
  const color = thresholdColor(widget, typeof value === "number" ? value : null);
  return (
    <WidgetBody q={q} empty={!result}>
      <div className="flex h-full flex-col items-start justify-center px-1">
        <div className="text-3xl font-bold tabular-nums" style={{ color }}>{value ?? "—"}</div>
        {label && <div className="mt-1 text-[11px] text-gray-500">{label}</div>}
      </div>
    </WidgetBody>
  );
}

function computeStat(result: WidgetTableResult | undefined, widget: MonitorWidget): { value: string | number | null; label: string } {
  if (!result || result.rows.length === 0) return { value: null, label: "" };
  const vizStat = (widget.viz?.stat as { valueColumn?: string }) || {};
  const numCol = vizStat.valueColumn || result.columns.find((c) => c.type === "number")?.name;
  const unit = (widget.viz?.unit as string) || "";
  if (numCol) {
    const ci = result.columns.findIndex((c) => c.name === numCol);
    // Sum the column (typical for counts) or take the last value for time-series.
    const isTime = result.columns.some((c) => c.type === "datetime");
    let v: number;
    if (isTime) {
      v = Number(result.rows[result.rows.length - 1][ci]) || 0;
    } else {
      v = result.rows.reduce((a, r) => a + (Number(r[ci]) || 0), 0);
    }
    const formatted = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
    return { value: unit ? `${formatted}${unit}` : formatted, label: numCol };
  }
  return { value: result.rows.length, label: "rows" };
}

function thresholdColor(widget: MonitorWidget, value: number | null): string {
  if (value === null) return "#111827";
  const thresholds = (widget.viz?.thresholds as { op: string; value: number; color: string }[]) || [];
  const map: Record<string, string> = { red: "#dc2626", amber: "#d97706", green: "#16a34a", yellow: "#ca8a04" };
  for (const t of thresholds) {
    const ok = t.op === ">" ? value > t.value : t.op === "<" ? value < t.value : t.op === ">=" ? value >= t.value : t.op === "<=" ? value <= t.value : value === t.value;
    if (ok) return map[t.color] || t.color;
  }
  return "#111827";
}

function GaugeWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const { value } = useMemo(() => computeStat(q.data, widget), [q.data, widget]);
  const num = typeof value === "string" ? parseFloat(value) : value ?? 0;
  const max = (widget.viz?.max as number) || 100;
  const pct = Math.max(0, Math.min(1, (num || 0) / max));
  const color = thresholdColor(widget, num) || "#6366f1";
  const r = 42;
  const circ = Math.PI * r; // half circle
  return (
    <WidgetBody q={q} empty={!q.data}>
      <div className="flex h-full flex-col items-center justify-center">
        <svg viewBox="0 0 100 60" className="w-full max-w-[160px]">
          <path d="M 8 54 A 42 42 0 0 1 92 54" fill="none" stroke="#e5e7eb" strokeWidth="8" strokeLinecap="round" />
          <path d="M 8 54 A 42 42 0 0 1 92 54" fill="none" stroke={color} strokeWidth="8" strokeLinecap="round"
            strokeDasharray={`${pct * circ} ${circ}`} />
        </svg>
        <div className="-mt-3 text-2xl font-bold tabular-nums" style={{ color }}>{value ?? "—"}</div>
      </div>
    </WidgetBody>
  );
}

function AvailabilityWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const result = q.data;
  const meta = (result?.meta || {}) as { latest?: { ok?: boolean; status?: number; latency_ms?: number; error?: string }; uptime_pct?: number; sample_count?: number };
  const latest = meta.latest || {};
  // Distinguish "no probe data yet" from an actual outage so the widget doesn't show a
  // red "Down" before the first sample exists.
  const hasProbe = !!meta.latest && latest.ok !== undefined;
  const up = !!latest.ok;
  const series = useMemo(() => (result ? toChartData(result, { x: "timestamp", series: ["latency_ms"] }) : null), [result]);
  return (
    <WidgetBody q={q} empty={!result}>
      <div className="flex h-full flex-col">
        <div className="flex items-center gap-2">
          <span className={`h-2.5 w-2.5 rounded-full ${!hasProbe ? "bg-gray-300" : up ? "bg-emerald-500" : "bg-red-500"}`} />
          <span className={`text-sm font-semibold ${!hasProbe ? "text-gray-400" : up ? "text-emerald-600" : "text-red-600"}`}>{!hasProbe ? "No data" : up ? "Up" : "Down"}</span>
          {latest.status ? <span className="text-[11px] text-gray-400">HTTP {latest.status}</span> : null}
          {typeof latest.latency_ms === "number" && <span className="ml-auto text-[11px] tabular-nums text-gray-500">{latest.latency_ms} ms</span>}
        </div>
        <div className="mt-0.5 flex items-center gap-3 text-[11px] text-gray-500">
          <span>Uptime {meta.uptime_pct ?? "—"}%</span>
          <span>{meta.sample_count ?? 0} samples</span>
        </div>
        {latest.error && <div className="mt-0.5 truncate text-[10px] text-red-400" title={latest.error}>{latest.error}</div>}
        {series && series.data.length > 1 && (
          <div className="mt-1 min-h-0 flex-1">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series.data}>
                <XAxis dataKey="timestamp" hide />
                <YAxis hide />
                <Tooltip contentStyle={{ fontSize: 11 }} labelFormatter={() => ""} />
                <Area type="monotone" dataKey="latency_ms" stroke="#0ea5e9" fill="#0ea5e9" fillOpacity={0.15} strokeWidth={1.5} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </WidgetBody>
  );
}

function ListWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const result = q.data;
  const rows = useMemo(() => {
    if (!result) return [] as { label: string; value: string }[];
    const labelCol = result.columns.find((c) => c.type === "string" || c.type === "category")?.name || result.columns[0]?.name;
    const valueCol = result.columns.find((c) => c.type === "number")?.name;
    const li = result.columns.findIndex((c) => c.name === labelCol);
    const vi = valueCol ? result.columns.findIndex((c) => c.name === valueCol) : -1;
    return result.rows.slice(0, 50).map((r) => ({ label: String(r[li] ?? ""), value: vi >= 0 ? fmtCell(r[vi]) : "" }));
  }, [result]);
  const max = Math.max(1, ...rows.map((r) => Number(r.value.replace(/[^0-9.-]/g, "")) || 0));
  return (
    <WidgetBody q={q} empty={!rows.length}>
      <div className="h-full space-y-1 overflow-auto pr-1">
        {rows.map((r, i) => {
          const v = Number(r.value.replace(/[^0-9.-]/g, "")) || 0;
          return (
            <div key={i} className="text-[11px]">
              <div className="flex items-center justify-between">
                <span className="truncate text-gray-700">{r.label}</span>
                <span className="tabular-nums text-gray-500">{r.value}</span>
              </div>
              {r.value && <div className="mt-0.5 h-1 rounded bg-gray-100"><div className="h-1 rounded bg-brand/60" style={{ width: `${(v / max) * 100}%` }} /></div>}
            </div>
          );
        })}
      </div>
    </WidgetBody>
  );
}

function MapWidget({ widget, params, live }: WidgetProps) {
  const q = useWidgetData(widget, params, live);
  const result = q.data;
  // Expect a region column + count column (e.g. Resource Graph: summarize count() by location).
  const rows = useMemo(() => {
    if (!result) return [] as { region: string; count: number }[];
    const regCol = result.columns.find((c) => /location|region/i.test(c.name))?.name || result.columns[0]?.name;
    const cntCol = result.columns.find((c) => c.type === "number")?.name;
    const ri = result.columns.findIndex((c) => c.name === regCol);
    const ci = cntCol ? result.columns.findIndex((c) => c.name === cntCol) : -1;
    return result.rows.map((r) => ({ region: String(r[ri] ?? ""), count: ci >= 0 ? Number(r[ci]) || 0 : 1 }));
  }, [result]);
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <WidgetBody q={q} empty={!rows.length}>
      <div className="h-full space-y-1 overflow-auto pr-1">
        <div className="mb-1 text-[10px] uppercase tracking-wide text-gray-400">By region</div>
        {rows.map((r, i) => (
          <div key={i} className="text-[11px]">
            <div className="flex items-center justify-between">
              <span className="truncate text-gray-700">🌍 {r.region}</span>
              <span className="tabular-nums text-gray-500">{r.count}</span>
            </div>
            <div className="mt-0.5 h-1.5 rounded bg-gray-100"><div className="h-1.5 rounded bg-sky-500/70" style={{ width: `${(r.count / max) * 100}%` }} /></div>
          </div>
        ))}
      </div>
    </WidgetBody>
  );
}

function MarkdownWidget({ widget }: WidgetProps) {
  const md = (widget.viz?.markdown as string) || (widget.dataSource?.query as string) || "_Empty markdown widget — edit to add content._";
  return (
    <div className="prose-chat h-full overflow-auto text-sm">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
    </div>
  );
}

function ClockWidget({ widget }: WidgetProps) {
  const tz = (widget.viz?.timezone as string) || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = useNow();
  const time = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
  const date = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric" }).format(now);
  return (
    <div className="flex h-full flex-col items-center justify-center">
      <div className="text-3xl font-bold tabular-nums text-gray-800">{time}</div>
      <div className="mt-1 text-[11px] text-gray-500">{date}</div>
      <div className="text-[10px] text-gray-400">{tz}</div>
    </div>
  );
}

function useNow() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
export interface WidgetProps {
  widget: MonitorWidget;
  params?: Record<string, unknown>;
  live?: boolean;
}

export function WidgetRenderer({ widget, params, live = true }: WidgetProps) {
  switch (widget.type) {
    case "chart":
      return <ChartWidget widget={widget} params={params} live={live} />;
    case "table":
      return <TableWidget widget={widget} params={params} live={live} />;
    case "stat":
      return <StatWidget widget={widget} params={params} live={live} />;
    case "gauge":
      return <GaugeWidget widget={widget} params={params} live={live} />;
    case "availability":
      return <AvailabilityWidget widget={widget} params={params} live={live} />;
    case "list":
      return <ListWidget widget={widget} params={params} live={live} />;
    case "map":
      return <MapWidget widget={widget} params={params} live={live} />;
    case "markdown":
      return <MarkdownWidget widget={widget} params={params} live={live} />;
    case "clock":
      return <ClockWidget widget={widget} params={params} live={live} />;
    default:
      return <div className="flex h-full items-center justify-center text-xs text-gray-400">Unsupported widget: {widget.type}</div>;
  }
}
