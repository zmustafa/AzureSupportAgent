import { useEffect, useMemo, useRef, useState, Fragment } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useSearchParams } from "react-router-dom";
import {
  api,
  type AmbaCell,
  type AmbaCoverage,
  type AmbaGap,
  type AmbaGroup,
  type AmbaRow,
} from "../api";
import { formatError } from "../utils/format";
import { TrendChart } from "./TrendChart";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { AllResourcesTab } from "./AllResourcesTab";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { DensityToggle } from "./DensityToggle";
import { CoverageHistory, coverageRunsKey } from "./CoverageHistory";
import { PdfGeneratingOverlay } from "./PdfGeneratingOverlay";
import { PageIntro } from "./PageIntro";
import { PAGE_INTROS } from "../help/content";
import { isRefreshing, startBackgroundRefresh, takeRefreshError, useBackgroundRefresh } from "../utils/backgroundRefresh";
import { Skeleton, useDebounced } from "../utils/perf";
import { MonitoringCoverageFleet } from "./coverage/MonitoringCoverageFleet";
import { RunCleanup } from "./cleanup/RunCleanup";

const SEV_CLS: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  error: "bg-orange-100 text-orange-700",
  warning: "bg-amber-100 text-amber-700",
  info: "bg-sky-100 text-sky-700",
};

const CAT_CLS: Record<string, string> = {
  availability: "bg-emerald-50 text-emerald-700",
  performance: "bg-violet-50 text-violet-700",
  security: "bg-rose-50 text-rose-700",
};

function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function StatusMark({ status }: { status: string }) {
  if (status === "present")
    return <span title="Present" className="text-green-600">✓</span>;
  if (status === "misconfigured")
    return <span title="Misconfigured" className="text-amber-500">⚠</span>;
  return <span title="Missing" className="text-red-500">✗</span>;
}

function Donut({ pct }: { pct: number }) {
  const r = 34;
  const c = 2 * Math.PI * r;
  const dash = (pct / 100) * c;
  const color = pct >= 80 ? "#16a34a" : pct >= 50 ? "#d97706" : "#dc2626";
  return (
    <svg viewBox="0 0 80 80" className="h-20 w-20">
      <circle cx="40" cy="40" r={r} fill="none" stroke="#e5e7eb" strokeWidth="8" />
      <circle
        cx="40" cy="40" r={r} fill="none" stroke={color} strokeWidth="8" strokeLinecap="round"
        strokeDasharray={`${dash} ${c - dash}`} transform="rotate(-90 40 40)"
      />
      <text x="40" y="45" textAnchor="middle" className="fill-gray-900 text-[18px] font-semibold">
        {pct}%
      </text>
    </svg>
  );
}

type CoverageMainView = "coverage" | "fleet" | "cleanup";

