import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Responsive, WidthProvider } from "react-grid-layout";
import type { Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import {
  api,
  type MonitorOverview,
  type MonitorWidget,
  type WorkbookTile,
} from "../api";
import { formatTimestamp, formatRelativeFromNow, formatDuration } from "../utils/format";
import { Spinner } from "./chat/icons";
import { WidgetRenderer } from "./monitor/widgets";
import { WidgetEditor, AiWidgetModal, BuildFromWorkloadModal, newBlankWidget } from "./monitor/editor";

const ResponsiveGridLayout = WidthProvider(Responsive);

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  github: "GitHub Models",
  github_copilot: "GitHub Copilot",
  ollama: "Ollama",
  chatgpt: "ChatGPT Codex",
  azure_openai: "Azure OpenAI",
  claude: "Claude",
  gemini: "Google Gemini",
  grok: "Grok",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  lmstudio: "LM Studio",
};

// Donut/segment palette (cycled for provider/model breakdowns).
const PALETTE = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6", "#f43f5e"];

// Well-Architected pillar metadata (label + icon), mirrors the Assessments view.
const PILLAR_META: Record<string, { label: string; icon: string }> = {
  security: { label: "Security", icon: "🛡️" },
  reliability: { label: "Reliability", icon: "🔄" },
  cost: { label: "Cost", icon: "💰" },
  operations: { label: "Operations", icon: "⚙️" },
  performance: { label: "Performance", icon: "⚡" },
};
const PILLAR_ORDER = ["security", "reliability", "cost", "operations", "performance"];
const SEV_META: Record<string, { label: string; cls: string; dot: string }> = {
  critical: { label: "Critical", cls: "bg-red-100 text-red-700", dot: "bg-red-500" },
  error: { label: "Error", cls: "bg-orange-100 text-orange-700", dot: "bg-orange-500" },
  warning: { label: "Warning", cls: "bg-amber-100 text-amber-700", dot: "bg-amber-500" },
  info: { label: "Info", cls: "bg-sky-100 text-sky-700", dot: "bg-sky-500" },
};
function scoreHex(s: number | null | undefined): string {
  if (s == null) return "#9ca3af";
  if (s >= 80) return "#16a34a";
  if (s >= 50) return "#d97706";
  return "#dc2626";
}

const TILE_SEV_STYLE: Record<string, string> = {
  info: "border-gray-200 bg-white text-gray-700",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  error: "border-orange-200 bg-orange-50 text-orange-800",
  critical: "border-red-200 bg-red-50 text-red-800",
};

/** Workbook-powered dashboard tiles. Each tile shows the latest run of a tile-enabled
 *  workbook (a severity badge or a numeric metric) and can be re-run inline. */
