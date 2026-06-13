/**
 * Monitor 2.0 — widget authoring UI: a side drawer editor (Data / Visualize / Settings)
 * plus two AI modals (build a single widget from natural language, and build a whole
 * dashboard from an Azure workload). Kept separate from MonitorView so the grid file
 * stays focused on layout.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type MonitorDatasourceDef,
  type MonitorWidget,
  type MonitorWidgetType,
  type Workload,
} from "../../api";
import { WidgetRenderer } from "./widgets";

const DEFAULT_SIZE: Record<string, { w: number; h: number }> = {
  stat: { w: 3, h: 2 }, chart: { w: 6, h: 4 }, table: { w: 6, h: 5 }, list: { w: 4, h: 5 },
  gauge: { w: 3, h: 3 }, availability: { w: 4, h: 3 }, map: { w: 6, h: 5 }, markdown: { w: 4, h: 3 }, clock: { w: 3, h: 2 },
};

const CHART_TYPES = ["line", "area", "bar", "stackedBar", "pie", "donut", "scatter"];

let _wcounter = 0;
export function newBlankWidget(type: MonitorWidgetType): MonitorWidget {
  const size = DEFAULT_SIZE[type] || { w: 4, h: 3 };
  const defaultKind =
    type === "availability" ? "web_ping" :
    type === "markdown" || type === "clock" ? "none" :
    type === "map" ? "resource_graph" : "app_telemetry";
  return {
    id: `w_new_${Date.now()}_${_wcounter++}`,
    title: type[0].toUpperCase() + type.slice(1),
    type,
    layout: { x: 0, y: 0, w: size.w, h: size.h },
    dataSource: { kind: defaultKind },
    transform: {},
    viz: type === "chart" ? { chartType: "line" } : {},
    refresh: { mode: "live", intervalSec: 60 },
    links: {},
    conditional: [],
  };
}

// ---------------------------------------------------------------------------
// Widget editor drawer
// ---------------------------------------------------------------------------
export function WidgetEditor({
  widget,
  onSave,
  onClose,
}: {
  widget: MonitorWidget;
  onSave: (w: MonitorWidget) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<MonitorWidget>(widget);
  const [tab, setTab] = useState<"data" | "viz" | "settings">("data");
  const catalogQ = useQuery({ queryKey: ["monitorDatasources"], queryFn: api.monitorDatasources });
  const connsQ = useQuery({ queryKey: ["adminConnections"], queryFn: api.adminConnections });
  const workbooksQ = useQuery({ queryKey: ["workbooks"], queryFn: api.workbooks });

  const dsDefs = catalogQ.data?.datasources ?? [];
  const curDs = dsDefs.find((d) => d.kind === draft.dataSource.kind);

  function patch(p: Partial<MonitorWidget>) {
    setDraft((d) => ({ ...d, ...p }));
  }
  function patchDs(p: Record<string, unknown>) {
    setDraft((d) => ({ ...d, dataSource: { ...d.dataSource, ...p } }));
  }
  function patchViz(p: Record<string, unknown>) {
    setDraft((d) => ({ ...d, viz: { ...d.viz, ...p } }));
  }

  const noData = draft.type === "markdown" || draft.type === "clock";

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Edit widget</div>
            <input
              value={draft.title}
              onChange={(e) => patch({ title: e.target.value })}
              className="mt-0.5 w-full rounded border-0 p-0 text-lg font-semibold text-gray-900 focus:ring-0"
              placeholder="Widget title"
            />
          </div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        {/* Live preview */}
        <div className="border-b border-gray-200 bg-gray-50 p-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Preview</div>
          <div className="h-44 rounded-lg border border-gray-200 bg-white p-2">
            <WidgetRenderer widget={draft} live={false} />
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 border-b border-gray-200 px-3 pt-2 text-sm">
          {(["data", "viz", "settings"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              disabled={t === "data" && noData}
              className={`rounded-t px-3 py-1.5 font-medium capitalize disabled:opacity-30 ${tab === t ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"}`}
            >
              {t === "viz" ? "Visualize" : t}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {tab === "data" && !noData && (
            <div className="space-y-3">
              <Field label="Data source">
                <select
                  value={draft.dataSource.kind}
                  onChange={(e) => patchDs({ kind: e.target.value })}
                  className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                >
                  {Object.entries(groupBy(dsDefs, (d) => d.group)).map(([group, items]) => (
                    <optgroup key={group} label={group}>
                      {items.map((d) => (
                        <option key={d.kind} value={d.kind}>{d.label}</option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                {curDs && <p className="mt-1 text-[11px] text-gray-500">{curDs.description}</p>}
              </Field>
              {curDs?.fields.map((f) => (
                <DsField
                  key={f.key}
                  def={f}
                  value={draft.dataSource[f.key]}
                  onChange={(v) => patchDs({ [f.key]: v })}
                  connections={connsQ.data?.connections ?? []}
                  workbooks={(workbooksQ.data?.workbooks ?? []).map((w) => ({ id: w.id, name: w.name }))}
                />
              ))}
            </div>
          )}

          {tab === "viz" && (
            <div className="space-y-3">
              <Field label="Widget type">
                <select
                  value={draft.type}
                  onChange={(e) => patch({ type: e.target.value as MonitorWidgetType })}
                  className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                >
                  {["stat", "chart", "table", "list", "gauge", "availability", "map", "markdown", "clock"].map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </Field>
              {draft.type === "chart" && (
                <Field label="Chart type">
                  <select
                    value={(draft.viz.chartType as string) || "line"}
                    onChange={(e) => patchViz({ chartType: e.target.value })}
                    className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                  >
                    {CHART_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </Field>
              )}
              {draft.type === "markdown" && (
                <Field label="Markdown">
                  <textarea
                    value={(draft.viz.markdown as string) || ""}
                    onChange={(e) => patchViz({ markdown: e.target.value })}
                    rows={8}
                    className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 font-mono text-xs"
                    placeholder="## Notes&#10;- runbook link"
                  />
                </Field>
              )}
              {draft.type === "clock" && (
                <Field label="Timezone (IANA)">
                  <input
                    value={(draft.viz.timezone as string) || ""}
                    onChange={(e) => patchViz({ timezone: e.target.value })}
                    placeholder="UTC, America/New_York, Europe/London"
                    className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                  />
                </Field>
              )}
              {(draft.type === "stat" || draft.type === "gauge") && (
                <Field label="Unit (optional)">
                  <input
                    value={(draft.viz.unit as string) || ""}
                    onChange={(e) => patchViz({ unit: e.target.value })}
                    placeholder="%  ms  GB"
                    className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                  />
                </Field>
              )}
              {(draft.type === "chart" || draft.type === "table" || draft.type === "stat") && (
                <>
                  <Field label="X / category column (optional)">
                    <input
                      value={(draft.transform.x as string) || ""}
                      onChange={(e) => setDraft((d) => ({ ...d, transform: { ...d.transform, x: e.target.value } }))}
                      placeholder="auto"
                      className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                    />
                  </Field>
                  <Field label="Series columns (comma-separated, optional)">
                    <input
                      value={Array.isArray(draft.transform.series) ? (draft.transform.series as string[]).join(", ") : ""}
                      onChange={(e) => setDraft((d) => ({ ...d, transform: { ...d.transform, series: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } }))}
                      placeholder="auto"
                      className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                    />
                  </Field>
                </>
              )}
            </div>
          )}

          {tab === "settings" && (
            <div className="space-y-3">
              <Field label="Auto-refresh">
                <div className="flex items-center gap-2">
                  <select
                    value={draft.refresh.mode}
                    onChange={(e) => patch({ refresh: { ...draft.refresh, mode: e.target.value as "live" | "manual" } })}
                    className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                  >
                    <option value="live">Live</option>
                    <option value="manual">Manual</option>
                  </select>
                  <select
                    value={draft.refresh.intervalSec}
                    disabled={draft.refresh.mode !== "live"}
                    onChange={(e) => patch({ refresh: { ...draft.refresh, intervalSec: Number(e.target.value) } })}
                    className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm disabled:opacity-50"
                  >
                    {[15, 30, 60, 300, 900, 3600].map((s) => (
                      <option key={s} value={s}>{s < 60 ? `${s}s` : `${s / 60}m`}</option>
                    ))}
                  </select>
                </div>
              </Field>
              <Field label="Threshold colors (stat/gauge)">
                <ThresholdsEditor
                  value={(draft.viz.thresholds as Threshold[]) || []}
                  onChange={(thresholds) => patchViz({ thresholds })}
                />
              </Field>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-gray-200 px-4 py-3">
          <button onClick={onClose} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => onSave(draft)} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Apply</button>
        </div>
      </div>
    </div>
  );
}

interface Threshold { op: string; value: number; color: string }
function ThresholdsEditor({ value, onChange }: { value: Threshold[]; onChange: (t: Threshold[]) => void }) {
  return (
    <div className="space-y-1.5">
      {value.map((t, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <select value={t.op} onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, op: e.target.value } : x)))} className="rounded border border-gray-200 px-1.5 py-1 text-xs">
            {[">", ">=", "<", "<=", "="].map((o) => <option key={o}>{o}</option>)}
          </select>
          <input type="number" value={t.value} onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, value: Number(e.target.value) } : x)))} className="w-20 rounded border border-gray-200 px-1.5 py-1 text-xs" />
          <select value={t.color} onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, color: e.target.value } : x)))} className="rounded border border-gray-200 px-1.5 py-1 text-xs">
            {["red", "amber", "green"].map((c) => <option key={c}>{c}</option>)}
          </select>
          <button onClick={() => onChange(value.filter((_, j) => j !== i))} className="text-gray-400 hover:text-red-500">✕</button>
        </div>
      ))}
      <button onClick={() => onChange([...value, { op: ">", value: 80, color: "red" }])} className="text-[11px] text-brand hover:underline">+ Add threshold</button>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-gray-400">{label}</span>
      {children}
    </label>
  );
}