function CoverageViewTabs({ value, onChange }: { value: CoverageMainView; onChange: (value: CoverageMainView) => void }) {
  return <div className="flex items-center gap-1 border-b bg-white px-5 pt-2">
    <button onClick={() => onChange("coverage")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "coverage" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>📡 Coverage</button>
    <button onClick={() => onChange("fleet")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "fleet" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>🚀 Fleet</button>
    <button onClick={() => onChange("cleanup")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "cleanup" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>🧹 Cleanup</button>
  </div>;
}

// MP1 — per-group coverage matrix body. Small groups render inline (full inline-expand detail).
// Large groups (> VIRT_THRESHOLD rows) virtualize the body via an internal-scroll windowed
// <tbody> with spacer rows: only the visible window of rows is in the DOM, keeping the sticky
// first column + dynamic alert columns. In virtualized mode a row click opens the detail drawer
// (which carries the same per-cell detail the inline expander shows) so rows stay fixed-height.
const VIRT_THRESHOLD = 60;
function AmbaMatrixBody({ group, expandedRow, setExpandedRow, setDrawer }: {
  group: AmbaGroup;
  expandedRow: string | null;
  setExpandedRow: (v: string | null) => void;
  setDrawer: (v: { row: AmbaRow; cell: AmbaCell } | null) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualize = group.rows.length > VIRT_THRESHOLD;
  const rowVirt = useVirtualizer({
    count: group.rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 41,
    overscan: 10,
  });
  const vItems = rowVirt.getVirtualItems();
  const padTop = vItems.length ? vItems[0].start : 0;
  const padBottom = vItems.length ? rowVirt.getTotalSize() - vItems[vItems.length - 1].end : 0;

  const cols = group.recommended_alerts;
  const mainRow = (row: AmbaRow) => (
    <tr key={row.resource_id} className="border-t hover:bg-gray-50">
      <td className="sticky left-0 bg-white px-3 py-2">
        <button
          onClick={() => (virtualize ? setDrawer({ row, cell: row.cells[0] }) : setExpandedRow(expandedRow === row.resource_id ? null : row.resource_id))}
          className="text-left"
        >
          <div className="font-medium text-gray-800">{row.resource_name}</div>
          <div className="text-[10px] text-gray-400">{row.resource_group}</div>
        </button>
      </td>
      {cols.map((ra) => {
        const cell = row.cells.find((c) => c.alert_key === ra.key);
        if (!cell) return <td key={ra.key} className="px-2 py-2 text-center text-gray-300">–</td>;
        return (
          <td key={ra.key} className="px-2 py-2 text-center">
            <button onClick={() => setDrawer({ row, cell })} className="text-base"><StatusMark status={cell.status} /></button>
          </td>
        );
      })}
    </tr>
  );

  return (
    <div ref={scrollRef} className="overflow-auto border-t" style={virtualize ? { maxHeight: "60vh" } : undefined}>
      <table className="w-full text-xs">
        <thead className="sticky top-0 z-10 bg-gray-50 text-gray-500">
          <tr>
            <th className="sticky left-0 z-20 bg-gray-50 px-3 py-2 text-left font-medium">Resource</th>
            {cols.map((a) => (
              <th key={a.key} className="px-2 py-2 text-center font-medium align-bottom" title={a.name}>
                <div className="mx-auto w-[80px] whitespace-normal break-words leading-tight">{a.name}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {virtualize ? (
            <>
              {padTop > 0 && <tr style={{ height: padTop }} aria-hidden />}
              {vItems.map((vi) => mainRow(group.rows[vi.index]))}
              {padBottom > 0 && <tr style={{ height: padBottom }} aria-hidden />}
            </>
          ) : (
            group.rows.map((row) => {
              const isExp = expandedRow === row.resource_id;
              return (
                <Fragment key={row.resource_id}>
                  {mainRow(row)}
                  {isExp && (
                    <tr className="border-t bg-gray-50/60">
                      <td colSpan={cols.length + 1} className="px-4 py-2">
                        <div className="space-y-1">
                          {row.cells.map((c) => (
                            <div key={c.alert_key} className="flex flex-wrap items-center gap-2 text-[11px]">
                              <StatusMark status={c.status} />
                              <span className={`rounded px-1.5 py-0.5 ${CAT_CLS[c.amba_category] ?? "bg-gray-100"}`}>{c.amba_category}</span>
                              <span className={`rounded px-1.5 py-0.5 ${SEV_CLS[c.severity] ?? ""}`}>{c.severity}</span>
                              <span className="text-gray-700">{c.alert_name}</span>
                              <span className="text-gray-400">
                                recommended {c.recommended.metric} {c.recommended.operator} {c.recommended.threshold ?? "—"}{c.recommended.unit}
                                {c.observed.observed_thresholds?.length ? ` · observed ${c.observed.observed_thresholds.join(", ")}` : ""}
                                {c.observed.rule_name ? ` · rule ${c.observed.rule_name}${c.observed.enabled === false ? " (disabled)" : ""}` : ""}
                              </span>
                              {c.status !== "present" && (
                                <button onClick={() => setDrawer({ row, cell: c })} className="text-indigo-600 hover:underline">details</button>
                              )}
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}

export function MonitoringCoveragePanel() {
  const qc = useQueryClient();
  const [storedMainView, setMainView] = usePersistedState<CoverageMainView | "overview">("azsup.amba.view", "coverage");
  const mainView: CoverageMainView = storedMainView === "overview" ? "coverage" : storedMainView;
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.amba.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState<string>("azsup.amba.workloadId", "");
  useWorkloadDeepLink(setScopeKind, setWorkloadId);
  const [subId, setSubId] = usePersistedState<string>("azsup.amba.subId", "");
  const [subName, setSubName] = usePersistedState<string>("azsup.amba.subName", "");
  const [connId, setConnId] = usePersistedState<string>("azsup.amba.connId", "");
  const mp0 = useRef(new URLSearchParams(window.location.search)).current;
  const [query, setQuery] = useState(mp0.get("q") || "");
  const dQuery = useDebounced(query, 150);
  const [catFilter, setCatFilter] = useState(mp0.get("cat") || "all");
  const [sevFilter, setSevFilter] = useState(mp0.get("sev") || "all");
  const [statusFilter, setStatusFilter] = useState(mp0.get("status") || "all");
  const [tab, setTab] = useState<"coverage" | "all">(mp0.get("tab") === "all" ? "all" : "coverage");
  const [density, setDensity] = usePersistedState<"compact" | "expanded">("azsup.amba.density", "expanded");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<{ row: AmbaRow; cell: AmbaCell } | null>(null);
  const [iacView, setIacView] = useState<{ title: string; text: string; format: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const pdfAbortRef = useRef<AbortController | null>(null);
  const [ticketFor, setTicketFor] = useState<string | null>(null);

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );

  // Default the workload to the demo one when present so the page is never empty.
  const workloads = workloadsQ.data?.workloads ?? [];
  const effectiveWorkloadId =
    scopeKind === "workload"
      ? workloadId || workloads.find((w) => w.id === "demo-amba-coverage")?.id || workloads[0]?.id || ""
      : "";

  const params =
    scopeKind === "workload"
      ? { workload_id: effectiveWorkloadId, connection_id: connId }
      : { subscription_id: subId, connection_id: connId };
  const scopeReady = scopeKind === "workload" ? !!effectiveWorkloadId : !!subId;
  const scopeKey = `${scopeKind}:${effectiveWorkloadId || subId}:${connId}`;
  const [loadedScope, setLoadedScope] = usePersistedState("azsup.amba.loadedScope", "");
  const enabled = scopeReady && loadedScope === scopeKey;

  // Background refresh (per-scope) — survives scope switches + navigation. "Refreshing…"
  // shows ONLY for the scope actually refreshing; other scopes show "↻ Refresh now".
  const refreshKey = `amba:${scopeKey}`;
  const refreshVersion = useBackgroundRefresh();
  const refreshing = isRefreshing(refreshKey);

  const covQ = useQuery({
    queryKey: ["amba", scopeKind, effectiveWorkloadId, subId, connId],
    queryFn: () => api.ambaCoverage(params),
    enabled,
    staleTime: 5 * 60 * 1000,
  });
  const data: AmbaCoverage | undefined = enabled ? covQ.data : undefined;

  // Coverage-% trend over time (loads with the coverage data).
  const trendQ = useQuery({
    queryKey: ["amba-trend", scopeKind, effectiveWorkloadId, subId, connId],
    queryFn: () => api.coverageTrend("amba", params),
    enabled,
    staleTime: 5 * 60 * 1000,
  });

  function loadCoverage() {
    setLoadedScope(scopeKey);
  }

  async function doRefresh() {
    if (refreshing || !scopeReady) return;
    setMsg(null);
    setLoadedScope(scopeKey);
    const p = params;
    const dataKey = ["amba", scopeKind, effectiveWorkloadId, subId, connId] as const;
    const trendKey = ["amba-trend", scopeKind, effectiveWorkloadId, subId, connId] as const;
    // Fire-and-forget: keeps running even if the user switches scope or navigates away; the
    // cache update via the shared queryClient lands whenever the scan finishes.
    startBackgroundRefresh(refreshKey, async () => {
      const fresh = await api.refreshAmba(p);
      qc.setQueryData(dataKey, fresh);
      await qc.invalidateQueries({ queryKey: trendKey });
      await qc.invalidateQueries({ queryKey: coverageRunsKey("amba", scopeKind, effectiveWorkloadId, subId) });
    });
  }

  // Surface an error from a background refresh that finished (possibly while away).
  useEffect(() => {
    if (!refreshing) {
      const err = takeRefreshError(refreshKey);
      if (err) setMsg({ text: err, ok: false });
    }
  }, [refreshVersion, refreshKey, refreshing]);

  function cellVisible(c: AmbaCell): boolean {
    if (catFilter !== "all" && c.amba_category !== catFilter) return false;
    if (sevFilter !== "all" && c.severity !== sevFilter) return false;
    if (statusFilter !== "all" && c.status !== statusFilter) return false;
    return true;
  }
  function rowVisible(r: AmbaRow): boolean {
    const q = dQuery.trim().toLowerCase();
    if (q && !(`${r.resource_name} ${r.resource_group}`.toLowerCase().includes(q))) return false;
    return r.cells.some(cellVisible);
  }

  const allGaps = data?.gaps ?? [];

  function download(text: string, name: string) {
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function downloadPdf() {
    if (busy === "pdf" || !scopeReady) return;
    const controller = new AbortController();
    pdfAbortRef.current = controller;
    setBusy("pdf"); setMsg(null);
    try {
      const blob = await api.coverageReportPdf("amba", params, controller.signal);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `monitoring-coverage-${data?.scope_name || "report"}.pdf`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      if ((e as { name?: string } | null)?.name !== "AbortError") setMsg({ text: formatError(e), ok: false });
    } finally { pdfAbortRef.current = null; setBusy(""); }
  }

  function cancelPdf() {
    pdfAbortRef.current?.abort();
  }

  async function saveEvidence() {
    if (busy === "evidence" || !scopeReady) return;
    setBusy("evidence"); setMsg(null);
    try {
      const r = await api.coverageSaveEvidence("amba", params);
      setMsg({ text: `Saved to Evidence Locker: ${r.snapshot.name}`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally { setBusy(""); }
  }

  async function genIac(gaps: AmbaGap[], format: "bicep" | "terraform", title: string) {
    if (gaps.length === 0) {
      setMsg({ text: "No gaps to generate IaC for.", ok: false });
      return;
    }
    setBusy("iac");
    try {
      const r = await api.ambaIac({ gaps, format });
      setIacView({ title, text: r.iac, format });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function registerFindings() {
    if (scopeKind !== "workload" || !effectiveWorkloadId) {
      setMsg({ text: "Switch to a workload scope to register findings.", ok: false });
      return;
    }
    setBusy("findings");
    setMsg(null);
    try {
      const r = await api.registerAmbaFindings({
        workload_id: effectiveWorkloadId,
        workload_name: data?.scope_name ?? "",
        gaps: allGaps,
      });
      setMsg({ text: `Registered ${r.finding_count} Operations-pillar finding(s).`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function sendApproval() {
    if (allGaps.length === 0) {
      setMsg({ text: "No gaps to send.", ok: false });
      return;
    }
    setBusy("approval");
    setMsg(null);
    try {
      await api.sendAmbaApproval({
        scope_kind: scopeKind,
        scope_id: effectiveWorkloadId || subId,
        scope_name: data?.scope_name ?? "",
        gaps: allGaps,
        format: "bicep",
      });
      setMsg({ text: "Sent to the Approval Inbox (Settings → AMBA Change Requests).", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function createTicket(gap: AmbaGap, connectorId: string) {
    setBusy(`ticket:${gap.resource_id}:${gap.alert_key}`);
    setMsg(null);
    try {
      const r = await api.createAmbaTicket({ connector_id: connectorId, gap });
      setMsg({
        text: r.ok ? `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.` : r.detail || "Ticket failed.",
        ok: !!r.ok,
      });
      setTicketFor(null);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  function toggleGroup(t: string) {
    const setter = density === "compact" ? setOpenGroups : setCollapsed;
    setter((p) => {
      const n = new Set(p);
      n.has(t) ? n.delete(t) : n.add(t);
      return n;
    });
  }

  const visibleGroups = useMemo(() => {
    if (!data) return [] as AmbaGroup[];
    return data.groups
      .map((g) => ({ ...g, rows: g.rows.filter(rowVisible) }))
      .filter((g) => g.rows.length > 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, dQuery, catFilter, sevFilter, statusFilter]);

  // MU1 — reflect the active tab + filters into the URL (shareable / restored on reload).
  const [, mSetParams] = useSearchParams();
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    if (tab !== "coverage") next.set("tab", tab); else next.delete("tab");
    const setOrDel = (k: string, v: string) => { if (v && v !== "all") next.set(k, v); else next.delete(k); };
    setOrDel("cat", catFilter); setOrDel("sev", sevFilter); setOrDel("status", statusFilter);
    if (query.trim()) next.set("q", query.trim()); else next.delete("q");
    mSetParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, catFilter, sevFilter, statusFilter, query]);

  // MU3 — active-filter chips (each removable).
  const ambaChips = useMemo(() => {
    const out: { key: string; label: string; clear: () => void }[] = [];
    if (catFilter !== "all") out.push({ key: "cat", label: `Category: ${catFilter}`, clear: () => setCatFilter("all") });
    if (sevFilter !== "all") out.push({ key: "sev", label: `Severity: ${sevFilter}`, clear: () => setSevFilter("all") });
    if (statusFilter !== "all") out.push({ key: "status", label: `Status: ${statusFilter}`, clear: () => setStatusFilter("all") });
    if (query.trim()) out.push({ key: "q", label: `“${query.trim()}”`, clear: () => setQuery("") });
    return out;
  }, [catFilter, sevFilter, statusFilter, query]);

  if (mainView === "fleet") {
    return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gray-50">
      <CoverageViewTabs value={mainView} onChange={setMainView} />
      <MonitoringCoverageFleet onOpenWorkload={(id, connectionId) => {
        setScopeKind("workload");
        setWorkloadId(id);
        setConnId(connectionId);
        setLoadedScope(`workload:${id}:${connectionId}`);
        setMainView("coverage");
      }} />
    </div>;
  }

  if (mainView === "cleanup") {
    return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gray-50">
      <CoverageViewTabs value={mainView} onChange={setMainView} />
      <RunCleanup
        prefix="/amba"
        queryKey={["ambaCleanup"]}
        invalidateKeys={[["ambaFleet"]]}
        isEmptyRun={(run) => (run.resource_count ?? 0) === 0}
        renderMeta={(run) => <span className="text-gray-700">{run.scope_name}{typeof run.headline === "number" ? <span className="ml-2 text-gray-400">{run.headline}% covered</span> : null}</span>}
      />
    </div>;
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <CoverageViewTabs value={mainView} onChange={setMainView} />
      {/* Header */}
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center gap-4">
          <Donut pct={data?.coverage_pct ?? 0} />
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900">Monitoring Coverage</h1>
            <p className="text-xs text-gray-500">
              Baseline-alert (AMBA) coverage of your resources.
              {data?.demo && (
                <span className="ml-1 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo data</span>
              )}
            </p>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-gray-600">
              <span>Resources: <b>{data?.kpis.total_resources_in_baseline ?? 0}</b></span>
              <span className="text-green-600">✓ {data?.kpis.alerts_present ?? 0}</span>
              <span className="text-red-500">✗ {data?.kpis.alerts_missing ?? 0}</span>
              <span className="text-amber-500">⚠ {data?.kpis.alerts_misconfigured ?? 0}</span>
            </div>
          </div>
          {enabled && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Coverage trend</span>
              <TrendChart points={trendQ.data?.points ?? []} current={trendQ.data?.current} previous={trendQ.data?.previous} delta={trendQ.data?.delta} loading={trendQ.isLoading} />
            </div>
          )}

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/* Azure tenant + scope switcher */}
            <ConnectionScopePicker value={connId} onChange={(id) => { setConnId(id); if (scopeKind === "subscription") { setSubId(""); setSubName(""); } }} />
            <ScopePicker
              scopeKind={scopeKind}
              onScopeKindChange={setScopeKind}
              workloads={workloads}
              workloadId={effectiveWorkloadId}
              onWorkloadChange={setWorkloadId}
              subId={subId}
              subName={subName}
              connectionId={connId}
              onSubPick={(id, name) => {
                setSubId(id);
                setSubName(name);
              }}
            />
            <span className="text-xs text-gray-500">
              {data ? (
                <>
                  Updated {agoText(data.age_seconds)}
                  {data.stale && <span className="ml-1 text-amber-600">· stale</span>}
                  <span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px]">cached</span>
                </>
              ) : "—"}
              {refreshing && <span className="ml-1 text-blue-600">· refreshing…</span>}
            </span>
            {/* MU5 — nudge a re-scan when the cached coverage is past its TTL. */}
            {data?.stale && enabled && !refreshing && (
              <button onClick={doRefresh} disabled={!scopeReady} title="This coverage scan is past its refresh interval — run a fresh scan." className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100 disabled:opacity-50">
                ⚠ stale · rescan
              </button>
            )}
            {!enabled && (
              <button
                onClick={loadCoverage}
                disabled={!scopeReady}
                className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-100 disabled:opacity-50"
              >
                Load coverage
              </button>
            )}
            <button
              onClick={doRefresh}
              disabled={refreshing || !scopeReady}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              data-testid="coverage-refresh"
            >
              {refreshing ? "Refreshing…" : "↻ Refresh now"}
            </button>
            <button
              onClick={() => download(JSON.stringify(data, null, 2), `coverage-${data?.scope_name || "export"}.json`)}
              disabled={!data}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              ⬇ Export
            </button>
            <button
              onClick={() => void downloadPdf()}
              disabled={!data || busy === "pdf"}
              title="Download a branded PDF coverage report for this scope"
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {busy === "pdf" ? "…" : "📄 PDF"}
            </button>
            <button
              onClick={() => void saveEvidence()}
              disabled={!data || busy === "evidence"}
              title="Capture this coverage scan as an immutable Evidence Locker snapshot"
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {busy === "evidence" ? "Saving…" : "🗄 Save to Evidence"}
            </button>
          </div>
        </div>

        {/* Tabs */}
        <div className="mt-3 flex items-center gap-1 border-b text-sm">
          <button onClick={() => setTab("coverage")} className={`-mb-px border-b-2 px-3 py-1.5 ${tab === "coverage" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>Monitoring Coverage</button>
          <button onClick={() => setTab("all")} className={`-mb-px border-b-2 px-3 py-1.5 ${tab === "all" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>
            All Resources {data?.all_resources?.length ? <span className="ml-1 rounded bg-gray-100 px-1.5 text-[10px] text-gray-600">{data.all_resources.length}</span> : null}
          </button>
        </div>

        {/* Source provenance + filters */}
        {tab === "coverage" && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-gray-400">
            Source: {data?.source === "demo_dummy_data" ? "demo dummy data" : "Azure Resource Graph"}
          </span>
          <span className="text-gray-300">·</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search resources…"
            className="w-44 rounded-lg border px-2.5 py-1.5 outline-none focus:border-gray-400"
          />
          <select value={catFilter} onChange={(e) => setCatFilter(e.target.value)} className="rounded-lg border px-2 py-1.5">
            <option value="all">All categories</option>
            <option value="availability">Availability</option>
            <option value="performance">Performance</option>
            <option value="security">Security</option>
          </select>
          <select value={sevFilter} onChange={(e) => setSevFilter(e.target.value)} className="rounded-lg border px-2 py-1.5">
            <option value="all">All severities</option>
            <option value="critical">Critical</option>
            <option value="error">Error</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="rounded-lg border px-2 py-1.5">
            <option value="all">All statuses</option>
            <option value="present">✓ Present</option>
            <option value="missing">✗ Missing</option>
            <option value="misconfigured">⚠ Misconfigured</option>
          </select>
          <span className="text-gray-300">·</span>
          <DensityToggle value={density} onChange={setDensity} title="Compact shows just the resource-type rows; Expanded shows the full alert matrix." />
        </div>
        )}
        {/* MU3 — active filter chips. */}
        {tab === "coverage" && ambaChips.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {ambaChips.map((c) => (
              <span key={c.key} className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
                {c.label}
                <button onClick={c.clear} className="text-brand/60 hover:text-brand">✕</button>
              </span>
            ))}
            <button onClick={() => { setCatFilter("all"); setSevFilter("all"); setStatusFilter("all"); setQuery(""); }} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear all</button>
          </div>
        )}

        {/* Bulk toolbar */}
        {tab === "coverage" && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-gray-500">{allGaps.length} gap(s):</span>
          <button onClick={() => void genIac(allGaps, "bicep", "All gaps — Bicep")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate Bicep</button>
          <button onClick={() => void genIac(allGaps, "terraform", "All gaps — Terraform")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate Terraform</button>
          <button onClick={() => void registerFindings()} disabled={busy === "findings"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Create findings</button>
          <button onClick={() => void sendApproval()} disabled={busy === "approval"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Send to Approval Inbox</button>
        </div>
        )}
      </div>

      {msg && (
        <div className={`mx-6 mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>
          {msg.text}
        </div>
      )}

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        <PageIntro {...PAGE_INTROS["/coverage"]} icon="📡" storageKey="coverage" />
        {enabled && tab === "coverage" && (
          <CoverageHistory<AmbaCoverage>
            feature="amba"
            scopeKind={scopeKind}
            workloadId={effectiveWorkloadId}
            subId={subId}
            enabled={enabled}
            headlineLabel="Coverage"
            onView={(snap) => {
              qc.setQueryData(["amba", scopeKind, effectiveWorkloadId, subId], snap);
              setLoadedScope(scopeKey);
            }}
          />
        )}
        {!enabled ? (
          <div className="py-16 text-center text-sm text-gray-400">
            {scopeReady
              ? <>Pick a workload, then click <b>Load coverage</b> to audit its monitoring baseline coverage.</>
              : "Pick a workload or enter a subscription to begin."}
          </div>
        ) : covQ.isLoading ? (
          <div className="p-6"><Skeleton rows={8} /></div>
        ) : covQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(covQ.error)}</div>
        ) : data && data.report_exists === false ? (
          <div className="py-16 text-center">
            <div className="text-2xl">📡</div>
            <div className="mt-2 text-sm font-medium text-gray-600">No coverage scan yet for this scope</div>
            <div className="mx-auto mt-1 max-w-md text-xs text-gray-400">Run a scan to audit this scope's resources against the AMBA baseline alert set. It runs live against Azure and is saved to history.</div>
            <button onClick={() => void doRefresh()} disabled={refreshing} className="mt-3 rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">{refreshing ? "Scanning…" : "↻ Run first scan"}</button>
          </div>
        ) : tab === "all" ? (
          <AllResourcesTab resources={data?.all_resources ?? []} />
        ) : data?.error ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">{data.error}</div>
        ) : visibleGroups.length === 0 ? (
          <div className="py-16 text-center text-sm text-gray-400">
            No resources match the current scope/filters, or none are covered by the baseline reference.
          </div>
        ) : (
          <div className={density === "compact" ? "space-y-1.5" : "space-y-4"}>
            {visibleGroups.map((g) => {
              const isCollapsed = density === "compact" ? !openGroups.has(g.resource_type) : collapsed.has(g.resource_type);
              return (
                <section key={g.resource_type} className={`overflow-hidden border bg-white ${density === "compact" ? "rounded-lg" : "rounded-xl"}`}>
                  <button onClick={() => toggleGroup(g.resource_type)} className={`flex w-full items-center gap-2 text-left ${density === "compact" ? "px-3 py-1.5" : "px-4 py-3"}`}>
                    <span className="text-gray-400">{isCollapsed ? "▸" : "▾"}</span>
                    <h2 className={`font-semibold text-gray-900 ${density === "compact" ? "text-xs" : "text-sm"}`}>{g.display}</h2>
                    <span className="font-mono text-[10px] text-gray-400">{g.resource_type}</span>
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">{g.rows.length}</span>
                    <span
                      className={`ml-auto rounded px-2 py-0.5 text-[11px] font-medium ${
                        g.coverage_pct >= 80 ? "bg-green-100 text-green-700" : g.coverage_pct >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700"
                      }`}
                    >
                      {g.coverage_pct}% covered
                    </span>
                  </button>

                  {!isCollapsed && (
                    <AmbaMatrixBody group={g} expandedRow={expandedRow} setExpandedRow={setExpandedRow} setDrawer={setDrawer} />
                  )}
                </section>
              );
            })}
          </div>
        )}
      </div>

      {/* Right drawer: recommended vs observed + why + per-alert actions */}
      {drawer && (
        <div className="fixed inset-y-0 right-0 z-40 flex w-[420px] flex-col border-l bg-white shadow-xl">
          <div className="flex items-center justify-between border-b px-4 py-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-gray-900">{drawer.cell.alert_name}</div>
              <div className="truncate text-[11px] text-gray-500">{drawer.row.resource_name}</div>
            </div>
            <button onClick={() => setDrawer(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4 text-xs">
            <div className="flex items-center gap-2">
              <StatusMark status={drawer.cell.status} />
              <span className={`rounded px-1.5 py-0.5 ${CAT_CLS[drawer.cell.amba_category] ?? "bg-gray-100"}`}>{drawer.cell.amba_category}</span>
              <span className={`rounded px-1.5 py-0.5 ${SEV_CLS[drawer.cell.severity] ?? ""}`}>{drawer.cell.severity}</span>
            </div>
            <div>
              <div className="mb-1 font-medium text-gray-700">Why this matters</div>
              <p className="text-gray-600">{drawer.cell.why || "—"}</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border bg-gray-50 p-2">
                <div className="mb-1 font-medium text-gray-700">Recommended</div>
                <pre className="whitespace-pre-wrap break-words text-[10px] text-gray-600">{JSON.stringify(drawer.cell.recommended, null, 2)}</pre>
              </div>
              <div className="rounded-lg border bg-gray-50 p-2">
                <div className="mb-1 font-medium text-gray-700">Observed</div>
                <pre className="whitespace-pre-wrap break-words text-[10px] text-gray-600">{Object.keys(drawer.cell.observed).length ? JSON.stringify(drawer.cell.observed, null, 2) : "(no matching rule)"}</pre>
              </div>
            </div>
            {drawer.cell.observed.issues?.length ? (
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-amber-700">
                Issues: {drawer.cell.observed.issues.join(", ")}
              </div>
            ) : null}
            {drawer.cell.status !== "present" && (
              <div className="space-y-2 border-t pt-3">
                {(() => {
                  const gap: AmbaGap = {
                    resource_id: drawer.row.resource_id,
                    resource_name: drawer.row.resource_name,
                    resource_type: visibleGroups.find((g) => g.rows.some((r) => r.resource_id === drawer.row.resource_id))?.resource_type ?? "",
                    resource_group: drawer.row.resource_group,
                    subscription_id: drawer.row.subscription_id,
                    location: drawer.row.location,
                    alert_key: drawer.cell.alert_key,
                    alert_name: drawer.cell.alert_name,
                    amba_category: drawer.cell.amba_category,
                    severity: drawer.cell.severity,
                    status: drawer.cell.status,
                    recommended: drawer.cell.recommended,
                    observed: drawer.cell.observed,
                    why: drawer.cell.why,
                  };
                  const tkey = `ticket:${gap.resource_id}:${gap.alert_key}`;
                  return (
                    <>
                      <div className="flex gap-2">
                        <button onClick={() => void genIac([gap], "bicep", `${gap.alert_name} — Bicep`)} className="rounded-md border px-2 py-1 hover:bg-gray-50">Generate Bicep</button>
                        <button onClick={() => void genIac([gap], "terraform", `${gap.alert_name} — Terraform`)} className="rounded-md border px-2 py-1 hover:bg-gray-50">Generate Terraform</button>
                      </div>
                      {ticketFor === tkey ? (
                        ticketConnectors.length > 0 ? (
                          <select
                            autoFocus
                            disabled={busy === tkey}
                            defaultValue=""
                            onChange={(e) => e.target.value && void createTicket(gap, e.target.value)}
                            className="w-full rounded-md border px-1.5 py-1"
                          >
                            <option value="" disabled>{busy === tkey ? "Creating…" : "Pick connector…"}</option>
                            {ticketConnectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.type})</option>)}
                          </select>
                        ) : (
                          <span className="text-gray-400">No Jira/ServiceNow connector configured.</span>
                        )
                      ) : (
                        <button onClick={() => setTicketFor(tkey)} className="rounded-md border px-2 py-1 hover:bg-gray-50">🎫 Create ticket</button>
                      )}
                    </>
                  );
                })()}
              </div>
            )}
          </div>
        </div>
      )}

      {/* IaC modal */}
      {iacView && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={() => setIacView(null)}>
          <div className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="text-sm font-semibold text-gray-900">{iacView.title}</div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => download(iacView.text, `amba-gaps.${iacView.format === "terraform" ? "tf" : "bicep"}`)}
                  className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                >
                  ⬇ Download
                </button>
                <button onClick={() => setIacView(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
              </div>
            </div>
            <pre className="min-h-0 flex-1 overflow-auto bg-gray-900 p-4 text-[11px] leading-relaxed text-gray-100">{iacView.text}</pre>
          </div>
        </div>
      )}
      <PdfGeneratingOverlay open={busy === "pdf"} onCancel={cancelPdf} />
    </div>
  );
}