function WorkbookTiles() {
  const qc = useQueryClient();
  const tilesQ = useQuery({
    queryKey: ["workbookTiles"],
    queryFn: api.workbookTiles,
    refetchInterval: 30_000,
  });
  const [busy, setBusy] = useState("");
  const tiles = tilesQ.data?.tiles ?? [];
  if (tiles.length === 0) return null;

  async function refresh(t: WorkbookTile) {
    setBusy(t.workbook_id);
    try {
      await api.runWorkbook(t.workbook_id, { params: {} });
      qc.invalidateQueries({ queryKey: ["workbookTiles"] });
    } catch {
      /* surfaced elsewhere */
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-gray-700">
        <span>📓 Workbook insights</span>
        <span className="text-xs font-normal text-gray-400">latest run per tile</span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {tiles.map((t) => (
          <div
            key={t.workbook_id}
            className={`rounded-xl border p-4 shadow-sm transition ${
              t.format === "severity" ? TILE_SEV_STYLE[t.severity ?? "info"] : "border-gray-200 bg-white"
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="text-xs font-medium text-gray-500">{t.label}</div>
              <button
                onClick={() => void refresh(t)}
                disabled={busy === t.workbook_id}
                title="Re-run"
                className="text-gray-300 hover:text-gray-600 disabled:opacity-50"
              >
                {busy === t.workbook_id ? "…" : "↻"}
              </button>
            </div>
            <div className="mt-1.5">
              {t.status === "never" ? (
                <span className="text-sm text-gray-400">Not run yet</span>
              ) : t.format === "number" ? (
                <span className="text-2xl font-bold text-gray-900">
                  {t.value != null ? String(t.value) : "—"}
                </span>
              ) : t.format === "severity" ? (
                <span className="text-lg font-semibold capitalize">{t.severity ?? "info"}</span>
              ) : (
                <span className="line-clamp-2 text-xs text-gray-600">{t.narrative}</span>
              )}
            </div>
            {t.narrative && t.format !== "text" && (
              <p className="mt-1 line-clamp-2 text-[11px] text-gray-500">{t.narrative}</p>
            )}
            {t.ran_at && (
              <p className="mt-1.5 text-[10px] text-gray-400">{formatRelativeFromNow(t.ran_at)}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Central monitoring dashboard. One aggregated, deep-linkable view of everything
 *  happening: activity volume, token usage, tool-call health, automations, and a feed. */
export function MonitorPanel() {
  const rootRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();
  // Enterprise controls: live/pause auto-refresh, selectable cadence, fullscreen NOC mode.
  const [live, setLive] = useState(true);
  const [intervalMs, setIntervalMs] = useState(15000);
  const [isFs, setIsFs] = useState(false);

  const q = useQuery({
    queryKey: ["monitor"],
    queryFn: api.monitor,
    refetchInterval: live ? intervalMs : false,
  });

  // Saved customizable dashboards (Azure-Dashboard style).
  const dashQ = useQuery({ queryKey: ["monitorDashboards"], queryFn: api.monitorDashboards });
  const dashboards = dashQ.data?.dashboards ?? [];
  const [activeId, setActiveId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Working copy of WIDGETS while editing / viewing the active dashboard.
  const [workWidgets, setWorkWidgets] = useState<MonitorWidget[]>(() => defaultWidgets());
  const [showAdd, setShowAdd] = useState(false);
  const [savingDash, setSavingDash] = useState(false);
  // Authoring surfaces.
  const [editorWidget, setEditorWidget] = useState<MonitorWidget | null>(null);
  const [showAiWidget, setShowAiWidget] = useState(false);
  const [showFromWorkload, setShowFromWorkload] = useState(false);

  // Resolve the active dashboard (explicit pick → default → first → built-in default).
  const active = useMemo(() => {
    if (activeId) return dashboards.find((d) => d.id === activeId) ?? null;
    return dashboards.find((d) => d.is_default) ?? dashboards[0] ?? null;
  }, [dashboards, activeId]);

  // Sync the working widgets when the active dashboard changes (unless mid-edit).
  useEffect(() => {
    if (editing) return;
    setWorkWidgets(active ? (active.widgets?.length ? active.widgets : defaultWidgets()) : defaultWidgets());
  }, [active, editing]);

  useEffect(() => {
    const onFs = () => setIsFs(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  function toggleFullscreen() {
    const el = rootRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    else void el.requestFullscreen().catch(() => {});
  }

  function exportJson() {
    if (!q.data) return;
    const blob = new Blob([JSON.stringify(q.data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `monitor-snapshot-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function nextY(): number {
    return workWidgets.reduce((m, w) => Math.max(m, w.layout.y + w.layout.h), 0);
  }
  function addBuiltinTile(tileId: string) {
    if (workWidgets.some((w) => w.tileId === tileId)) return;
    const def = TILE_BY_ID.get(tileId);
    if (!def) return;
    setWorkWidgets((prev) => [...prev, builtinWidget(tileId, { x: 0, y: nextY(), w: def.w, h: def.h })]);
    setShowAdd(false);
  }
  function addWidget(widget: MonitorWidget) {
    setWorkWidgets((prev) => [...prev, { ...widget, layout: { ...widget.layout, x: 0, y: nextY() } }]);
    setShowAdd(false);
    setShowAiWidget(false);
  }
  function removeWidget(widgetId: string) {
    setWorkWidgets((prev) => prev.filter((w) => w.id !== widgetId));
  }
  function applyEditedWidget(widget: MonitorWidget) {
    setWorkWidgets((prev) => {
      const exists = prev.some((w) => w.id === widget.id);
      return exists ? prev.map((w) => (w.id === widget.id ? widget : w)) : [...prev, { ...widget, layout: { ...widget.layout, y: nextY() } }];
    });
    setEditorWidget(null);
  }
  function openNewWidget(type: MonitorWidget["type"]) {
    setShowAdd(false);
    setEditorWidget(newBlankWidget(type));
  }

  async function saveDashboard(asNew: boolean) {
    setSavingDash(true);
    try {
      if (asNew || !active) {
        const name = window.prompt("Name this dashboard", active ? `${active.name} copy` : "My dashboard");
        if (!name) { setSavingDash(false); return; }
        const res = await api.upsertMonitorDashboard({
          name,
          widgets: workWidgets,
          is_default: dashboards.length === 0,
        });
        setActiveId(res.dashboard.id);
      } else {
        await api.upsertMonitorDashboard({ id: active.id, name: active.name, description: active.description, is_default: active.is_default, widgets: workWidgets });
      }
      await qc.invalidateQueries({ queryKey: ["monitorDashboards"] });
      setEditing(false);
    } finally {
      setSavingDash(false);
    }
  }

  async function newDashboard() {
    const name = window.prompt("Name the new dashboard", "My dashboard");
    if (!name) return;
    const res = await api.upsertMonitorDashboard({ name, widgets: defaultWidgets(), is_default: dashboards.length === 0 });
    await qc.invalidateQueries({ queryKey: ["monitorDashboards"] });
    setActiveId(res.dashboard.id);
    setEditing(true);
  }
  async function deleteDashboard() {
    if (!active) return;
    if (!window.confirm(`Delete dashboard “${active.name}”?`)) return;
    await api.deleteMonitorDashboard(active.id);
    setActiveId(null);
    await qc.invalidateQueries({ queryKey: ["monitorDashboards"] });
  }
  async function setDefault() {
    if (!active) return;
    await api.setDefaultMonitorDashboard(active.id);
    await qc.invalidateQueries({ queryKey: ["monitorDashboards"] });
  }
  async function onDashboardCreated(dashboardId: string) {
    setShowFromWorkload(false);
    await qc.invalidateQueries({ queryKey: ["monitorDashboards"] });
    setActiveId(dashboardId);
    setEditing(false);
  }

  function cancelEdit() {
    setEditing(false);
    setWorkWidgets(active ? (active.widgets?.length ? active.widgets : defaultWidgets()) : defaultWidgets());
  }

  const placedTileIds = new Set(workWidgets.filter((w) => w.type === "builtin").map((w) => w.tileId || ""));

  return (
    <div ref={rootRef} className="h-full overflow-y-auto bg-gradient-to-b from-gray-50 to-gray-100/60">
      <div className="space-y-4 p-6 lg:p-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-gray-900">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand/10 text-brand">
                <PulseIcon className="h-4 w-4" />
              </span>
              Monitor
              <span className="rounded-full bg-gray-900/5 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                Operations
              </span>
            </h1>
            <p className="mt-1 text-sm text-gray-500">
              Customizable observability — metrics, logs, queries, availability, and app health.
            </p>
          </div>
          <ControlBar
            live={live}
            onToggleLive={() => setLive((v) => !v)}
            intervalMs={intervalMs}
            onIntervalChange={setIntervalMs}
            fetching={q.isFetching}
            onRefresh={() => void q.refetch()}
            onExport={exportJson}
            canExport={!!q.data}
            isFullscreen={isFs}
            onToggleFullscreen={toggleFullscreen}
            generatedAt={q.data?.generated_at}
          />
        </div>

        {/* Dashboard toolbar: select / new / customize / save */}
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Dashboard</span>
          <select
            value={active?.id ?? "__default__"}
            onChange={(e) => { setEditing(false); setActiveId(e.target.value === "__default__" ? null : e.target.value); }}
            disabled={editing}
            className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm disabled:opacity-50"
          >
            {dashboards.length === 0 && <option value="__default__">Overview (built-in)</option>}
            {dashboards.map((d) => (
              <option key={d.id} value={d.id}>{d.name}{d.is_default ? " ★" : ""}</option>
            ))}
          </select>

          {!editing ? (
            <>
              <button onClick={() => setEditing(true)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50">✎ Customize</button>
              <button onClick={() => void newDashboard()} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50">+ New</button>
              <button onClick={() => setShowFromWorkload(true)} className="rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-sm font-medium text-violet-700 hover:bg-violet-100">🏗️ Build from workload</button>
              {active && !active.is_default && (
                <button onClick={() => void setDefault()} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">★ Set default</button>
              )}
              {active && (
                <button onClick={() => void deleteDashboard()} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50">Delete</button>
              )}
            </>
          ) : (
            <>
              <span className="flex items-center gap-1.5 rounded-lg bg-brand/10 px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-brand">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
                Editing
              </span>
              <div className="relative">
                <button onClick={() => setShowAdd((v) => !v)} className="rounded-lg border border-brand/40 bg-brand/5 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/10">+ Add widget</button>
                {showAdd && (
                  <AddWidgetMenu
                    placedTileIds={placedTileIds}
                    onAddWidgetType={openNewWidget}
                    onAddBuiltin={addBuiltinTile}
                    onAiWidget={() => { setShowAdd(false); setShowAiWidget(true); }}
                    onClose={() => setShowAdd(false)}
                  />
                )}
              </div>
              <button onClick={() => void saveDashboard(false)} disabled={savingDash} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">{savingDash ? "Saving…" : active ? "Save" : "Save as…"}</button>
              {active && <button onClick={() => void saveDashboard(true)} disabled={savingDash} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50">Save as…</button>}
              <button onClick={cancelEdit} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
              <span className="ml-auto flex items-center gap-1.5 text-[11px] text-gray-400">
                <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-500">Drag header</span> move
                <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-500">Drag edge / corner</span> resize
              </span>
            </>
          )}
        </div>

        {q.isLoading && <SkeletonGrid />}
        {q.isError && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-600">
            Failed to load monitoring data.
          </div>
        )}
        {q.data && (
          <DashboardGrid
            data={q.data}
            widgets={workWidgets}
            editing={editing}
            live={live}
            onLayoutChange={setWorkWidgets}
            onRemoveWidget={removeWidget}
            onEditWidget={(w) => setEditorWidget(w)}
          />
        )}
      </div>

      {editorWidget && (
        <WidgetEditor widget={editorWidget} onSave={applyEditedWidget} onClose={() => setEditorWidget(null)} />
      )}
      {showAiWidget && <AiWidgetModal onAdd={addWidget} onClose={() => setShowAiWidget(false)} />}
      {showFromWorkload && <BuildFromWorkloadModal onCreated={onDashboardCreated} onClose={() => setShowFromWorkload(false)} />}
    </div>
  );
}

/** Read-only "Stats" page — a fixed, at-a-glance view of the built-in app metrics
 *  (KPIs, system health, token usage & cost, provider mix, tool-call health, and
 *  activity trends). Reuses the Monitor overview snapshot and tile components, without
 *  the customizable-dashboard machinery. */
export function StatsPanel() {
  const [live, setLive] = useState(true);
  const q = useQuery({
    queryKey: ["monitor"],
    queryFn: api.monitor,
    refetchInterval: live ? 30000 : false,
  });

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-gray-50 to-gray-100/60">
      <div className="space-y-4 p-6 lg:p-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-gray-900">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand/10 text-brand">
                <PulseIcon className="h-4 w-4" />
              </span>
              Stats
              <span className="rounded-full bg-gray-900/5 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                Overview
              </span>
            </h1>
            <p className="mt-1 text-sm text-gray-500">
              At-a-glance usage and health — messages, tool calls, token usage &amp; cost, provider mix, and activity trends.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1.5 rounded-full border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-500 shadow-sm">
              <span className={`h-1.5 w-1.5 rounded-full ${q.isFetching ? "animate-pulse bg-sky-500" : live ? "bg-emerald-500" : "bg-gray-300"}`} />
              <span className="tabular-nums">{q.data?.generated_at ? formatTimestamp(q.data.generated_at) : "Loading…"}</span>
            </div>
            <button
              onClick={() => setLive((v) => !v)}
              title={live ? "Pause auto-refresh" : "Resume auto-refresh"}
              className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium shadow-sm transition ${
                live ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${live ? "animate-pulse bg-emerald-500" : "bg-gray-400"}`} />
              {live ? "Live" : "Paused"}
            </button>
            <button
              onClick={() => void q.refetch()}
              title="Refresh now"
              className="flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-sm transition hover:bg-gray-50"
            >
              <svg className={`h-3.5 w-3.5 ${q.isFetching ? "animate-spin" : ""}`} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M15.5 8a6 6 0 1 0 .5 4" strokeLinecap="round" />
                <path d="M16 3v5h-5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </div>

        {q.isLoading && <SkeletonGrid />}
        {q.isError && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-600">
            Failed to load stats.
          </div>
        )}
        {q.data && (
          <div className="space-y-4">
            {/* KPI row */}
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
              <div className="h-28"><TileKpiMessages data={q.data} /></div>
              <div className="h-28"><TileKpiToolCalls data={q.data} /></div>
              <div className="h-28"><TileKpiChats data={q.data} /></div>
              <div className="h-28"><TileKpiTaskRuns data={q.data} /></div>
              <div className="h-28"><TileKpiAgents data={q.data} /></div>
              <div className="h-28"><TileKpiApprovals data={q.data} /></div>
            </div>

            {/* Health + usage row */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-4">
              <div className="h-72"><TileHealth data={q.data} /></div>
              <div className="h-72"><TileTokenUsage data={q.data} /></div>
              <div className="h-72"><TileProviderMix data={q.data} /></div>
              <div className="h-72"><TileToolStatus data={q.data} /></div>
            </div>

            {/* Activity trends */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div className="h-80"><TileActivity24h data={q.data} /></div>
              <div className="h-80"><TileActivity14d data={q.data} /></div>
            </div>

            {/* Advanced — live ops, investigations, logs & tool health */}
            <div className="pt-1">
              <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-gray-700">
                <span>📜</span> Activity &amp; logs
              </h2>
              <div className="space-y-4">
                {q.data.live_turns.length > 0 && <TileLiveOps data={q.data} />}
                <TileInvestigations />
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div className="h-80"><TileTopChats data={q.data} /></div>
                  <div className="h-80"><TileTopTools data={q.data} /></div>
                </div>
                <div className="grid grid-cols-1 gap-4">
                  <div><TileActivityFeed data={q.data} /></div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Catalog popover for adding widgets in customize mode: new data widgets, AI, or
 *  built-in app-telemetry tiles. */
function AddWidgetMenu({
  placedTileIds,
  onAddWidgetType,
  onAddBuiltin,
  onAiWidget,
  onClose,
}: {
  placedTileIds: Set<string>;
  onAddWidgetType: (type: MonitorWidget["type"]) => void;
  onAddBuiltin: (tileId: string) => void;
  onAiWidget: () => void;
  onClose: () => void;
}) {
  const availableBuiltins = TILE_CATALOG.filter((t) => !placedTileIds.has(t.id));
  const widgetTypes: { type: MonitorWidget["type"]; label: string; icon: string }[] = [
    { type: "chart", label: "Chart", icon: "📈" },
    { type: "stat", label: "Stat / KPI", icon: "🔢" },
    { type: "table", label: "Table", icon: "▦" },
    { type: "gauge", label: "Gauge", icon: "◑" },
    { type: "availability", label: "Availability (ping)", icon: "🟢" },
    { type: "list", label: "List", icon: "≣" },
    { type: "map", label: "Map (by region)", icon: "🗺️" },
    { type: "markdown", label: "Markdown", icon: "✎" },
    { type: "clock", label: "Clock", icon: "🕐" },
  ];
  return (
    <>
      <div className="fixed inset-0 z-20" onClick={onClose} />
      <div className="absolute left-0 z-30 mt-1 max-h-[28rem] w-80 overflow-y-auto rounded-xl border border-gray-200 bg-white p-2 shadow-lg">
        <button onClick={onAiWidget} className="mb-1.5 block w-full rounded-lg bg-gradient-to-r from-brand/10 to-violet-100 px-2.5 py-2 text-left text-[13px] font-medium text-brand hover:from-brand/15">
          ✨ Build a widget with AI
        </button>
        <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">New data widget</div>
        <div className="grid grid-cols-2 gap-1">
          {widgetTypes.map((w) => (
            <button key={w.type} onClick={() => onAddWidgetType(w.type)} className="flex items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px] text-gray-700 hover:bg-brand/5 hover:text-brand">
              <span>{w.icon}</span>{w.label}
            </button>
          ))}
        </div>
        <div className="mt-1.5 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">App telemetry tiles</div>
        {availableBuiltins.length === 0 && <div className="px-2 py-1.5 text-center text-[11px] text-gray-400">All built-in tiles placed.</div>}
        {availableBuiltins.map((t) => (
          <button key={t.id} onClick={() => onAddBuiltin(t.id)} className="block w-full truncate rounded-md px-2 py-1.5 text-left text-[12px] text-gray-600 hover:bg-brand/5 hover:text-brand">
            {t.title} <span className="text-[10px] text-gray-400">· {t.group}</span>
          </button>
        ))}
      </div>
    </>
  );
}

/** Enterprise control bar: live/pause, refresh cadence, manual refresh, export, fullscreen. */
function ControlBar({
  live,
  onToggleLive,
  intervalMs,
  onIntervalChange,
  fetching,
  onRefresh,
  onExport,
  canExport,
  isFullscreen,
  onToggleFullscreen,
  generatedAt,
}: {
  live: boolean;
  onToggleLive: () => void;
  intervalMs: number;
  onIntervalChange: (ms: number) => void;
  fetching: boolean;
  onRefresh: () => void;
  onExport: () => void;
  canExport: boolean;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  generatedAt?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-1.5 rounded-full border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-500 shadow-sm">
        <span className={`h-1.5 w-1.5 rounded-full ${fetching ? "animate-pulse bg-sky-500" : live ? "bg-emerald-500" : "bg-gray-300"}`} />
        <span className="tabular-nums">{generatedAt ? formatTimestamp(generatedAt) : "Loading…"}</span>
      </div>

      {/* Live / Pause */}
      <button
        onClick={onToggleLive}
        title={live ? "Pause auto-refresh" : "Resume auto-refresh"}
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium shadow-sm transition ${
          live ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${live ? "animate-pulse bg-emerald-500" : "bg-gray-400"}`} />
        {live ? "Live" : "Paused"}
      </button>

      {/* Cadence */}
      <select
        value={intervalMs}
        onChange={(e) => onIntervalChange(Number(e.target.value))}
        disabled={!live}
        title="Auto-refresh cadence"
        className="rounded-full border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm disabled:opacity-50"
      >
        <option value={5000}>5s</option>
        <option value={15000}>15s</option>
        <option value={30000}>30s</option>
        <option value={60000}>60s</option>
      </select>

      {/* Manual refresh */}
      <button
        onClick={onRefresh}
        title="Refresh now"
        className="flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-sm transition hover:bg-gray-50"
      >
        <svg className={`h-3.5 w-3.5 ${fetching ? "animate-spin" : ""}`} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M15.5 8a6 6 0 1 0 .5 4" strokeLinecap="round" />
          <path d="M16 3v5h-5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {/* Export snapshot */}
      <button
        onClick={onExport}
        disabled={!canExport}
        title="Export dashboard snapshot (JSON)"
        className="flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-sm transition hover:bg-gray-50 disabled:opacity-50"
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M10 3v9m0 0l-3-3m3 3l3-3" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 15h12" strokeLinecap="round" />
        </svg>
      </button>

      {/* Fullscreen NOC mode */}
      <button
        onClick={onToggleFullscreen}
        title={isFullscreen ? "Exit fullscreen" : "Fullscreen (NOC mode)"}
        className="flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-sm transition hover:bg-gray-50"
      >
        {isFullscreen ? (
          <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M8 3v3a2 2 0 0 1-2 2H3m14 0h-3a2 2 0 0 1-2-2V3M3 12h3a2 2 0 0 1 2 2v3m9-5h-3a2 2 0 0 0-2 2v3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M3 7V4a1 1 0 0 1 1-1h3m6 0h3a1 1 0 0 1 1 1v3m0 6v3a1 1 0 0 1-1 1h-3m-6 0H4a1 1 0 0 1-1-1v-3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Customizable tile catalog — each tile is a self-contained card fed the
// MonitorOverview snapshot. Tiles are placed on a 12-column react-grid-layout.
// ---------------------------------------------------------------------------

/** Derived, dashboard-wide values some tiles share (health score, WoW deltas). */
function useMonitorDerived(data: MonitorOverview) {
  return useMemo(() => {
    const t = data.totals;
    const tc = data.tool_calls;
    const toolTotal = tc.succeeded + tc.failed;
    const successRate = toolTotal > 0 ? tc.succeeded / toolTotal : 1;
    const connectorRate = t.connectors > 0 ? t.connectors_ok / t.connectors : 1;
    const approvalsClear = t.pending_approvals === 0 ? 1 : 0.6;
    const health = Math.round((successRate * 0.5 + connectorRate * 0.3 + approvalsClear * 0.2) * 100);
    const d = data.activity_14d;
    const sum = (rows: typeof d, key: "messages" | "tool_calls" | "runs") => rows.reduce((a, r) => a + r[key], 0);
    const wowDelta = (key: "messages" | "tool_calls" | "runs") => {
      const p = sum(d.slice(0, 7), key);
      const l = sum(d.slice(7), key);
      if (p === 0) return l > 0 ? 100 : null;
      return Math.round(((l - p) / p) * 100);
    };
    return {
      health,
      successRate,
      connectorRate,
      msgTrend: data.activity_24h.map((h) => h.messages),
      toolTrend: data.activity_24h.map((h) => h.tool_calls),
      wow: { messages: wowDelta("messages"), tool_calls: wowDelta("tool_calls"), runs: wowDelta("runs") },
    };
  }, [data]);
}

/** Wraps a StatCard so it fills its grid cell. */
function KpiShell({ children }: { children: React.ReactNode }) {
  return <div className="h-full [&>button]:h-full [&>button]:w-full">{children}</div>;
}

function TileHealth({ data }: { data: MonitorOverview }) {
  const { health, successRate, connectorRate } = useMonitorDerived(data);
  const t = data.totals;
  return (
    <Panel fill title="System health">
      <div className="flex items-center gap-4">
        <HealthGauge value={health} />
        <ul className="space-y-1 text-[12px] text-gray-600">
          <li className="flex items-center gap-1.5"><Dot ok={successRate >= 0.9} /> Tool success {Math.round(successRate * 100)}%</li>
          <li className="flex items-center gap-1.5"><Dot ok={connectorRate >= 0.99} /> Connectors {t.connectors_ok}/{t.connectors}</li>
          <li className="flex items-center gap-1.5"><Dot ok={t.pending_approvals === 0} /> Approvals {t.pending_approvals === 0 ? "clear" : `${t.pending_approvals} pending`}</li>
        </ul>
      </div>
    </Panel>
  );
}

function TileKpiMessages({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const { msgTrend, wow } = useMonitorDerived(data);
  const t = data.totals;
  return <KpiShell><StatCard label="Messages" value={t.messages} sub={`+${t.messages_24h} today`} spark={msgTrend} color="#6366f1" delta={wow.messages} onClick={() => nav("/")} /></KpiShell>;
}
function TileKpiToolCalls({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const { toolTrend, wow } = useMonitorDerived(data);
  const t = data.totals;
  return <KpiShell><StatCard label="Tool calls" value={t.tool_calls} sub={`+${t.tool_calls_24h} today`} spark={toolTrend} color="#0ea5e9" delta={wow.tool_calls} onClick={() => nav("/admin/audit")} /></KpiShell>;
}
function TileKpiChats({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const t = data.totals;
  return <KpiShell><StatCard label="Chats" value={t.chats} sub={`${t.deep_investigations} deep`} color="#10b981" onClick={() => nav("/")} /></KpiShell>;
}
function TileKpiTaskRuns({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const { wow } = useMonitorDerived(data);
  const t = data.totals;
  return <KpiShell><StatCard label="Task runs" value={t.task_runs} sub={`${t.active_schedules}/${t.total_schedules} active`} color="#f59e0b" delta={wow.runs} onClick={() => nav("/automations/tasks")} /></KpiShell>;
}
function TileKpiAgents({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const t = data.totals;
  return <KpiShell><StatCard label="Agents" value={t.custom_agents} sub="custom agents" color="#8b5cf6" onClick={() => nav("/automations/agents")} /></KpiShell>;
}
function TileKpiApprovals({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const t = data.totals;
  return <KpiShell><StatCard label="Approvals" value={t.pending_approvals} sub={t.pending_approvals > 0 ? "pending" : "all clear"} color={t.pending_approvals > 0 ? "#f59e0b" : "#10b981"} accent={t.pending_approvals > 0} onClick={() => nav("/admin/audit")} /></KpiShell>;
}

function TileLiveOps({ data }: { data: MonitorOverview }) {
  return <div className="h-full [&>section]:h-full">{<LiveOps turns={data.live_turns} />}</div>;
}
function TileInvestigations() {
  return <div className="h-full [&>section]:h-full"><InvestigationsHistory /></div>;
}
function TileAzurePosture({ data }: { data: MonitorOverview }) {
  return <div className="h-full overflow-auto [&>section]:h-full"><AzurePosture data={data.azure_posture} /></div>;
}

function TileActivity24h({ data }: { data: MonitorOverview }) {
  return (
    <Panel fill title="Activity — last 24 hours">
      <AreaChart data={data.activity_24h} />
      <div className="mt-2 flex flex-wrap gap-4 text-[11px] text-gray-500">
        <Legend color="bg-brand" label="Messages" />
        <Legend color="bg-sky-400" label="Tool calls" />
      </div>
    </Panel>
  );
}

function TileToolStatus({ data }: { data: MonitorOverview }) {
  const tc = data.tool_calls;
  return (
    <Panel fill title="Tool calls by status">
      <Donut
        segments={Object.entries(tc.by_status).map(([k, v], i) => ({ label: k.replace(/_/g, " "), value: v, color: statusColor(k) ?? PALETTE[i % PALETTE.length] }))}
        centerLabel="calls"
        centerValue={Object.values(tc.by_status).reduce((a, b) => a + b, 0)}
      />
    </Panel>
  );
}

function TileActivity14d({ data }: { data: MonitorOverview }) {
  return (
    <Panel fill title="Activity — last 14 days">
      <ActivityChart data={data.activity_14d} />
      <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-gray-500">
        <Legend color="bg-brand" label="Messages" />
        <Legend color="bg-sky-400" label="Tools" />
        <Legend color="bg-emerald-400" label="Runs" />
      </div>
    </Panel>
  );
}

function TileTokenUsage({ data }: { data: MonitorOverview }) {
  return (
    <Panel fill title="Token usage">
      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <span className="text-2xl font-bold text-gray-900">{formatCompact(data.tokens.total)}</span>
          <span className="text-xs text-gray-400">{data.tokens.requests} requests</span>
        </div>
        <div className="flex h-2.5 overflow-hidden rounded-full bg-gray-100">
          <div className="bg-brand" style={{ width: `${pct(data.tokens.prompt, data.tokens.total)}%` }} title={`Prompt: ${data.tokens.prompt.toLocaleString()}`} />
          <div className="bg-sky-400" style={{ width: `${pct(data.tokens.completion, data.tokens.total)}%` }} title={`Completion: ${data.tokens.completion.toLocaleString()}`} />
        </div>
        <div className="flex justify-between text-[11px] text-gray-500">
          <span>Prompt {formatCompact(data.tokens.prompt)}</span>
          <span>Completion {formatCompact(data.tokens.completion)}</span>
        </div>
        <div className="flex items-baseline justify-between border-t border-gray-100 pt-2">
          <span className="text-[11px] text-gray-400">Estimated cost</span>
          <span className="text-sm font-semibold text-emerald-700" title="Estimated from token counts and standard per-model rates — for visibility, not billing.">{fmtUsd(data.tokens.cost_usd)}</span>
        </div>
      </div>
      <div className="mt-3 space-y-1.5">
        {data.tokens.by_model.length === 0 && <p className="text-xs text-gray-400">No usage recorded yet.</p>}
        {data.tokens.by_model.map((m) => (
          <div key={m.model} className="flex items-center gap-2">
            <span className="w-28 shrink-0 truncate font-mono text-[11px] text-gray-600" title={m.model}>{m.model}</span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
              <div className="h-full bg-brand/70" style={{ width: `${pct(m.total, data.tokens.by_model[0]?.total || 1)}%` }} />
            </div>
            <span className="w-12 shrink-0 text-right text-[11px] text-gray-500">{formatCompact(m.total)}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function TileProviderMix({ data }: { data: MonitorOverview }) {
  return (
    <Panel fill title="Provider mix">
      {data.providers.length === 0 ? (
        <p className="text-xs text-gray-400">No assistant turns recorded.</p>
      ) : (
        <Donut
          segments={data.providers.map((p, i) => ({ label: PROVIDER_LABELS[p.provider] ?? p.provider, value: p.count, color: PALETTE[i % PALETTE.length] }))}
          centerLabel="turns"
          centerValue={data.providers.reduce((a, b) => a + b.count, 0)}
        />
      )}
    </Panel>
  );
}

function TileTopChats({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  return (
    <Panel fill title="Most active chats" action={<LinkBtn onClick={() => nav("/")}>All chats →</LinkBtn>}>
      {data.top_chats.length === 0 ? (
        <p className="text-xs text-gray-400">No chats yet.</p>
      ) : (
        <div className="space-y-1">
          {data.top_chats.map((c) => {
            const max = data.top_chats[0].messages + data.top_chats[0].tool_calls || 1;
            const tot = c.messages + c.tool_calls;
            return (
              <button key={c.id} onClick={() => nav(`/c/${c.id}`)} className="group flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left transition hover:bg-gray-50">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-gray-700 group-hover:text-brand" title={c.title}>{c.title}</span>
                  <span className="mt-1 flex h-1.5 overflow-hidden rounded-full bg-gray-100">
                    <span className="bg-brand" style={{ width: `${pct(c.messages, max)}%` }} />
                    <span className="bg-sky-400" style={{ width: `${pct(c.tool_calls, max)}%` }} />
                  </span>
                </span>
                <span className="shrink-0 text-right text-[11px] text-gray-400"><span className="font-medium text-gray-600">{tot}</span> events<span className="block">{formatTimestamp(c.last_activity ?? undefined)}</span></span>
              </button>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function TileTopTools({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  const tc = data.tool_calls;
  return (
    <Panel fill title="Top tools" action={<ReadWritePill byKind={tc.by_kind} />}>
      <div className="space-y-1">
        {tc.top_tools.map((tool) => (
          <div key={tool.name} className="flex items-center gap-2">
            <span className="w-24 shrink-0 truncate font-mono text-[11px] text-gray-700" title={tool.name}>{tool.name}</span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100"><div className="h-full bg-sky-400/70" style={{ width: `${pct(tool.count, tc.top_tools[0]?.count || 1)}%` }} /></div>
            <span className="w-9 shrink-0 text-right text-[11px] text-gray-500">{tool.count}</span>
          </div>
        ))}
        {tc.top_tools.length === 0 && <p className="text-xs text-gray-400">No tool calls yet.</p>}
      </div>
      {tc.failed_recent.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-2">
          <div className="mb-1 text-[11px] font-medium text-red-500">Recent failures</div>
          {tc.failed_recent.map((f, i) => (
            <button key={i} onClick={() => f.chat_id && nav(`/c/${f.chat_id}`)} className="flex w-full items-center justify-between rounded px-1 py-0.5 text-[11px] text-gray-500 transition hover:bg-red-50">
              <span className="truncate font-mono text-gray-700">{f.tool_name}</span>
              <span className="shrink-0 text-gray-400">{formatTimestamp(f.created_at)}</span>
            </button>
          ))}
        </div>
      )}
      {data.tool_latency.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-2">
          <div className="mb-1 text-[11px] font-medium text-gray-400">Slowest tools (avg)</div>
          {data.tool_latency.map((l) => (
            <div key={l.name} className="flex items-center gap-2 py-0.5">
              <span className="w-24 shrink-0 truncate font-mono text-[11px] text-gray-700" title={l.name}>{l.name}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100"><div className="h-full bg-violet-400/70" style={{ width: `${pct(l.avg_ms, data.tool_latency[0]?.avg_ms || 1)}%` }} /></div>
              <span className="w-14 shrink-0 text-right text-[11px] text-gray-500">{formatDuration(l.avg_ms)}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function TileAutomations({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  return (
    <Panel fill title="Automations" action={<LinkBtn onClick={() => nav("/automations/tasks")}>Open →</LinkBtn>}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <div className="mb-2 flex flex-wrap gap-1.5">
            {Object.entries(data.automations.runs_by_status).map(([s, n]) => (<span key={s} className={`rounded-full px-2 py-0.5 text-[11px] ${runBadge(s)}`}>{s} · {n}</span>))}
            {data.automations.runs_total === 0 && <span className="text-xs text-gray-400">No runs yet.</span>}
          </div>
          <div className="text-[11px] font-medium text-gray-400">Recent runs</div>
          <div className="mt-1 space-y-0.5">
            {data.automations.recent_runs.slice(0, 6).map((r, i) => (
              <button key={i} onClick={() => (r.thread_id ? nav(`/c/${r.thread_id}`) : nav("/automations/tasks"))} className="flex w-full items-center gap-2 rounded px-1 py-1 text-[11px] transition hover:bg-gray-50">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${runDot(r.status)}`} />
                <span className="min-w-0 flex-1 truncate text-left text-gray-700" title={r.task_name ?? ""}>{r.task_name ?? "(deleted task)"}</span>
                {r.duration_ms != null && <span className="shrink-0 text-gray-400">{formatDuration(r.duration_ms)}</span>}
                <span className="shrink-0 text-gray-400">{formatTimestamp(r.started_at)}</span>
              </button>
            ))}
            {data.automations.recent_runs.length === 0 && <p className="text-xs text-gray-400">No runs recorded.</p>}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-medium text-gray-400">Upcoming</div>
          <div className="mt-1 space-y-0.5">
            {data.automations.upcoming.map((u) => (
              <button key={u.id} onClick={() => nav("/automations/tasks")} className="flex w-full items-center justify-between rounded px-1 py-1 text-[11px] transition hover:bg-gray-50">
                <span className="min-w-0 flex-1 truncate text-left text-gray-700" title={u.name}>{u.name}</span>
                <span className="shrink-0 text-brand">{formatRelativeFromNow(u.next_run_at ?? undefined)}</span>
              </button>
            ))}
            {data.automations.upcoming.length === 0 && <p className="text-xs text-gray-400">No schedules enabled.</p>}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function TileConnectors({ data }: { data: MonitorOverview }) {
  const nav = useNavigate();
  return (
    <Panel fill title="Connectors" action={<LinkBtn onClick={() => nav("/automations/connectors")}>Manage →</LinkBtn>}>
      {data.connectors_detail.length === 0 ? (
        <p className="text-xs text-gray-400">No connectors configured.</p>
      ) : (
        <div className="space-y-1">
          {data.connectors_detail.map((c) => (
            <button key={c.id} onClick={() => nav("/automations/connectors")} className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-gray-50">
              <span className="text-sm">{connectorIcon(c.type)}</span>
              <span className="min-w-0 flex-1"><span className="block truncate text-[13px] text-gray-700">{c.name}</span><span className="text-[10px] uppercase text-gray-400">{c.type}</span></span>
              <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${connStatusBadge(c.status)}`}>{c.status}</span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

function TileActivityFeed({ data }: { data: MonitorOverview }) {
  return <div className="h-full overflow-auto [&>section]:h-full"><ActivityFeed items={data.recent_activity} /></div>;
}
function TileWorkbooks() {
  return <div className="h-full overflow-auto"><WorkbookTiles /></div>;
}

interface TileDef {
  id: string;
  title: string;
  group: string;
  w: number;
  h: number;
  minW: number;
  minH: number;
  Render: React.ComponentType<{ data: MonitorOverview }>;
}

const TILE_CATALOG: TileDef[] = [
  { id: "health", title: "System health", group: "Overview", w: 3, h: 3, minW: 2, minH: 2, Render: TileHealth },
  { id: "kpi-messages", title: "KPI · Messages", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiMessages },
  { id: "kpi-toolcalls", title: "KPI · Tool calls", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiToolCalls },
  { id: "kpi-chats", title: "KPI · Chats", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiChats },
  { id: "kpi-taskruns", title: "KPI · Task runs", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiTaskRuns },
  { id: "kpi-agents", title: "KPI · Agents", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiAgents },
  { id: "kpi-approvals", title: "KPI · Approvals", group: "KPIs", w: 3, h: 2, minW: 2, minH: 2, Render: TileKpiApprovals },
  { id: "live-ops", title: "Live operations", group: "Activity", w: 12, h: 3, minW: 4, minH: 2, Render: TileLiveOps },
  { id: "investigations", title: "Recent investigations", group: "Activity", w: 12, h: 4, minW: 4, minH: 3, Render: TileInvestigations },
  { id: "azure-posture", title: "Azure posture", group: "Posture", w: 12, h: 6, minW: 6, minH: 4, Render: TileAzurePosture },
  { id: "activity-24h", title: "Activity (24h)", group: "Charts", w: 8, h: 4, minW: 4, minH: 3, Render: TileActivity24h },
  { id: "tool-status", title: "Tool calls by status", group: "Charts", w: 4, h: 4, minW: 3, minH: 3, Render: TileToolStatus },
  { id: "activity-14d", title: "Activity (14d)", group: "Charts", w: 4, h: 5, minW: 3, minH: 3, Render: TileActivity14d },
  { id: "token-usage", title: "Token usage", group: "Usage", w: 4, h: 5, minW: 3, minH: 3, Render: TileTokenUsage },
  { id: "provider-mix", title: "Provider mix", group: "Usage", w: 4, h: 5, minW: 3, minH: 3, Render: TileProviderMix },
  { id: "top-chats", title: "Most active chats", group: "Activity", w: 8, h: 4, minW: 4, minH: 3, Render: TileTopChats },
  { id: "top-tools", title: "Top tools", group: "Activity", w: 4, h: 6, minW: 3, minH: 3, Render: TileTopTools },
  { id: "automations", title: "Automations", group: "Automations", w: 8, h: 4, minW: 4, minH: 3, Render: TileAutomations },
  { id: "connectors", title: "Connectors", group: "Automations", w: 4, h: 4, minW: 3, minH: 3, Render: TileConnectors },
  { id: "activity-feed", title: "Recent activity", group: "Activity", w: 12, h: 5, minW: 4, minH: 3, Render: TileActivityFeed },
  { id: "workbook-tiles", title: "Workbook tiles", group: "Workbooks", w: 12, h: 3, minW: 4, minH: 2, Render: TileWorkbooks },
];

const TILE_BY_ID = new Map(TILE_CATALOG.map((t) => [t.id, t]));

/** The built-in default layout (used when no saved dashboard exists). Mirrors the
 *  original fixed Monitor arrangement. */
const DEFAULT_TILES = [
  { tileId: "health", x: 0, y: 0, w: 3, h: 3 },
  { tileId: "kpi-messages", x: 3, y: 0, w: 3, h: 2 },
  { tileId: "kpi-toolcalls", x: 6, y: 0, w: 3, h: 2 },
  { tileId: "kpi-chats", x: 9, y: 0, w: 3, h: 2 },
  { tileId: "kpi-taskruns", x: 3, y: 2, w: 3, h: 2 },
  { tileId: "kpi-agents", x: 6, y: 2, w: 3, h: 2 },
  { tileId: "kpi-approvals", x: 9, y: 2, w: 3, h: 2 },
  { tileId: "live-ops", x: 0, y: 4, w: 12, h: 3 },
  { tileId: "investigations", x: 0, y: 7, w: 12, h: 4 },
  { tileId: "azure-posture", x: 0, y: 11, w: 12, h: 6 },
  { tileId: "activity-24h", x: 0, y: 17, w: 8, h: 4 },
  { tileId: "tool-status", x: 8, y: 17, w: 4, h: 4 },
  { tileId: "activity-14d", x: 0, y: 21, w: 4, h: 5 },
  { tileId: "token-usage", x: 4, y: 21, w: 4, h: 5 },
  { tileId: "provider-mix", x: 8, y: 21, w: 4, h: 5 },
  { tileId: "top-chats", x: 0, y: 26, w: 8, h: 4 },
  { tileId: "top-tools", x: 8, y: 26, w: 4, h: 6 },
  { tileId: "automations", x: 0, y: 32, w: 8, h: 4 },
  { tileId: "connectors", x: 8, y: 32, w: 4, h: 4 },
  { tileId: "activity-feed", x: 0, y: 36, w: 12, h: 5 },
  { tileId: "workbook-tiles", x: 0, y: 41, w: 12, h: 3 },
];

/** Build a builtin-tile widget (wraps a TILE_CATALOG entry so legacy app-telemetry
 *  tiles render inside the generic widget grid). */
function builtinWidget(tileId: string, layout: { x: number; y: number; w: number; h: number }): MonitorWidget {
  return {
    id: `builtin_${tileId}`,
    title: TILE_BY_ID.get(tileId)?.title ?? tileId,
    type: "builtin",
    tileId,
    layout,
    dataSource: { kind: "none" },
    transform: {},
    viz: {},
    refresh: { mode: "manual", intervalSec: 60 },
    links: {},
    conditional: [],
  };
}

/** The built-in default arrangement (used when no saved dashboard exists), as widgets. */
function defaultWidgets(): MonitorWidget[] {
  return DEFAULT_TILES.map((t) => builtinWidget(t.tileId, { x: t.x, y: t.y, w: t.w, h: t.h }));
}

/** The customizable, react-grid-layout-based dashboard (widgets of any type). */
function DashboardGrid({
  data,
  widgets,
  editing,
  live,
  onLayoutChange,
  onRemoveWidget,
  onEditWidget,
}: {
  data: MonitorOverview;
  widgets: MonitorWidget[];
  editing: boolean;
  live: boolean;
  onLayoutChange: (next: MonitorWidget[]) => void;
  onRemoveWidget: (widgetId: string) => void;
  onEditWidget: (widget: MonitorWidget) => void;
}) {
  const visible = widgets.filter((w) => w.type !== "builtin" || TILE_BY_ID.has(w.tileId || ""));
  const layout: Layout[] = visible.map((w) => {
    const def = w.type === "builtin" ? TILE_BY_ID.get(w.tileId || "") : undefined;
    return {
      i: w.id,
      x: w.layout.x, y: w.layout.y, w: w.layout.w, h: w.layout.h,
      minW: def?.minW ?? 2, minH: def?.minH ?? 2,
    };
  });

  function handleChange(next: Layout[]) {
    const byId = new Map(visible.map((w) => [w.id, w]));
    onLayoutChange(
      next.map((l) => {
        const w = byId.get(l.i)!;
        return { ...w, layout: { x: l.x, y: l.y, w: l.w, h: l.h } };
      }),
    );
  }

  if (visible.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-gray-300 bg-white/60 p-10 text-center text-sm text-gray-400">
        This dashboard has no widgets. {editing ? "Use “Add widget” to place some." : "Click Customize to add widgets."}
      </div>
    );
  }

  return (
    <ResponsiveGridLayout
      className={`monitor-grid layout ${editing ? "monitor-grid--editing" : ""}`}
      layouts={{ lg: layout, md: layout, sm: layout, xs: layout, xxs: layout }}
      breakpoints={{ lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 }}
      cols={{ lg: 12, md: 12, sm: 6, xs: 4, xxs: 2 }}
      rowHeight={70}
      margin={[16, 16]}
      isDraggable={editing}
      isResizable={editing}
      resizeHandles={editing ? ["se", "e", "s"] : []}
      draggableHandle=".tile-drag"
      onLayoutChange={(l) => editing && handleChange(l)}
      compactType="vertical"
    >
      {visible.map((w) => {
        const def = w.type === "builtin" ? TILE_BY_ID.get(w.tileId || "") : undefined;
        const Render = def?.Render;
        const title = w.title || def?.title || w.type;
        const size = `${w.layout.w}×${w.layout.h}`;
        return (
          <div key={w.id} className="monitor-tile group/tile relative">
            {editing && (
              <div className="tile-drag absolute inset-x-0 top-0 z-20 flex h-7 cursor-move items-center justify-between rounded-t-xl bg-gradient-to-r from-brand to-indigo-500 px-2 text-[11px] font-medium text-white shadow-sm">
                <span className="flex items-center gap-1 truncate"><span className="opacity-80">⠿</span>{title}</span>
                <span className="flex items-center gap-0.5">
                  <span className="mr-1 rounded bg-white/20 px-1 py-px font-mono text-[10px] tabular-nums" title="Width × Height (grid units) — drag the edges or corner to resize">{size}</span>
                  {w.type !== "builtin" && (
                    <button
                      onClick={(e) => { e.stopPropagation(); onEditWidget(w); }}
                      onMouseDown={(e) => e.stopPropagation()}
                      className="rounded p-0.5 hover:bg-white/20"
                      title="Edit widget"
                    >✎</button>
                  )}
                  <button
                    onClick={(e) => { e.stopPropagation(); onRemoveWidget(w.id); }}
                    onMouseDown={(e) => e.stopPropagation()}
                    className="rounded p-0.5 hover:bg-white/20"
                    title="Remove widget"
                  >✕</button>
                </span>
              </div>
            )}
            <div className={`h-full ${editing ? "pointer-events-none overflow-hidden rounded-xl pt-7 ring-1 ring-brand/20" : ""}`}>
              {w.type === "builtin" && Render ? (
                <Render data={data} />
              ) : w.type === "builtin" ? (
                <div className="flex h-full items-center justify-center text-xs text-gray-400">Unknown tile</div>
              ) : (
                <WidgetCard widget={w} live={live} />
              )}
            </div>
          </div>
        );
      })}
    </ResponsiveGridLayout>
  );
}

/** A self-contained card frame around a data-bound widget renderer. */
function WidgetCard({ widget, live }: { widget: MonitorWidget; live: boolean }) {
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-gray-200/90 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-gray-100 bg-gradient-to-b from-gray-50/80 to-white px-3 py-1.5">
        <span className="truncate text-[11px] font-semibold uppercase tracking-wide text-gray-600">{widget.title}</span>
        {widget.refresh?.mode === "live" && live && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-emerald-600" title={`Live · ${widget.refresh.intervalSec}s`}>
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
            Live
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1 p-3">
        <WidgetRenderer widget={widget} live={live} />
      </div>
    </div>
  );
}


/** Confidence bar color band by score. */
function confColor(score: number): string {
  if (score >= 75) return "bg-emerald-500";
  if (score >= 50) return "bg-amber-500";
  if (score > 0) return "bg-orange-500";
  return "bg-gray-300";
}

/** Browsable history of recent deep investigations with a derived confidence score. */
function InvestigationsHistory() {
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["deepInvestigations"], queryFn: () => api.deepInvestigations(20) });
  const items = q.data?.investigations ?? [];
  if (q.isLoading) return null;
  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          <span className="text-base">🔬</span>
          Recent investigations
          <span className="rounded-full bg-gray-900/5 px-2 py-0.5 text-[10px] font-semibold text-gray-500">
            {items.length}
          </span>
        </span>
      }
    >
      {items.length === 0 ? (
        <p className="py-2 text-xs text-gray-400">No deep investigations recorded yet.</p>
      ) : (
        <div className="space-y-1.5">
          {items.map((it) => (
            <button
              key={it.message_id}
              onClick={() => nav(`/c/${it.chat_id}`)}
              className="flex w-full items-center gap-3 rounded-lg border border-gray-100 bg-white px-3 py-2 text-left transition hover:border-violet-300 hover:bg-violet-50/40"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium text-gray-800" title={it.title}>
                  {it.title}
                </span>
                <span className="block truncate text-[11px] text-gray-500" title={it.root_cause || it.summary}>
                  {it.root_cause || it.summary || "No conclusion recorded"}
                </span>
                <span className="mt-0.5 flex items-center gap-2 text-[10px] text-gray-400">
                  <span>{it.hypothesis_total} hypotheses</span>
                  {it.hypothesis_counts.validated > 0 && (
                    <span className="text-emerald-600">{it.hypothesis_counts.validated} validated</span>
                  )}
                  {it.agent_count > 0 && <span>· {it.agent_count} agents</span>}
                  <span>· {formatTimestamp(it.created_at)}</span>
                </span>
              </span>
              <span className="flex shrink-0 flex-col items-end gap-1">
                <span className="text-[11px] font-semibold tabular-nums text-gray-600">{it.confidence}%</span>
                <span className="h-1.5 w-16 overflow-hidden rounded-full bg-gray-100">
                  <span className={`block h-full ${confColor(it.confidence)}`} style={{ width: `${it.confidence}%` }} />
                </span>
                <span className="text-[9px] uppercase tracking-wide text-gray-400">confidence</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

/** Live operations — agent turns in flight right now, with current tool + elapsed. */
function LiveOps({ turns }: { turns: MonitorOverview["live_turns"] }) {
  const nav = useNavigate();
  const fmtElapsed = (s: number) => {
    const sec = Math.max(0, Math.round(s));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    const r = sec % 60;
    return r ? `${m}m ${r}s` : `${m}m`;
  };
  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            {turns.length > 0 && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
            )}
            <span className={`relative inline-flex h-2 w-2 rounded-full ${turns.length > 0 ? "bg-emerald-500" : "bg-gray-300"}`} />
          </span>
          Live operations
          <span className="rounded-full bg-gray-900/5 px-2 py-0.5 text-[10px] font-semibold text-gray-500">
            {turns.length} active
          </span>
        </span>
      }
    >
      {turns.length === 0 ? (
        <p className="py-2 text-xs text-gray-400">No agent turns in flight right now.</p>
      ) : (
        <div className="space-y-1.5">
          {turns.map((tr) => (
            <button
              key={tr.chat_id}
              onClick={() => nav(`/c/${tr.chat_id}`)}
              className="flex w-full items-center gap-3 rounded-lg border border-gray-100 bg-white px-3 py-2 text-left transition hover:border-brand/30 hover:bg-brand/[0.03]"
            >
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                  tr.kind === "deep" ? "bg-violet-100 text-violet-700" : "bg-sky-100 text-sky-700"
                }`}
              >
                {tr.kind === "deep" ? "Deep" : "Chat"}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium text-gray-800" title={tr.title}>
                  {tr.title}
                </span>
                <span className="block truncate text-[11px] text-gray-500">
                  {tr.current_tool ? (
                    <span className="inline-flex items-center gap-1">
                      <Spinner className="h-3 w-3 text-brand" />
                      Running <span className="font-mono text-gray-600">{tr.current_tool}</span>
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1">
                      <Spinner className="h-3 w-3 text-brand" />
                      Thinking…
                    </span>
                  )}
                  {tr.tool_count > 0 && <span className="ml-2 text-gray-400">· {tr.tool_count} tools</span>}
                </span>
              </span>
              <span className="shrink-0 font-mono text-[11px] tabular-nums text-gray-400">
                {fmtElapsed(tr.elapsed_s)}
              </span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

/** Searchable, action-filterable recent-activity feed with deep links. */
function ActivityFeed({ items }: { items: MonitorOverview["recent_activity"] }) {
  const nav = useNavigate();
  const [search, setSearch] = useState("");
  const [action, setAction] = useState("all");

  const actions = useMemo(
    () => Array.from(new Set(items.map((a) => a.action))).sort(),
    [items],
  );
  const filtered = useMemo(() => {
    const ql = search.trim().toLowerCase();
    return items.filter((a) => {
      if (action !== "all" && a.action !== action) return false;
      if (!ql) return true;
      return (
        a.action.toLowerCase().includes(ql) ||
        (a.target ?? "").toLowerCase().includes(ql) ||
        (a.actor_id ?? "").toLowerCase().includes(ql) ||
        (a.model ?? "").toLowerCase().includes(ql)
      );
    });
  }, [items, search, action]);

  return (
    <Panel
      title="Recent activity"
      action={
        <div className="flex items-center gap-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search…"
            className="w-32 rounded-lg border border-gray-200 px-2 py-1 text-[11px] focus:w-44 focus:outline-none focus:ring-1 focus:ring-brand/30"
          />
          <select
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="rounded-lg border border-gray-200 px-1.5 py-1 text-[11px] text-gray-600"
            title="Filter by action"
          >
            <option value="all">All actions</option>
            {actions.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <LinkBtn onClick={() => nav("/admin/audit")}>Full audit log →</LinkBtn>
        </div>
      }
    >
      <div className="divide-y divide-gray-100">
        {filtered.map((a) => {
          const link = activityLink(a.action, a.chat_id);
          const Row = (
            <div className="flex items-center gap-3 py-1.5 text-xs">
              <span className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] ${actionBadge(a.action)}`}>{a.action}</span>
              <span className="min-w-0 flex-1 truncate text-gray-400" title={a.target ?? ""}>{a.target ?? ""}</span>
              {a.model && (
                <span className="hidden shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 sm:inline">
                  {(a.provider && PROVIDER_LABELS[a.provider]) || a.provider} · {a.model}
                </span>
              )}
              <span className="hidden shrink-0 text-gray-400 sm:inline">{a.actor_id}</span>
              <span className="shrink-0 text-gray-400">{formatTimestamp(a.created_at)}</span>
            </div>
          );
          return link ? (
            <button key={a.id} onClick={() => nav(link)} className="block w-full rounded text-left transition hover:bg-gray-50">
              {Row}
            </button>
          ) : (
            <div key={a.id}>{Row}</div>
          );
        })}
        {filtered.length === 0 && (
          <p className="py-2 text-xs text-gray-400">
            {items.length === 0 ? "No activity recorded yet." : "No activity matches your filters."}
          </p>
        )}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Azure posture
// ---------------------------------------------------------------------------
function AzurePosture({ data }: { data: MonitorOverview["azure_posture"] }) {
  const nav = useNavigate();
  if (!data) return null;
  const assessed = data.assessed_count > 0;
  const sevEntries = (["critical", "error", "warning", "info"] as const).filter(
    (s) => data.findings_by_severity[s] > 0,
  );

  return (
    <Panel
      title="Azure posture — Well-Architected"
      action={<LinkBtn onClick={() => nav("/assessments")}>Assessments →</LinkBtn>}
    >
      {!assessed ? (
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <span className="text-3xl">🛡️</span>
          <p className="text-sm text-gray-500">
            {data.workload_total > 0
              ? `${data.workload_total} workload${data.workload_total === 1 ? "" : "s"} defined, none assessed yet.`
              : "No workloads assessed yet."}
          </p>
          <button onClick={() => nav("/assessments")} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90">
            Run an assessment →
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
          {/* Overall score + pillar rings */}
          <div className="lg:col-span-5">
            <div className="flex items-center gap-4">
              <PostureGauge value={data.avg_score} />
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Average score</div>
                <div className="mt-1 text-[12px] text-gray-600">
                  {data.assessed_count} of {data.workload_total} workload{data.workload_total === 1 ? "" : "s"} assessed
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-600">{data.open_findings} open findings</span>
                  {data.new_findings > 0 && (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-700">+{data.new_findings} new (drift)</span>
                  )}
                </div>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-5 gap-2">
              {PILLAR_ORDER.map((p) => (
                <PillarRing key={p} pillar={p} score={data.pillar_avgs[p]} />
              ))}
            </div>
          </div>

          {/* Open findings by severity */}
          <div className="lg:col-span-3">
            <div className="text-[11px] font-medium text-gray-400">Open findings by severity</div>
            <div className="mt-2 space-y-1.5">
              {sevEntries.length === 0 && <p className="text-xs text-emerald-600">No open findings 🎉</p>}
              {sevEntries.map((s) => {
                const n = data.findings_by_severity[s];
                const max = Math.max(...Object.values(data.findings_by_severity), 1);
                return (
                  <button key={s} onClick={() => nav("/assessments")} className="flex w-full items-center gap-2 text-left">
                    <span className={`h-2 w-2 shrink-0 rounded-sm ${SEV_META[s].dot}`} />
                    <span className="w-16 shrink-0 text-[11px] capitalize text-gray-600">{SEV_META[s].label}</span>
                    <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
                      <span className={`block h-full ${SEV_META[s].dot}`} style={{ width: `${pct(n, max)}%` }} />
                    </span>
                    <span className="w-7 shrink-0 text-right text-[11px] font-medium text-gray-700">{n}</span>
                  </button>
                );
              })}
            </div>
            <div className="mt-3 text-[11px] font-medium text-gray-400">Top failing controls</div>
            <div className="mt-1 space-y-0.5">
              {data.top_failing.map((c, i) => (
                <button key={i} onClick={() => nav("/assessments")} className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left transition hover:bg-gray-50">
                  <span className={`shrink-0 rounded px-1 py-0.5 text-[9px] font-medium ${SEV_META[c.severity]?.cls ?? ""}`}>{PILLAR_META[c.pillar]?.icon ?? ""}</span>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-gray-700" title={c.title}>{c.title}</span>
                  {c.resources > 0 && <span className="shrink-0 text-[10px] text-gray-400">{c.resources} res</span>}
                </button>
              ))}
              {data.top_failing.length === 0 && <p className="text-[11px] text-gray-400">None.</p>}
            </div>
          </div>

          {/* Worst-scoring workloads */}
          <div className="lg:col-span-4">
            <div className="text-[11px] font-medium text-gray-400">Workloads by score (worst first)</div>
            <div className="mt-2 space-y-1">
              {data.workloads.map((w) => (
                <button
                  key={w.run_id}
                  onClick={() => nav(`/assessments/${w.run_id}`)}
                  className="group flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left transition hover:bg-gray-50"
                >
                  <span
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[11px] font-bold text-white"
                    style={{ background: scoreHex(w.overall_score) }}
                  >
                    {w.overall_score ?? "—"}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12px] font-medium text-gray-700 group-hover:text-brand" title={w.workload_name}>{w.workload_name}</span>
                    <span className="flex items-center gap-1 text-[10px] text-gray-400">
                      {w.pillars.map((p) => PILLAR_META[p]?.icon ?? "").join(" ")}
                      {w.failed > 0 && <span className="text-orange-500">· {w.failed} failed</span>}
                    </span>
                  </span>
                  <svg className="h-3.5 w-3.5 shrink-0 text-gray-300 opacity-0 transition group-hover:opacity-100" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M7 5l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

/** Big circular gauge for the overall average posture score. */
function PostureGauge({ value }: { value: number | null }) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const v = value ?? 0;
  const offset = c * (1 - v / 100);
  const color = scoreHex(value);
  return (
    <svg viewBox="0 0 80 80" className="h-20 w-20 -rotate-90">
      <circle cx="40" cy="40" r={r} fill="none" stroke="#f1f5f9" strokeWidth="8" />
      <circle cx="40" cy="40" r={r} fill="none" stroke={color} strokeWidth="8" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={offset} className="transition-all duration-700" />
      <text x="40" y="40" transform="rotate(90 40 40)" textAnchor="middle" dominantBaseline="central" className="fill-gray-800 text-[18px] font-bold">
        {value ?? "—"}
      </text>
    </svg>
  );
}

/** Compact pillar score ring with an icon + label. */
function PillarRing({ pillar, score }: { pillar: string; score: number | undefined }) {
  const meta = PILLAR_META[pillar] ?? { label: pillar, icon: "📋" };
  const has = score != null;
  const r = 16;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - (score ?? 0) / 100);
  return (
    <div className="flex flex-col items-center gap-1" title={`${meta.label}: ${has ? score : "n/a"}`}>
      <svg viewBox="0 0 44 44" className="h-11 w-11 -rotate-90">
        <circle cx="22" cy="22" r={r} fill="none" stroke="#f1f5f9" strokeWidth="5" />
        {has && (
          <circle cx="22" cy="22" r={r} fill="none" stroke={scoreHex(score)} strokeWidth="5" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={offset} />
        )}
        <text x="22" y="22" transform="rotate(90 22 22)" textAnchor="middle" dominantBaseline="central" className="fill-gray-700 text-[11px] font-bold">
          {has ? score : "—"}
        </text>
      </svg>
      <span className="text-[9px] text-gray-500">{meta.icon} {meta.label}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------
function StatCard({
  label,
  value,
  sub,
  spark,
  color,
  accent,
  delta,
  onClick,
}: {
  label: string;
  value: number | string;
  sub?: string;
  spark?: number[];
  color: string;
  accent?: boolean;
  delta?: number | null;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`group relative overflow-hidden rounded-xl border bg-white p-3 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md ${
        accent ? "border-amber-300 ring-1 ring-amber-100" : "border-gray-200"
      }`}
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="text-2xl font-bold" style={{ color: accent ? "#d97706" : "#111827" }}>
            {typeof value === "number" ? value.toLocaleString() : value}
          </div>
          <div className="text-[11px] font-medium text-gray-500">{label}</div>
          {sub && <div className="mt-0.5 text-[10px] text-gray-400">{sub}</div>}
        </div>
        {delta != null ? (
          <TrendBadge delta={delta} />
        ) : (
          <span className="opacity-0 transition group-hover:opacity-100">
            <svg className="h-3.5 w-3.5 text-gray-300" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M7 5l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        )}
      </div>
      {spark && spark.some((v) => v > 0) && (
        <div className="pointer-events-none mt-1 h-7">
          <Sparkline values={spark} color={color} />
        </div>
      )}
    </button>
  );
}

/** Week-over-week delta pill (▲/▼ %, neutral when flat). Title explains the comparison. */
function TrendBadge({ delta }: { delta: number }) {
  const up = delta > 0;
  const flat = delta === 0;
  const cls = flat
    ? "bg-gray-100 text-gray-500"
    : up
      ? "bg-emerald-50 text-emerald-600"
      : "bg-red-50 text-red-600";
  return (
    <span
      title="Last 7 days vs previous 7 days"
      className={`flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${cls}`}
    >
      <span aria-hidden>{flat ? "—" : up ? "▲" : "▼"}</span>
      {Math.abs(delta)}%
    </span>
  );
}

function Panel({
  title,
  children,
  className = "",
  action,
  fill,
}: {
  title?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  action?: React.ReactNode;
  fill?: boolean;
}) {
  return (
    <section className={`rounded-xl border border-gray-200 bg-white p-4 shadow-sm ${fill ? "flex h-full flex-col" : ""} ${className}`}>
      {(title || action) && (
        <div className="mb-3 flex shrink-0 items-center justify-between">
          {title && <h2 className="text-sm font-semibold text-gray-700">{title}</h2>}
          {action}
        </div>
      )}
      {fill ? <div className="min-h-0 flex-1 overflow-auto">{children}</div> : children}
    </section>
  );
}

function LinkBtn({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button onClick={onClick} className="text-[11px] font-medium text-brand transition hover:underline">
      {children}
    </button>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-sm ${color}`} />
      {label}
    </span>
  );
}

function Dot({ ok }: { ok: boolean }) {
  return <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-amber-500"}`} />;
}

function ReadWritePill({ byKind }: { byKind: Record<string, number> }) {
  const read = byKind.read ?? 0;
  const write = byKind.write ?? 0;
  if (read + write === 0) return null;
  return (
    <span className="flex items-center gap-1.5 text-[10px]">
      <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-green-700">{read} read</span>
      {write > 0 && <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-amber-700">{write} write</span>}
    </span>
  );
}

/** Circular health gauge (SVG arc). */
function HealthGauge({ value }: { value: number }) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - value / 100);
  const color = value >= 85 ? "#10b981" : value >= 60 ? "#f59e0b" : "#ef4444";
  return (
    <svg viewBox="0 0 80 80" className="h-20 w-20 -rotate-90">
      <circle cx="40" cy="40" r={r} fill="none" stroke="#f1f5f9" strokeWidth="8" />
      <circle
        cx="40"
        cy="40"
        r={r}
        fill="none"
        stroke={color}
        strokeWidth="8"
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={offset}
        className="transition-all duration-700"
      />
      <text x="40" y="40" transform="rotate(90 40 40)" textAnchor="middle" dominantBaseline="central" className="fill-gray-800 text-[18px] font-bold">
        {value}
      </text>
    </svg>
  );
}

/** Donut chart from labelled segments, with a center total. */
function Donut({
  segments,
  centerLabel,
  centerValue,
}: {
  segments: { label: string; value: number; color: string }[];
  centerLabel: string;
  centerValue: number;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  const r = 30;
  const c = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 80 80" className="h-24 w-24 -rotate-90">
        <circle cx="40" cy="40" r={r} fill="none" stroke="#f1f5f9" strokeWidth="10" />
        {segments.map((s, i) => {
          const frac = s.value / total;
          const dash = c * frac;
          const seg = (
            <circle
              key={i}
              cx="40"
              cy="40"
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth="10"
              strokeDasharray={`${dash} ${c - dash}`}
              strokeDashoffset={-acc}
            />
          );
          acc += dash;
          return seg;
        })}
        <text x="40" y="40" transform="rotate(90 40 40)" textAnchor="middle" dominantBaseline="central" className="fill-gray-900 text-[14px] font-bold">
          {formatCompact(centerValue)}
        </text>
      </svg>
      <div className="min-w-0 flex-1 space-y-1">
        {segments.slice(0, 5).map((s, i) => (
          <div key={i} className="flex items-center gap-2 text-[11px]">
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: s.color }} />
            <span className="min-w-0 flex-1 truncate capitalize text-gray-600">{s.label}</span>
            <span className="shrink-0 text-gray-400">{s.value}</span>
          </div>
        ))}
        {segments.length === 0 && <span className="text-[11px] text-gray-400">{centerLabel}: none</span>}
      </div>
    </div>
  );
}

/** Smooth filled area chart for the 24h hourly activity (two series). */
function AreaChart({ data }: { data: MonitorOverview["activity_24h"] }) {
  const W = 600;
  const H = 130;
  const pad = 4;
  const n = data.length;
  const max = Math.max(1, ...data.map((d) => Math.max(d.messages, d.tool_calls)));
  const x = (i: number) => pad + (i / Math.max(1, n - 1)) * (W - 2 * pad);
  const y = (v: number) => H - pad - (v / max) * (H - 2 * pad);
  const path = (key: "messages" | "tool_calls") => {
    const pts = data.map((d, i) => `${x(i)},${y(d[key])}`);
    return `M ${pts.join(" L ")}`;
  };
  const area = (key: "messages" | "tool_calls") => {
    const pts = data.map((d, i) => `${x(i)},${y(d[key])}`);
    return `M ${x(0)},${H - pad} L ${pts.join(" L ")} L ${x(n - 1)},${H - pad} Z`;
  };
  return (
    <svg viewBox={`0 0 ${W} ${H + 14}`} className="h-36 w-full" preserveAspectRatio="none">
      <defs>
        <linearGradient id="g-msg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#6366f1" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#6366f1" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="g-tool" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#0ea5e9" stopOpacity="0.2" />
          <stop offset="100%" stopColor="#0ea5e9" stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0.25, 0.5, 0.75].map((f) => (
        <line key={f} x1={pad} y1={H * f} x2={W - pad} y2={H * f} stroke="#f1f5f9" strokeWidth="1" />
      ))}
      <path d={area("tool_calls")} fill="url(#g-tool)" />
      <path d={area("messages")} fill="url(#g-msg)" />
      <path d={path("tool_calls")} fill="none" stroke="#0ea5e9" strokeWidth="1.5" />
      <path d={path("messages")} fill="none" stroke="#6366f1" strokeWidth="1.5" />
      {data.map((d, i) =>
        i % 3 === 0 ? (
          <text key={i} x={x(i)} y={H + 11} textAnchor="middle" className="fill-gray-400 text-[8px]">
            {new Date(d.hour).toLocaleTimeString([], { hour: "numeric" }).replace(" ", "")}
          </text>
        ) : null,
      )}
    </svg>
  );
}

/** Tiny inline sparkline for stat cards. */
function Sparkline({ values, color }: { values: number[]; color: string }) {
  const W = 100;
  const H = 28;
  const max = Math.max(1, ...values);
  const n = values.length;
  const x = (i: number) => (i / Math.max(1, n - 1)) * W;
  const y = (v: number) => H - (v / max) * (H - 2) - 1;
  const line = values.map((v, i) => `${x(i)},${y(v)}`).join(" L ");
  const fill = `M 0,${H} L ${values.map((v, i) => `${x(i)},${y(v)}`).join(" L ")} L ${W},${H} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-full w-full" preserveAspectRatio="none">
      <path d={fill} fill={color} opacity="0.12" />
      <path d={`M ${line}`} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

/** Stacked vertical bar chart of daily activity (messages / tool calls / runs). */
function ActivityChart({ data }: { data: MonitorOverview["activity_14d"] }) {
  const CHART_H = 120;
  const max = Math.max(1, ...data.map((d) => d.messages + d.tool_calls + d.runs));
  const px = (v: number) => Math.round((v / max) * CHART_H);
  return (
    <div className="flex items-end gap-1" style={{ height: CHART_H + 16 }}>
      {data.map((d) => {
        const total = d.messages + d.tool_calls + d.runs;
        const day = new Date(`${d.date}T00:00:00Z`).toLocaleDateString([], { weekday: "short" })[0];
        return (
          <div key={d.date} className="group flex flex-1 flex-col items-center gap-1">
            <div
              className="flex w-full flex-col-reverse overflow-hidden rounded-sm transition group-hover:opacity-80"
              style={{ height: CHART_H }}
              title={`${d.date}: ${d.messages} msgs · ${d.tool_calls} tools · ${d.runs} runs`}
            >
              <div className="w-full bg-brand" style={{ height: px(d.messages) }} />
              <div className="w-full bg-sky-400" style={{ height: px(d.tool_calls) }} />
              <div className="w-full bg-emerald-400" style={{ height: px(d.runs) }} />
              {total === 0 && <div className="mt-auto h-px w-full bg-gray-100" />}
            </div>
            <span className="text-[9px] text-gray-400">{day}</span>
          </div>
        );
      })}
    </div>
  );
}

function SkeletonGrid() {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        <div className="h-28 animate-pulse rounded-xl border bg-gray-100" />
        <div className="grid grid-cols-3 gap-4 lg:col-span-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-28 animate-pulse rounded-xl border bg-gray-100" />
          ))}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="h-52 animate-pulse rounded-xl border bg-gray-100 lg:col-span-2" />
        <div className="h-52 animate-pulse rounded-xl border bg-gray-100" />
      </div>
    </div>
  );
}

function PulseIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M2 10h3l2-5 3 11 2.5-7 1.5 3H18" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function pct(value: number, total: number): number {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (value / total) * 100));
}

function formatCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

function fmtUsd(v: number): string {
  if (!v) return "$0.00";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function statusColor(status: string): string | null {
  switch (status) {
    case "succeeded":
      return "#10b981";
    case "failed":
    case "rejected":
      return "#ef4444";
    case "awaiting_approval":
      return "#f59e0b";
    case "running":
    case "pending":
      return "#0ea5e9";
    default:
      return null;
  }
}

function runBadge(status: string): string {
  switch (status) {
    case "succeeded":
      return "bg-emerald-100 text-emerald-700";
    case "failed":
      return "bg-red-100 text-red-700";
    case "running":
    case "queued":
      return "bg-sky-100 text-sky-700";
    case "skipped":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-gray-100 text-gray-600";
  }
}

function runDot(status: string): string {
  switch (status) {
    case "succeeded":
      return "bg-emerald-400";
    case "failed":
      return "bg-red-400";
    case "running":
    case "queued":
      return "bg-sky-400";
    default:
      return "bg-gray-300";
  }
}

function connStatusBadge(status: string): string {
  switch (status) {
    case "ok":
      return "bg-emerald-100 text-emerald-700";
    case "error":
      return "bg-red-100 text-red-700";
    default:
      return "bg-gray-100 text-gray-500";
  }
}

function connectorIcon(type: string): string {
  switch (type) {
    case "teams":
      return "💬";
    case "outlook":
      return "✉️";
    case "jira":
      return "🟦";
    case "grafana":
      return "📊";
    default:
      return "🔌";
  }
}

function actionBadge(action: string): string {
  if (action.startsWith("tool.")) return "bg-sky-50 text-sky-600";
  if (action.startsWith("chat.")) return "bg-indigo-50 text-indigo-600";
  if (action.startsWith("agent.")) return "bg-violet-50 text-violet-600";
  if (action.startsWith("task.")) return "bg-amber-50 text-amber-600";
  if (action.startsWith("connector.") || action.startsWith("connection.")) return "bg-teal-50 text-teal-600";
  if (action.startsWith("approval.")) return "bg-orange-50 text-orange-600";
  return "bg-gray-100 text-gray-500";
}

/** Map an audit action (+ optional chat id) to a deep link, or null if none. */
function activityLink(action: string, chatId: string | null): string | null {
  if (chatId && (action.startsWith("tool.") || action.startsWith("chat.") || action.startsWith("command."))) {
    return `/c/${chatId}`;
  }
  if (action.startsWith("agent.")) return "/automations/agents";
  if (action.startsWith("task.")) return "/automations/tasks";
  if (action.startsWith("connector.")) return "/automations/connectors";
  if (action.startsWith("connection.")) return "/admin/tenants";
  if (action.startsWith("settings.")) return "/admin/settings";
  if (action.startsWith("approval.")) return "/admin/audit";
  if (action.startsWith("llm.")) return "/admin/providers";
  if (chatId) return `/c/${chatId}`;
  return null;
}
