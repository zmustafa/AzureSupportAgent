import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  api,
  type BackupDrCell,
  type BackupDrCoverage,
  type BackupDrGap,
  type BackupDrGroup,
  type BackupDrRow,
} from "../api";
import { formatError } from "../utils/format";
import { TrendChart } from "./TrendChart";
import { usePersistedState, useWorkloadDeepLink } from "../utils/persistedState";
import { AllResourcesTab } from "./AllResourcesTab";
import { ScopePicker } from "./ScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { DensityToggle } from "./DensityToggle";
import { isRefreshing, startBackgroundRefresh, takeRefreshError, useBackgroundRefresh } from "../utils/backgroundRefresh";
import { Skeleton, useDebounced } from "../utils/perf";
import { CoverageHistory, coverageRunsKey } from "./CoverageHistory";
import { PdfGeneratingOverlay } from "./PdfGeneratingOverlay";
import { PageIntro } from "./PageIntro";
import { PAGE_INTROS } from "../help/content";
import { BackupDrCoverageFleet } from "./coverage/BackupDrCoverageFleet";
import { RunCleanup } from "./cleanup/RunCleanup";

type CoverageMainView = "coverage" | "fleet" | "cleanup";

function CoverageViewTabs({ value, onChange }: { value: CoverageMainView; onChange: (value: CoverageMainView) => void }) {
  return <div className="flex items-center gap-1 border-b bg-white px-5 pt-2">
    <button onClick={() => onChange("coverage")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "coverage" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>🛡️ Coverage</button>
    <button onClick={() => onChange("fleet")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "fleet" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>🚀 Fleet</button>
    <button onClick={() => onChange("cleanup")} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${value === "cleanup" ? "border-brand font-medium text-brand" : "border-transparent text-gray-500 hover:text-gray-700"}`}>🧹 Cleanup</button>
  </div>;
}

const CELL_CLS: Record<string, string> = {
  green: "text-green-600",
  amber: "text-amber-500",
  red: "text-red-500",
  na: "text-gray-300",
};
const CELL_MARK: Record<string, string> = { green: "●", amber: "▲", red: "✗", na: "–" };

// Matrix column labels keyed by check id.
const CHECK_LABEL: Record<string, string> = {
  backup_enabled: "Backup",
  policy: "Policy",
  retention: "Retention",
  last_job: "Last Job",
  geo_redundancy: "Geo",
  offsite_region: "Backup Region",
  dr_pair: "DR Pair",
  encryption: "Encryption",
  soft_delete: "Soft-Delete",
  restore_test: "Restore Test",
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

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
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

// BP6 — memoized so a search/filter re-render of the matrix doesn't re-render every glyph cell
// (the protection matrix renders up to 10 cells per row across many rows).
const Cell = memo(function Cell({ cell }: { cell: BackupDrCell }) {
  return (
    <td className="px-2 py-2 text-center" title={`${cell.value}${cell.detail ? " — " + cell.detail : ""}`}>
      <span className={`${CELL_CLS[cell.status]} text-[11px]`}>
        <span className="mr-0.5">{CELL_MARK[cell.status]}</span>
        {cell.value && cell.value !== "—" ? <span className="text-gray-600">{cell.value}</span> : null}
      </span>
    </td>
  );
});

// BP1 — per-group protection matrix body. Small groups render inline; large groups (> 60 rows)
// virtualize via an internal-scroll windowed <tbody> with spacer rows, keeping the sticky first
// column + the up-to-10 per-check glyph columns. Rows already only open the drawer (no inline
// expander), so virtualized rows stay fixed-height.
const BDR_VIRT_THRESHOLD = 60;
function BackupMatrixBody({ group, openDrawer }: { group: BackupDrGroup; openDrawer: (g: BackupDrGroup, r: BackupDrRow) => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualize = group.rows.length > BDR_VIRT_THRESHOLD;
  const rowVirt = useVirtualizer({
    count: group.rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 41,
    overscan: 10,
  });
  const vItems = rowVirt.getVirtualItems();
  const padTop = vItems.length ? vItems[0].start : 0;
  const padBottom = vItems.length ? rowVirt.getTotalSize() - vItems[vItems.length - 1].end : 0;

  const mainRow = (row: BackupDrRow) => (
    <tr key={row.resource_id} className="border-t hover:bg-gray-50">
      <td className="sticky left-0 bg-white px-3 py-2">
        <button onClick={() => openDrawer(group, row)} className="text-left">
          <div className="font-medium text-gray-800">{row.resource_name}</div>
          <div className="text-[10px] text-gray-400">{row.region}</div>
        </button>
      </td>
      {group.checks.map((c) => {
        const cell = row.cells.find((x) => x.check === c);
        return cell ? <Cell key={c} cell={cell} /> : <td key={c} className="px-2 py-2 text-center text-gray-300">–</td>;
      })}
      <td className="px-2 py-2 text-right">
        <button onClick={() => openDrawer(group, row)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open</button>
      </td>
    </tr>
  );

  return (
    <div ref={scrollRef} className="overflow-auto border-t" style={virtualize ? { maxHeight: "60vh" } : undefined}>
      <table className="w-full text-xs">
        <thead className="sticky top-0 z-10 bg-gray-50 text-gray-500">
          <tr>
            <th className="sticky left-0 z-20 bg-gray-50 px-3 py-2 text-left font-medium">Resource</th>
            {group.checks.map((c) => <th key={c} className="px-2 py-2 text-center font-medium">{CHECK_LABEL[c] || c}</th>)}
            <th className="px-2 py-2"></th>
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
            group.rows.map((row) => mainRow(row))
          )}
        </tbody>
      </table>
    </div>
  );
}

export function BackupDrCoveragePanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [mainView, setMainView] = usePersistedState<CoverageMainView>("azsup.backupdr.view", "coverage");
  const bp0 = useRef(new URLSearchParams(window.location.search)).current;
  const [tab, setTab] = useState<"backup" | "dr" | "all">((bp0.get("tab") as "backup" | "dr" | "all") || "backup");
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.backupdr.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.backupdr.workloadId", "");
  useWorkloadDeepLink(setScopeKind, setWorkloadId);
  const [subId, setSubId] = usePersistedState("azsup.backupdr.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.backupdr.subName", "");
  const [connId, setConnId] = usePersistedState("azsup.backupdr.connId", "");
  const [query, setQuery] = useState(bp0.get("q") || "");
  const dQuery = useDebounced(query, 150);
  const [statusFilter, setStatusFilter] = useState(bp0.get("status") || "all");
  const [density, setDensity] = usePersistedState<"compact" | "expanded">("azsup.backupdr.density", "expanded");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [drawer, setDrawer] = useState<{ group: BackupDrGroup; row: BackupDrRow } | null>(null);
  const [drawerTab, setDrawerTab] = useState<"details" | "fix">("details");
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

  const workloads = workloadsQ.data?.workloads ?? [];
  const effectiveWorkloadId =
    scopeKind === "workload"
      ? workloadId || workloads.find((w) => w.id === "demo-amba-coverage")?.id || workloads[0]?.id || ""
      : "";
  const params = scopeKind === "workload" ? { workload_id: effectiveWorkloadId, connection_id: connId } : { subscription_id: subId, connection_id: connId };
  const scopeReady = scopeKind === "workload" ? !!effectiveWorkloadId : !!subId;
  const scopeKey = `${scopeKind}:${effectiveWorkloadId || subId}:${connId}`;
  // Coverage is loaded ONLY on an explicit click — switching workload/subscription does NOT
  // auto-fetch (a fetch can compute against Azure). The user clicks "Load coverage".
  // Persisted so revisiting the screen restores the last loaded workload + its cached data
  // (the GET reads the server cache; no fresh Azure scan on visit).
  const [loadedScope, setLoadedScope] = usePersistedState<string>("azsup.backupdr.loadedScope", "");
  const enabled = scopeReady && loadedScope === scopeKey;

  // Background refresh (per-scope) — survives scope switches + navigation.
  const refreshKey = `backupdr:${scopeKey}`;
  const refreshVersion = useBackgroundRefresh();
  const refreshing = isRefreshing(refreshKey);

  const covQ = useQuery({
    queryKey: ["backupdr", scopeKind, effectiveWorkloadId, subId, connId],
    queryFn: () => api.backupDrCoverage(params),
    enabled,
    staleTime: 5 * 60 * 1000,
  });
  const data: BackupDrCoverage | undefined = enabled ? covQ.data : undefined;
  const allGaps = data?.gaps ?? [];

  // % protected trend over time (loads with the coverage data).
  const trendQ = useQuery({
    queryKey: ["backupdr-trend", scopeKind, effectiveWorkloadId, subId, connId],
    queryFn: () => api.coverageTrend("backupdr", params),
    enabled,
    staleTime: 5 * 60 * 1000,
  });

  function loadCoverage() {
    if (scopeReady) { setMsg(null); setLoadedScope(scopeKey); }
  }

  async function doRefresh() {
    if (refreshing || !scopeReady) return;
    setMsg(null);
    setLoadedScope(scopeKey);
    const p = params;
    const dataKey = ["backupdr", scopeKind, effectiveWorkloadId, subId, connId] as const;
    const trendKey = ["backupdr-trend", scopeKind, effectiveWorkloadId, subId, connId] as const;
    startBackgroundRefresh(refreshKey, async () => {
      const fresh = await api.refreshBackupDr(p);
      qc.setQueryData(dataKey, fresh);
      await qc.invalidateQueries({ queryKey: trendKey });
      await qc.invalidateQueries({ queryKey: coverageRunsKey("backupdr", scopeKind, effectiveWorkloadId, subId) });
    });
  }

  // Surface an error from a background refresh that finished (possibly while away).
  useEffect(() => {
    if (!refreshing) {
      const err = takeRefreshError(refreshKey);
      if (err) setMsg({ text: err, ok: false });
    }
  }, [refreshVersion, refreshKey, refreshing]);

  function download(text: string, name: string) {
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  async function downloadPdf() {
    if (busy === "pdf" || !scopeReady) return;
    const controller = new AbortController();
    pdfAbortRef.current = controller;
    setBusy("pdf"); setMsg(null);
    try {
      const blob = await api.coverageReportPdf("backupdr", params, controller.signal);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `backupdr-coverage-${data?.scope_name || "report"}.pdf`; a.click();
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
      const r = await api.coverageSaveEvidence("backupdr", params);
      setMsg({ text: `Saved to Evidence Locker: ${r.snapshot.name}`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally { setBusy(""); }
  }

  async function genIac(gaps: BackupDrGap[], format: "bicep" | "runbook", title: string) {
    if (gaps.length === 0) { setMsg({ text: "No gaps to generate for.", ok: false }); return; }
    setBusy("iac");
    try {
      const r = await api.backupDrIac({ gaps, format });
      setIacView({ title, text: r.iac, format });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  async function registerFindings() {
    if (scopeKind !== "workload" || !effectiveWorkloadId) {
      setMsg({ text: "Switch to a workload scope to register findings.", ok: false }); return;
    }
    setBusy("findings"); setMsg(null);
    try {
      const r = await api.registerBackupDrFindings({ workload_id: effectiveWorkloadId, workload_name: data?.scope_name ?? "", gaps: allGaps });
      setMsg({ text: `Registered ${r.finding_count} Reliability-pillar finding(s).`, ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  async function sendApproval(format: "bicep" | "runbook") {
    if (allGaps.length === 0) { setMsg({ text: "No gaps to send.", ok: false }); return; }
    setBusy("approval"); setMsg(null);
    try {
      await api.sendBackupDrApproval({ scope_kind: scopeKind, scope_id: effectiveWorkloadId || subId, scope_name: data?.scope_name ?? "", gaps: allGaps, format });
      setMsg({ text: "Sent to the Approval Inbox (Settings → Backup/DR Change Requests).", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  async function createTicket(gap: BackupDrGap, connectorId: string) {
    setBusy(`ticket:${gap.resource_id}`); setMsg(null);
    try {
      const r = await api.createBackupDrTicket({ connector_id: connectorId, gap });
      setMsg({ text: r.ok ? `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.` : r.detail || "Ticket failed.", ok: !!r.ok });
      setTicketFor(null);
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  // Investigate → War Room: deep mode + workload + a gap-preloaded prompt.
  function investigate(row: BackupDrRow, group: BackupDrGroup) {
    const failed = row.cells.filter((c) => c.status === "red" || c.status === "amber").map((c) => CHECK_LABEL[c.check] || c.check);
    const prompt =
      `War Room: investigate the backup/DR coverage gap on "${row.resource_name}" (${group.display}). ` +
      `Failing: ${failed.join(", ")}. Region ${row.region}${row.backup_region ? `, backup in ${row.backup_region}` : ""}. ` +
      `Assess RTO/RPO risk, confirm current backup/DR state in Azure, and propose remediation.`;
    try {
      sessionStorage.setItem("azsup.warRoomHandoff", JSON.stringify({ workloadId: effectiveWorkloadId, prompt }));
    } catch { /* ignore */ }
    navigate("/chat");
  }

  function gapFor(group: BackupDrGroup, row: BackupDrRow): BackupDrGap {
    const failed = row.cells.filter((c) => c.status === "red" || c.status === "amber").map((c) => c.check);
    return {
      resource_id: row.resource_id, resource_name: row.resource_name, resource_type: group.resource_type,
      resource_group: row.resource_group, subscription_id: row.subscription_id, region: row.region,
      backup_region: row.backup_region, status: row.status, failed_checks: failed,
      vault_name: String((row.state as Record<string, unknown>).vault_name ?? ""),
      policy: String((row.state as Record<string, unknown>).policy ?? ""),
      dr_target_region: String((row.state as Record<string, unknown>).dr_target_region ?? ""),
      severity: row.status === "red" ? "error" : "warning",
    };
  }

  function toggleGroup(t: string) {
    const setter = density === "compact" ? setOpenGroups : setCollapsed;
    setter((p) => { const n = new Set(p); n.has(t) ? n.delete(t) : n.add(t); return n; });
  }
  function rowVisible(r: BackupDrRow): boolean {
    if (statusFilter !== "all" && r.status !== statusFilter) return false;
    const q = dQuery.trim().toLowerCase();
    if (q && !(`${r.resource_name} ${r.resource_group}`.toLowerCase().includes(q))) return false;
    return true;
  }

  const visibleGroups = useMemo(() => {
    if (!data) return [] as BackupDrGroup[];
    return data.groups.map((g) => ({ ...g, rows: g.rows.filter(rowVisible) })).filter((g) => g.rows.length > 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, dQuery, statusFilter]);

  // BU1 — reflect the active tab + filters into the URL (shareable / restored on reload).
  const [, bSetParams] = useSearchParams();
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    if (tab !== "backup") next.set("tab", tab); else next.delete("tab");
    if (statusFilter !== "all") next.set("status", statusFilter); else next.delete("status");
    if (query.trim()) next.set("q", query.trim()); else next.delete("q");
    bSetParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, statusFilter, query]);

  // BU3 — active-filter chips (each removable).
  const bdrChips = useMemo(() => {
    const out: { key: string; label: string; clear: () => void }[] = [];
    if (statusFilter !== "all") out.push({ key: "status", label: `Status: ${statusFilter}`, clear: () => setStatusFilter("all") });
    if (query.trim()) out.push({ key: "q", label: `“${query.trim()}”`, clear: () => setQuery("") });
    return out;
  }, [statusFilter, query]);

  // Per-status resource counts across all backup groups, for the header summary line.
  const statusTotals = useMemo(() => {
    const t = { green: 0, amber: 0, red: 0 };
    for (const g of data?.groups ?? []) { t.green += g.green; t.amber += g.amber; t.red += g.red; }
    return t;
  }, [data]);

  const sc = data?.scorecard;

  function openDrawer(group: BackupDrGroup, row: BackupDrRow) {
    setDrawer({ group, row });
    setDrawerTab("details");
  }

  if (mainView === "fleet") {
    return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gray-50">
      <CoverageViewTabs value={mainView} onChange={setMainView} />
      <BackupDrCoverageFleet onOpenWorkload={(id, connectionId) => {
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
        prefix="/backupdr"
        queryKey={["backupdrCleanup"]}
        invalidateKeys={[["backupdrFleet"]]}
        isEmptyRun={(run) => (run.resource_count ?? 0) === 0}
        renderMeta={(run) => <span className="text-gray-700">{run.scope_name}{typeof run.headline === "number" ? <span className="ml-2 text-gray-400">{run.headline}% protected</span> : null}</span>}
      />
    </div>;
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <CoverageViewTabs value={mainView} onChange={setMainView} />
      {/* Header */}
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center gap-4">
          <Donut pct={sc?.pct_protected ?? 0} />
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900">Backup &amp; DR Coverage</h1>
            <p className="text-xs text-gray-500">
              Are RTO/RPO commitments actually backed by configured &amp; tested protection?
              {data?.demo && <span className="ml-1 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo data</span>}
            </p>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-gray-600">
              <span>Resources: <b>{statusTotals.green + statusTotals.amber + statusTotals.red}</b></span>
              <span className="text-green-600">✓ {statusTotals.green}</span>
              <span className="text-amber-500">⚠ {statusTotals.amber}</span>
              <span className="text-red-500">✗ {statusTotals.red}</span>
            </div>
          </div>
          {enabled && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Protected trend</span>
              <TrendChart points={trendQ.data?.points ?? []} current={trendQ.data?.current} previous={trendQ.data?.previous} delta={trendQ.data?.delta} loading={trendQ.isLoading} />
            </div>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
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
              {data ? (<>Updated {agoText(data.age_seconds)}{data.stale_cache && <span className="ml-1 text-amber-600">· stale</span>}<span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px]">cached</span></>) : "—"}
              {refreshing && <span className="ml-1 text-blue-600">· refreshing…</span>}
            </span>
            {/* BU4 — stale-cache rescan nudge (backup-DR uses stale_cache, not stale). */}
            {data?.stale_cache && enabled && !refreshing && (
              <button onClick={doRefresh} disabled={!scopeReady} title="This coverage scan is past its refresh interval — run a fresh scan." className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100 disabled:opacity-50">⚠ stale · rescan</button>
            )}
            {!enabled && (
              <button onClick={loadCoverage} disabled={!scopeReady} className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">Load coverage</button>
            )}
            <button onClick={doRefresh} disabled={refreshing || !scopeReady} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50" data-testid="coverage-refresh">{refreshing ? "Refreshing…" : "↻ Refresh now"}</button>
            <button onClick={() => download(JSON.stringify(data, null, 2), `backupdr-${data?.scope_name || "export"}.json`)} disabled={!data} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">⬇ Export</button>
            <button onClick={() => void downloadPdf()} disabled={!data || busy === "pdf"} title="Download a branded PDF coverage report for this scope" className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{busy === "pdf" ? "…" : "📄 PDF"}</button>
            <button onClick={() => void saveEvidence()} disabled={!data || busy === "evidence"} title="Capture this coverage scan as an immutable Evidence Locker snapshot" className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{busy === "evidence" ? "Saving…" : "🗄 Save to Evidence"}</button>
          </div>
        </div>

        {/* Scorecard */}
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Protected" value={`${sc?.pct_protected ?? 0}%`} tone={(sc?.pct_protected ?? 0) >= 80 ? "text-green-600" : "text-amber-600"} />
          <Stat label="Offsite / geo" value={`${sc?.pct_offsite ?? 0}%`} tone={(sc?.pct_offsite ?? 0) >= 80 ? "text-green-600" : "text-amber-600"} />
          <Stat label="Job ✓ in SLA" value={`${sc?.pct_recent_job ?? 0}%`} tone={(sc?.pct_recent_job ?? 0) >= 80 ? "text-green-600" : "text-amber-600"} />
          <Stat label="DR pairs" value={`${sc?.dr_pairs ?? 0}`} />
          <Stat label="Stale drills" value={`${sc?.dr_pairs_stale ?? 0}`} tone={(sc?.dr_pairs_stale ?? 0) > 0 ? "text-red-600" : "text-green-600"} />
          <Stat label="Last drill" value={sc?.last_drill_days != null ? `${sc.last_drill_days}d` : "—"} />
        </div>

        {/* Tabs */}
        <div className="mt-3 flex items-center gap-1 border-b text-sm">
          <button onClick={() => setTab("backup")} className={`-mb-px border-b-2 px-3 py-1.5 ${tab === "backup" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>Backup Coverage</button>
          <button onClick={() => setTab("dr")} className={`-mb-px border-b-2 px-3 py-1.5 ${tab === "dr" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>
            Disaster Recovery {sc?.dr_pairs_stale ? <span className="ml-1 rounded bg-red-100 px-1.5 text-[10px] text-red-700">{sc.dr_pairs_stale} stale</span> : null}
          </button>
          <button onClick={() => setTab("all")} className={`-mb-px border-b-2 px-3 py-1.5 ${tab === "all" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>
            All Resources {data?.all_resources?.length ? <span className="ml-1 rounded bg-gray-100 px-1.5 text-[10px] text-gray-600">{data.all_resources.length}</span> : null}
          </button>
        </div>

        {tab === "backup" && (
          <>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-gray-400">Source: {data?.source === "demo_dummy_data" ? "demo dummy data" : "Resource Graph"}</span>
              <span className="text-gray-300">·</span>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search resources…" className="w-44 rounded-lg border px-2.5 py-1.5 outline-none focus:border-gray-400" />
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="rounded-lg border px-2 py-1.5">
                <option value="all">All statuses</option>
                <option value="red">🔴 Critical</option>
                <option value="amber">🟠 At risk</option>
                <option value="green">🟢 Protected</option>
              </select>
              <span className="text-gray-300">·</span>
              <DensityToggle value={density} onChange={setDensity} title="Compact shows just the resource-type rows; Expanded shows the full protection matrix." />
            </div>
            {/* BU3 — active filter chips. */}
            {bdrChips.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {bdrChips.map((c) => (
                  <span key={c.key} className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
                    {c.label}
                    <button onClick={c.clear} className="text-brand/60 hover:text-brand">✕</button>
                  </span>
                ))}
                <button onClick={() => { setStatusFilter("all"); setQuery(""); }} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear all</button>
              </div>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-gray-500">{allGaps.length} gap(s):</span>
              <button onClick={() => void genIac(allGaps, "bicep", "All gaps — Bicep")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate Bicep</button>
              <button onClick={() => void genIac(allGaps, "runbook", "All gaps — Runbook")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate runbook</button>
              <button onClick={() => void registerFindings()} disabled={busy === "findings"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Create Reliability findings</button>
              <button onClick={() => void sendApproval("bicep")} disabled={busy === "approval"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Send to Approval Inbox</button>
              {/* BU5 — matrix glyph legend. */}
              <span className="ml-auto flex items-center gap-2 text-[11px] text-gray-400">
                <span className="text-green-600">●</span> protected
                <span className="text-amber-500">▲</span> at risk
                <span className="text-red-500">✗</span> missing
                <span className="text-gray-300">–</span> n/a
              </span>
            </div>
          </>
        )}
      </div>

      {msg && (
        <div className={`mx-6 mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        <PageIntro {...PAGE_INTROS["/backupdr"]} icon="💾" storageKey="backupdr" />
        {enabled && tab === "backup" && (
          <CoverageHistory<BackupDrCoverage>
            feature="backupdr"
            scopeKind={scopeKind}
            workloadId={effectiveWorkloadId}
            subId={subId}
            enabled={enabled}
            headlineLabel="Protected"
            onView={(snap) => {
              qc.setQueryData(["backupdr", scopeKind, effectiveWorkloadId, subId], snap);
              setLoadedScope(scopeKey);
            }}
          />
        )}
        {!enabled ? (
          <div className="py-16 text-center text-sm text-gray-400">
            {scopeReady
              ? <>Pick a workload, then click <b>Load coverage</b> to scan its backup &amp; DR posture.</>
              : "Pick a workload or enter a subscription to begin."}
          </div>
        ) : covQ.isLoading ? (
          <div className="p-6"><Skeleton rows={8} /></div>
        ) : covQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(covQ.error)}</div>
        ) : data && data.report_exists === false ? (
          <div className="py-16 text-center">
            <div className="text-2xl">💾</div>
            <div className="mt-2 text-sm font-medium text-gray-600">No coverage scan yet for this scope</div>
            <div className="mx-auto mt-1 max-w-md text-xs text-gray-400">Run a scan to audit this scope's backup &amp; DR posture against the reference. It runs live against Azure and is saved to history.</div>
            <button onClick={() => void doRefresh()} disabled={refreshing} className="mt-3 rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">{refreshing ? "Scanning…" : "↻ Run first scan"}</button>
          </div>
        ) : tab === "all" ? (
          <AllResourcesTab resources={data?.all_resources ?? []} />
        ) : tab === "dr" ? (
          <div className="space-y-2">
            {(data?.dr_pairs ?? []).length === 0 ? (
              <div className="py-16 text-center text-sm text-gray-400">No DR replication pairs found in this scope.</div>
            ) : (
              (data?.dr_pairs ?? []).map((p) => {
                // BU6 — DR SLA flag: a pair is "at risk" if replication is unhealthy, the drill is
                // stale, or it was never drilled. Surfaced as a leading severity dot + an SLA badge.
                const neverDrilled = p.last_failover_test_age_days == null;
                const atRisk = !p.healthy || p.stale || neverDrilled;
                return (
                <div key={p.name} className={`rounded-xl border bg-white p-3 ${atRisk ? "border-l-4 border-l-red-400" : "border-l-4 border-l-green-400"}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-gray-900">{p.name}</span>
                    <span className="text-xs text-gray-500">{p.primary_region} → {p.secondary_region}</span>
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${p.healthy ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                      {p.healthy ? "✓ " : "✗ "}{p.replication_health}
                    </span>
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${p.stale || neverDrilled ? "bg-red-100 text-red-700" : "bg-green-100 text-green-700"}`}>
                      {neverDrilled ? "Never drilled" : `Last drill ${p.last_failover_test_age_days}d ago`}{p.stale ? " · stale" : ""}
                    </span>
                    {atRisk && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">⚠ DR SLA at risk</span>}
                    <span className="ml-auto text-[11px] text-gray-400">{p.protected_items} protected item(s)</span>
                  </div>
                </div>
                );
              })
            )}
          </div>
        ) : visibleGroups.length === 0 ? (
          <div className="py-16 text-center text-sm text-gray-400">{data?.error || "No resources match the current scope/filters."}</div>
        ) : (
          <div className={density === "compact" ? "space-y-1.5" : "space-y-4"}>
            {data?.error && <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700">{data.error}</div>}
            {visibleGroups.map((g) => {
              const isCollapsed = density === "compact" ? !openGroups.has(g.resource_type) : collapsed.has(g.resource_type);
              return (
                <section key={g.resource_type} className={`overflow-hidden border bg-white ${density === "compact" ? "rounded-lg" : "rounded-xl"}`}>
                  <button onClick={() => toggleGroup(g.resource_type)} className={`flex w-full items-center gap-2 text-left ${density === "compact" ? "px-3 py-1.5" : "px-4 py-3"}`}>
                    <span className="text-gray-400">{isCollapsed ? "▸" : "▾"}</span>
                    <h2 className={`font-semibold text-gray-900 ${density === "compact" ? "text-xs" : "text-sm"}`}>{g.display}</h2>
                    <span className="font-mono text-[10px] text-gray-400">{g.resource_type}</span>
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">{g.rows.length}</span>
                    <span className="ml-auto flex items-center gap-2 text-[11px]">
                      {g.red > 0 && <span className="text-red-600">🔴 {g.red}</span>}
                      {g.amber > 0 && <span className="text-amber-600">🟠 {g.amber}</span>}
                      {g.green > 0 && <span className="text-green-600">🟢 {g.green}</span>}
                      <span className={`rounded px-2 py-0.5 font-medium ${g.coverage_pct >= 80 ? "bg-green-100 text-green-700" : g.coverage_pct >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700"}`}>{g.coverage_pct}%</span>
                    </span>
                  </button>
                  {!isCollapsed && (
                    <BackupMatrixBody group={g} openDrawer={openDrawer} />
                  )}
                </section>
              );
            })}
          </div>
        )}
      </div>

      {/* Side panel */}
      {drawer && (() => {
        const { group, row } = drawer;
        const gap = gapFor(group, row);
        const tkey = `ticket:${row.resource_id}`;
        return (
          <div className="fixed inset-y-0 right-0 z-40 flex w-[460px] flex-col border-l bg-white shadow-xl">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-gray-900">{row.resource_name}</div>
                <div className="truncate text-[11px] text-gray-500">{group.display} · {row.region}{row.backup_region ? ` → backup ${row.backup_region}` : ""}</div>
              </div>
              <button onClick={() => setDrawer(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
            </div>
            <div className="flex items-center gap-1 border-b px-3 text-xs">
              <button onClick={() => setDrawerTab("details")} className={`-mb-px border-b-2 px-2 py-1.5 ${drawerTab === "details" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>Details</button>
              <button onClick={() => setDrawerTab("fix")} className={`-mb-px border-b-2 px-2 py-1.5 ${drawerTab === "fix" ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>Fix</button>
            </div>
            <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4 text-xs">
              {drawerTab === "details" ? (
                <>
                  <div className="space-y-1">
                    {row.cells.map((c) => (
                      <div key={c.check} className="flex items-center gap-2">
                        <span className={CELL_CLS[c.status]}>{CELL_MARK[c.status]}</span>
                        <span className="w-28 text-gray-700">{CHECK_LABEL[c.check] || c.check}</span>
                        <span className="text-gray-600">{c.value}</span>
                        {c.detail && <span className="text-gray-400">— {c.detail}</span>}
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg border bg-gray-50 p-2">
                    <div className="mb-1 font-medium text-gray-700">Resource Graph properties (state)</div>
                    <pre className="whitespace-pre-wrap break-words text-[10px] text-gray-600">{JSON.stringify(row.state, null, 2)}</pre>
                  </div>
                  <button onClick={() => investigate(row, group)} className="w-full rounded-md border px-2 py-1.5 text-center hover:bg-gray-50">🚨 Investigate in War Room</button>
                </>
              ) : (
                <div className="space-y-2">
                  <p className="text-gray-500">Generate remediation, or hand off to a ticket. Read-only — nothing is applied.</p>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => void genIac([gap], "bicep", `${row.resource_name} — Bicep`)} className="rounded-md border px-2 py-1 hover:bg-gray-50">Generate Bicep</button>
                    <button onClick={() => void genIac([gap], "runbook", `${row.resource_name} — Runbook`)} className="rounded-md border px-2 py-1 hover:bg-gray-50">Generate runbook</button>
                  </div>
                  {ticketFor === tkey ? (
                    ticketConnectors.length > 0 ? (
                      <select autoFocus disabled={busy === tkey} defaultValue="" onChange={(e) => e.target.value && void createTicket(gap, e.target.value)} className="w-full rounded-md border px-1.5 py-1">
                        <option value="" disabled>{busy === tkey ? "Creating…" : "Pick connector…"}</option>
                        {ticketConnectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.type})</option>)}
                      </select>
                    ) : <span className="text-gray-400">No Jira/ServiceNow connector configured.</span>
                  ) : (
                    <button onClick={() => setTicketFor(tkey)} className="rounded-md border px-2 py-1 hover:bg-gray-50">🎫 Create ticket</button>
                  )}
                  <button onClick={() => investigate(row, group)} className="w-full rounded-md border px-2 py-1.5 text-center hover:bg-gray-50">🚨 Investigate in War Room</button>
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {/* IaC modal */}
      {iacView && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={() => setIacView(null)}>
          <div className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="text-sm font-semibold text-gray-900">{iacView.title}</div>
              <div className="flex items-center gap-2">
                <button onClick={() => download(iacView.text, iacView.format === "runbook" ? "backupdr-runbook.ps1" : "backupdr.bicep")} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">⬇ Download</button>
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
