import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  api,
  type BackupDrCell,
  type BackupDrCoverage,
  type BackupDrGap,
  type BackupDrGroup,
  type BackupDrRow,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { AllResourcesTab } from "./AllResourcesTab";

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

function Cell({ cell }: { cell: BackupDrCell }) {
  return (
    <td className="px-2 py-2 text-center" title={`${cell.value}${cell.detail ? " — " + cell.detail : ""}`}>
      <span className={`${CELL_CLS[cell.status]} text-[11px]`}>
        <span className="mr-0.5">{CELL_MARK[cell.status]}</span>
        {cell.value && cell.value !== "—" ? <span className="text-gray-600">{cell.value}</span> : null}
      </span>
    </td>
  );
}

export function BackupDrCoveragePanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"backup" | "dr" | "all">("backup");
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.backupdr.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.backupdr.workloadId", "");
  const [subId, setSubId] = usePersistedState("azsup.backupdr.subId", "");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [drawer, setDrawer] = useState<{ group: BackupDrGroup; row: BackupDrRow } | null>(null);
  const [drawerTab, setDrawerTab] = useState<"details" | "fix">("details");
  const [refreshing, setRefreshing] = useState(false);
  const [iacView, setIacView] = useState<{ title: string; text: string; format: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
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
  const params = scopeKind === "workload" ? { workload_id: effectiveWorkloadId } : { subscription_id: subId };
  const scopeReady = scopeKind === "workload" ? !!effectiveWorkloadId : !!subId;
  const scopeKey = `${scopeKind}:${effectiveWorkloadId || subId}`;
  // Coverage is loaded ONLY on an explicit click — switching workload/subscription does NOT
  // auto-fetch (a fetch can compute against Azure). The user clicks "Load coverage".
  // Persisted so revisiting the screen restores the last loaded workload + its cached data
  // (the GET reads the server cache; no fresh Azure scan on visit).
  const [loadedScope, setLoadedScope] = usePersistedState<string>("azsup.backupdr.loadedScope", "");
  const enabled = scopeReady && loadedScope === scopeKey;

  const covQ = useQuery({
    queryKey: ["backupdr", scopeKind, effectiveWorkloadId, subId],
    queryFn: () => api.backupDrCoverage(params),
    enabled,
  });
  const data: BackupDrCoverage | undefined = enabled ? covQ.data : undefined;
  const allGaps = data?.gaps ?? [];

  function loadCoverage() {
    if (scopeReady) { setMsg(null); setLoadedScope(scopeKey); }
  }

  async function doRefresh() {
    setRefreshing(true); setMsg(null);
    try {
      const fresh = await api.refreshBackupDr(params);
      setLoadedScope(scopeKey);
      qc.setQueryData(["backupdr", scopeKind, effectiveWorkloadId, subId], fresh);
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setRefreshing(false); }
  }

  function download(text: string, name: string) {
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
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
    setCollapsed((p) => { const n = new Set(p); n.has(t) ? n.delete(t) : n.add(t); return n; });
  }
  function rowVisible(r: BackupDrRow): boolean {
    if (statusFilter !== "all" && r.status !== statusFilter) return false;
    const q = query.trim().toLowerCase();
    if (q && !(`${r.resource_name} ${r.resource_group}`.toLowerCase().includes(q))) return false;
    return true;
  }

  const visibleGroups = useMemo(() => {
    if (!data) return [] as BackupDrGroup[];
    return data.groups.map((g) => ({ ...g, rows: g.rows.filter(rowVisible) })).filter((g) => g.rows.length > 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, query, statusFilter]);

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

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
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
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              <button onClick={() => setScopeKind("workload")} className={`rounded-md px-2.5 py-1 ${scopeKind === "workload" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}>Workload</button>
              <button onClick={() => setScopeKind("subscription")} className={`rounded-md px-2.5 py-1 ${scopeKind === "subscription" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}>Subscription</button>
            </div>
            {scopeKind === "workload" ? (
              <select value={effectiveWorkloadId} onChange={(e) => setWorkloadId(e.target.value)} className="rounded-lg border px-2 py-1.5 text-xs">
                {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            ) : (
              <input value={subId} onChange={(e) => setSubId(e.target.value)} placeholder="Subscription GUID" className="w-64 rounded-lg border px-2 py-1.5 text-xs" />
            )}
            <span className="text-xs text-gray-500">
              {data ? (<>Updated {agoText(data.age_seconds)}{data.stale_cache && <span className="ml-1 text-amber-600">· stale</span>}<span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px]">cached</span></>) : "—"}
            </span>
            {!enabled && (
              <button onClick={loadCoverage} disabled={!scopeReady} className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">Load coverage</button>
            )}
            <button onClick={() => void doRefresh()} disabled={refreshing || !scopeReady} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{refreshing ? "Refreshing…" : "↻ Refresh now"}</button>
            <button onClick={() => download(JSON.stringify(data, null, 2), `backupdr-${data?.scope_name || "export"}.json`)} disabled={!data} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">⬇ Export</button>
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
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-gray-500">{allGaps.length} gap(s):</span>
              <button onClick={() => void genIac(allGaps, "bicep", "All gaps — Bicep")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate Bicep</button>
              <button onClick={() => void genIac(allGaps, "runbook", "All gaps — Runbook")} disabled={busy === "iac"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Generate runbook</button>
              <button onClick={() => void registerFindings()} disabled={busy === "findings"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Create Reliability findings</button>
              <button onClick={() => void sendApproval("bicep")} disabled={busy === "approval"} className="rounded-md border px-2 py-1 hover:bg-gray-50 disabled:opacity-50">Send to Approval Inbox</button>
            </div>
          </>
        )}
      </div>

      {msg && (
        <div className={`mx-6 mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        {!enabled ? (
          <div className="py-16 text-center text-sm text-gray-400">
            {scopeReady
              ? <>Pick a workload, then click <b>Load coverage</b> to scan its backup &amp; DR posture.</>
              : "Pick a workload or enter a subscription to begin."}
          </div>
        ) : covQ.isLoading ? (
          <div className="py-16 text-center text-sm text-gray-400">Loading backup &amp; DR coverage…</div>
        ) : covQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{formatError(covQ.error)}</div>
        ) : tab === "all" ? (
          <AllResourcesTab resources={data?.all_resources ?? []} />
        ) : tab === "dr" ? (
          <div className="space-y-2">
            {(data?.dr_pairs ?? []).length === 0 ? (
              <div className="py-16 text-center text-sm text-gray-400">No DR replication pairs found in this scope.</div>
            ) : (
              (data?.dr_pairs ?? []).map((p) => (
                <div key={p.name} className="rounded-xl border bg-white p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-gray-900">{p.name}</span>
                    <span className="text-xs text-gray-500">{p.primary_region} → {p.secondary_region}</span>
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${p.healthy ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                      {p.healthy ? "✓ " : "✗ "}{p.replication_health}
                    </span>
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${p.stale ? "bg-red-100 text-red-700" : "bg-green-100 text-green-700"}`}>
                      {p.last_failover_test_age_days == null ? "Never drilled" : `Last drill ${p.last_failover_test_age_days}d ago`}{p.stale ? " · stale" : ""}
                    </span>
                    <span className="ml-auto text-[11px] text-gray-400">{p.protected_items} protected item(s)</span>
                  </div>
                </div>
              ))
            )}
          </div>
        ) : visibleGroups.length === 0 ? (
          <div className="py-16 text-center text-sm text-gray-400">{data?.error || "No resources match the current scope/filters."}</div>
        ) : (
          <div className="space-y-4">
            {data?.error && <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700">{data.error}</div>}
            {visibleGroups.map((g) => {
              const isCollapsed = collapsed.has(g.resource_type);
              return (
                <section key={g.resource_type} className="overflow-hidden rounded-xl border bg-white">
                  <button onClick={() => toggleGroup(g.resource_type)} className="flex w-full items-center gap-2 px-4 py-3 text-left">
                    <span className="text-gray-400">{isCollapsed ? "▸" : "▾"}</span>
                    <h2 className="text-sm font-semibold text-gray-900">{g.display}</h2>
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
                    <div className="overflow-x-auto border-t">
                      <table className="w-full text-xs">
                        <thead className="bg-gray-50 text-gray-500">
                          <tr>
                            <th className="sticky left-0 bg-gray-50 px-3 py-2 text-left font-medium">Resource</th>
                            {g.checks.map((c) => <th key={c} className="px-2 py-2 text-center font-medium">{CHECK_LABEL[c] || c}</th>)}
                            <th className="px-2 py-2"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {g.rows.map((row) => (
                            <tr key={row.resource_id} className="border-t hover:bg-gray-50">
                              <td className="sticky left-0 bg-white px-3 py-2">
                                <button onClick={() => openDrawer(g, row)} className="text-left">
                                  <div className="font-medium text-gray-800">{row.resource_name}</div>
                                  <div className="text-[10px] text-gray-400">{row.region}</div>
                                </button>
                              </td>
                              {g.checks.map((c) => {
                                const cell = row.cells.find((x) => x.check === c);
                                return cell ? <Cell key={c} cell={cell} /> : <td key={c} className="px-2 py-2 text-center text-gray-300">–</td>;
                              })}
                              <td className="px-2 py-2 text-right">
                                <button onClick={() => openDrawer(g, row)} className="rounded border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Open</button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
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
    </div>
  );
}