function DsField({
  def,
  value,
  onChange,
  connections,
  workbooks,
}: {
  def: MonitorDatasourceDef["fields"][number];
  value: unknown;
  onChange: (v: unknown) => void;
  connections: { id: string; display_name: string }[];
  workbooks: { id: string; name: string }[];
}) {
  if (def.type === "connection") {
    return (
      <Field label={def.label}>
        <select value={(value as string) || ""} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
          <option value="">Default connection</option>
          {connections.map((c) => <option key={c.id} value={c.id}>{c.display_name}</option>)}
        </select>
      </Field>
    );
  }
  if (def.type === "workbook") {
    return (
      <Field label={def.label}>
        <select value={(value as string) || ""} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
          <option value="">Pick a workbook…</option>
          {workbooks.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </Field>
    );
  }
  if (def.type === "select") {
    return (
      <Field label={def.label}>
        <select value={(value as string) || def.default || ""} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
          {(def.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </Field>
    );
  }
  if (def.type === "kql") {
    return (
      <Field label={def.label}>
        <textarea value={(value as string) || ""} onChange={(e) => onChange(e.target.value)} rows={4} placeholder={def.placeholder} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 font-mono text-xs" />
      </Field>
    );
  }
  if (def.type === "text_list") {
    const arr = Array.isArray(value) ? (value as string[]) : [];
    return (
      <Field label={def.label}>
        <textarea
          value={arr.join("\n")}
          onChange={(e) => onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
          rows={3}
          placeholder={def.placeholder || "one per line"}
          className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 font-mono text-xs"
        />
      </Field>
    );
  }
  if (def.type === "json") {
    return (
      <Field label={def.label}>
        <textarea
          value={typeof value === "string" ? value : JSON.stringify(value ?? [], null, 2)}
          onChange={(e) => {
            try { onChange(JSON.parse(e.target.value)); } catch { onChange(e.target.value); }
          }}
          rows={5}
          placeholder='[{"name":"A","value":10}]'
          className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 font-mono text-xs"
        />
      </Field>
    );
  }
  return (
    <Field label={def.label}>
      <input
        type={def.type === "number" ? "number" : "text"}
        value={(value as string) ?? ""}
        onChange={(e) => onChange(def.type === "number" ? (e.target.value === "" ? undefined : Number(e.target.value)) : e.target.value)}
        placeholder={def.placeholder}
        className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
      />
    </Field>
  );
}

function groupBy<T>(arr: T[], key: (t: T) => string): Record<string, T[]> {
  const out: Record<string, T[]> = {};
  for (const item of arr) {
    const k = key(item);
    (out[k] ||= []).push(item);
  }
  return out;
}

// ---------------------------------------------------------------------------
// AI: build a single widget from natural language
// ---------------------------------------------------------------------------
export function AiWidgetModal({ onAdd, onClose }: { onAdd: (w: MonitorWidget) => void; onClose: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<MonitorWidget | null>(null);

  async function generate() {
    setBusy(true);
    setError("");
    try {
      const res = await api.aiBuildWidget(prompt);
      setPreview(res.widget);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to generate.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="✨ Build a widget with AI" onClose={onClose}>
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        rows={3}
        placeholder="e.g. Line chart of resource count by type across all subscriptions"
        className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm"
      />
      <div className="mt-2 flex gap-2">
        <button onClick={() => void generate()} disabled={busy || !prompt.trim()} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
          {busy ? "Generating…" : "Generate"}
        </button>
      </div>
      {error && <div className="mt-2 rounded bg-red-50 p-2 text-xs text-red-600">{error}</div>}
      {preview && (
        <div className="mt-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Preview · {preview.type}</div>
          <div className="h-44 rounded-lg border border-gray-200 bg-white p-2">
            <WidgetRenderer widget={preview} live={false} />
          </div>
          <div className="mt-2 flex justify-end gap-2">
            <button onClick={() => setPreview(null)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600">Discard</button>
            <button onClick={() => onAdd(preview)} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white">Add to dashboard</button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// AI: build a full dashboard from an Azure workload
// ---------------------------------------------------------------------------
export function BuildFromWorkloadModal({ onCreated, onClose }: { onCreated: (dashboardId: string) => void; onClose: () => void }) {
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const [workloadId, setWorkloadId] = useState("");
  const [archetype, setArchetype] = useState("full_stack");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [usedMemory, setUsedMemory] = useState(false);
  const [summary, setSummary] = useState("");
  const [brief, setBrief] = useState<Record<string, unknown> | null>(null);
  const [suggestions, setSuggestions] = useState<{ title: string; type: string; why?: string }[] | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const workloads: Workload[] = workloadsQ.data?.workloads ?? [];

  async function suggest() {
    setBusy(true); setError(""); setSuggestions(null);
    try {
      const res = await api.aiSuggestDashboard(workloadId, archetype);
      setSuggestions(res.widgets);
      setSummary(res.summary || "");
      setUsedMemory(!!res.used_memory);
      setBrief(res.design_brief || null);
      setPicked(new Set(res.widgets.map((_, i) => i)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to get suggestions.");
    } finally {
      setBusy(false);
    }
  }

  async function build() {
    if (!suggestions) return;
    setBusy(true); setError("");
    try {
      const selected = suggestions.filter((_, i) => picked.has(i));
      const res = await api.aiBuildDashboard(workloadId, selected, true, archetype);
      const id = res.saved_dashboard?.id;
      if (id) onCreated(id);
      else setError("Dashboard built but not saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to build dashboard.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="🏗️ Build a dashboard from an Azure workload" onClose={onClose} wide>
      <div className="grid gap-2 sm:grid-cols-[1fr_220px_auto] sm:items-end">
        <label className="flex-1">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-gray-400">Workload</span>
          <select value={workloadId} onChange={(e) => setWorkloadId(e.target.value)} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
            <option value="">Pick a workload…</option>
            {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
        </label>
        <label>
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-gray-400">Dashboard style</span>
          <select value={archetype} onChange={(e) => setArchetype(e.target.value)} className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm">
            <option value="full_stack">Full-stack observability</option>
            <option value="sre_live">SRE live operations</option>
            <option value="incident">Incident commander</option>
            <option value="security">Security & identity</option>
            <option value="cost_capacity">Cost & capacity</option>
            <option value="executive">Executive overview</option>
          </select>
        </label>
        <button onClick={() => void suggest()} disabled={busy || !workloadId} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
          {busy && !suggestions ? "Analyzing…" : "Suggest widgets"}
        </button>
      </div>
      {error && <div className="mt-2 rounded bg-red-50 p-2 text-xs text-red-600">{error}</div>}
      {suggestions && (
        <div className="mt-3">
          <div className="mb-1.5 flex items-center gap-2 text-xs text-gray-500">
            {summary && <span>{summary}</span>}
            {usedMemory && <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium text-violet-700">🧠 Used Architecture Memory</span>}
          </div>
          {brief && (
            <div className="mb-2 rounded-lg border border-gray-200 bg-gray-50 p-2 text-[11px] text-gray-600">
              <div className="font-semibold text-gray-700">Design brief</div>
              <div className="mt-0.5 line-clamp-2">{String(brief.purpose || brief.layout_strategy || "AI created a dashboard design brief before proposing widgets.")}</div>
              {Array.isArray(brief.dashboard_layers) && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {(brief.dashboard_layers as unknown[]).slice(0, 8).map((layer, i) => (
                    <span key={i} className="rounded-full bg-white px-1.5 py-0.5 text-[10px] text-gray-500">{String(layer)}</span>
                  ))}
                </div>
              )}
            </div>
          )}
          <div className="max-h-72 space-y-1.5 overflow-auto">
            {suggestions.map((s, i) => (
              <label key={i} className="flex cursor-pointer items-start gap-2 rounded-lg border border-gray-200 p-2 hover:bg-gray-50">
                <input
                  type="checkbox"
                  checked={picked.has(i)}
                  onChange={(e) => setPicked((prev) => { const n = new Set(prev); if (e.target.checked) n.add(i); else n.delete(i); return n; })}
                  className="mt-0.5"
                />
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-800">{s.title} <span className="text-[10px] font-normal text-gray-400">· {s.type}</span></div>
                  {s.why && <div className="text-[11px] text-gray-500">{s.why}</div>}
                </div>
              </label>
            ))}
          </div>
          <div className="mt-3 flex items-center justify-end gap-2">
            <span className="mr-auto text-[11px] text-gray-400">{picked.size} of {suggestions.length} selected</span>
            <button onClick={onClose} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600">Cancel</button>
            <button onClick={() => void build()} disabled={busy || picked.size === 0} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">
              {busy ? "Building, dry-running, and critiquing…" : `Build dashboard (${picked.size})`}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function Modal({ title, children, onClose, wide }: { title: string; children: React.ReactNode; onClose: () => void; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className={`w-full ${wide ? "max-w-2xl" : "max-w-lg"} rounded-2xl bg-white p-5 shadow-2xl`} onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-base font-semibold text-gray-900">{title}</h3>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}
