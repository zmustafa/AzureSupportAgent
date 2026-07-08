import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  api,
  type ReservationItem,
  type ReservationsSnapshot,
  type ReservationsDigestPreview,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { Skeleton, InlineSearch, useDebounced } from "../utils/perf";
import { ConnectionScopePicker } from "./ConnectionScopePicker";

const SEV_TEXT: Record<string, string> = {
  red: "text-red-600",
  amber: "text-amber-600",
  grey: "text-gray-500",
};
const SEV_ROW: Record<string, string> = {
  red: "bg-red-50",
  amber: "bg-amber-50",
  grey: "",
};

function daysLabel(d: number | null): string {
  if (d === null) return "TBD";
  if (d < 0) return `${Math.abs(d)}d ago`;
  return `${d}d left`;
}

function renewBadge(v: boolean | null) {
  if (v === true) return <span className="rounded bg-green-100 px-1.5 py-0.5 text-[11px] font-medium text-green-700">Auto-renew</span>;
  if (v === false) return <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">No renew</span>;
  return <span className="text-gray-400">—</span>;
}

function utilBadge(v: number | null) {
  if (v === null || v === undefined) return <span className="text-gray-400">—</span>;
  const low = v < 25;
  return (
    <span className={low ? "font-medium text-amber-600" : "text-gray-700"}>
      {`${v}%`}
      {low && <span className="ml-1 rounded bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-700">low</span>}
    </span>
  );
}

function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function Stat({ label, value, tone, active, onClick }: { label: string; value: string | number; tone?: string; active?: boolean; onClick?: () => void }) {
  const base = `rounded-lg border bg-white px-3 py-2 text-left transition ${active ? "ring-2 ring-brand border-brand" : ""}`;
  const inner = (
    <>
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </>
  );
  if (!onClick) return <div className={base}>{inner}</div>;
  return (
    <button type="button" onClick={onClick} className={`${base} hover:border-brand hover:shadow-sm`} title={active ? "Click to clear filter" : `Filter to ${label}`}>
      {inner}
    </button>
  );
}

export function ReservationsMonitorPanel() {
  const qc = useQueryClient();
  const [, setParams] = useSearchParams();
  const p0 = useRef(new URLSearchParams(window.location.search)).current;
  const [demo, setDemo] = usePersistedState<boolean>("azsup.reservations.demo", p0.get("demo") === "1");
  const [connId, setConnId] = usePersistedState<string>("azsup.reservations.connId", "");
  const [refreshing, setRefreshing] = useState(false);
  const [showDigest, setShowDigest] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [q, setQ] = useState(p0.get("q") || "");
  const dQ = useDebounced(q, 150);
  const [sortBy, setSortBy] = usePersistedState<string>("azsup.reservations.sort", "days");
  // RU2 — status + renew + utilization filters (also driven by the KPI drill-through).
  const [statusF, setStatusF] = useState(p0.get("status") || "all"); // all|urgent|expiring_soon|recently_expired|active
  const [renewF, setRenewF] = useState(p0.get("renew") || "all");     // all|auto|none
  const [utilF, setUtilF] = useState(p0.get("util") || "all");        // all|low

  // Selecting the view only READS the server cache (no Azure call), so it's safe to load
  // on mount. A miss returns never_loaded so we prompt for Refresh.
  const resQ = useQuery({
    queryKey: ["reservations", demo, connId],
    queryFn: () => api.reservationsOverview(demo, connId),
    staleTime: 5 * 60 * 1000,
  });
  const data: ReservationsSnapshot | undefined = resQ.data;
  const items = data?.items ?? [];
  const counts = data?.counts;

  const digestQ = useQuery<ReservationsDigestPreview>({
    queryKey: ["reservations-digest", demo, connId],
    queryFn: () => api.reservationsDigestPreview(demo, connId),
    enabled: showDigest,
  });

  // RU3 — reflect demo + view filters into the URL so a link / refresh restores the view.
  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    if (demo) next.set("demo", "1"); else next.delete("demo");
    const setOrDel = (k: string, v: string) => { if (v && v !== "all") next.set(k, v); else next.delete(k); };
    setOrDel("status", statusF); setOrDel("renew", renewF); setOrDel("util", utilF);
    if (q.trim()) next.set("q", q.trim()); else next.delete("q");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [demo, statusF, renewF, utilF, q]);

  // RP2/RU2 — real client-side search + filters + sort over the loaded reservations.
  const sorted = useMemo(() => {
    const t = dQ.trim().toLowerCase();
    let list = items.filter((r) => {
      if (t && !`${r.display_name} ${r.sku} ${r.reserved_resource_type} ${r.applied_scope_type}`.toLowerCase().includes(t)) return false;
      if (statusF === "urgent" && r.severity !== "red") return false;
      if (statusF !== "all" && statusF !== "urgent" && r.bucket !== statusF) return false;
      if (renewF === "auto" && r.renew !== true) return false;
      if (renewF === "none" && r.renew !== false) return false;
      if (utilF === "low" && !(typeof r.utilization_pct === "number" && r.utilization_pct < 25)) return false;
      return true;
    });
    list = [...list].sort((a, b) => {
      if (sortBy === "utilization") return (a.utilization_pct ?? 999) - (b.utilization_pct ?? 999);
      if (sortBy === "name") return a.display_name.localeCompare(b.display_name);
      return (a.days_until ?? 1e9) - (b.days_until ?? 1e9);
    });
    return list;
  }, [items, dQ, sortBy, statusF, renewF, utilF]);

  // RU2 — active-filter chips (each removable).
  const chips = useMemo(() => {
    const out: { key: string; label: string; clear: () => void }[] = [];
    if (statusF !== "all") out.push({ key: "status", label: `Status: ${statusF === "urgent" ? "Urgent" : statusF.replace("_", " ")}`, clear: () => setStatusF("all") });
    if (renewF !== "all") out.push({ key: "renew", label: renewF === "auto" ? "Auto-renew" : "Not renewing", clear: () => setRenewF("all") });
    if (utilF === "low") out.push({ key: "util", label: "Low utilization", clear: () => setUtilF("all") });
    if (q.trim()) out.push({ key: "q", label: `“${q.trim()}”`, clear: () => setQ("") });
    return out;
  }, [statusF, renewF, utilF, q]);

  // RU5 — KPI drill-through: clicking a tile toggles the matching filter.
  const toggle = <T,>(cur: T, set: (v: T) => void, val: T, reset: T) => set(cur === val ? reset : val);

  // RU6 — rich export mechanism: CSV, JSON, a standalone HTML report, and Markdown-to-clipboard,
  // all over the current (filtered) view. Everything is generated client-side from loaded data.
  const [exportOpen, setExportOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!exportOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (exportRef.current && !exportRef.current.contains(e.target as Node)) setExportOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [exportOpen]);

  const stamp = () => new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const scopeLabel = demo ? "Demo data" : connId ? `Connection ${connId}` : "Default connection";
  const filterLabel = () => {
    const parts: string[] = [];
    if (statusF !== "all") parts.push(`status=${statusF}`);
    if (renewF !== "all") parts.push(`renew=${renewF}`);
    if (utilF === "low") parts.push("utilization=low");
    if (dQ.trim()) parts.push(`search="${dQ.trim()}"`);
    return parts.length ? parts.join(", ") : "none";
  };

  function download(filename: string, mime: string, content: string | Blob) {
    const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportCsv() {
    const esc = (v: unknown) => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const head = ["Reservation", "SKU", "Resource type", "Term", "Billing plan", "Quantity", "Created", "Expires", "Days until", "Bucket", "Renew", "Utilization%", "Scope", "Status", "Order ID"];
    const lines = sorted.map((r) => [
      r.display_name || r.id, r.sku, r.reserved_resource_type, r.term, r.billing_plan, r.quantity ?? "",
      (r.created_date || "").slice(0, 10), (r.expiry_date || "").slice(0, 10), r.days_until ?? "", r.bucket,
      r.renew === true ? "auto" : r.renew === false ? "no" : "", r.utilization_pct ?? "",
      r.applied_scope_type, r.provisioning_state, r.order_id,
    ].map(esc).join(","));
    // Prepend a UTF-8 BOM so Excel opens accented names correctly.
    download(`reservations-${stamp()}.csv`, "text/csv;charset=utf-8", "\uFEFF" + [head.join(","), ...lines].join("\r\n"));
    setMsg({ text: `Exported ${sorted.length} reservation${sorted.length === 1 ? "" : "s"} to CSV`, ok: true });
  }

  function exportJson() {
    const payload = {
      report: "Azure Reservations Monitor",
      generated_at: new Date().toISOString(),
      scope: scopeLabel,
      window_days: data?.window_days ?? 60,
      snapshot_generated_at: data?.generated_at ?? null,
      filters: filterLabel(),
      count: sorted.length,
      counts: counts ?? null,
      items: sorted,
    };
    download(`reservations-${stamp()}.json`, "application/json", JSON.stringify(payload, null, 2));
    setMsg({ text: `Exported ${sorted.length} reservation${sorted.length === 1 ? "" : "s"} to JSON`, ok: true });
  }

  function buildMarkdown(): string {
    const cell = (v: unknown) => String(v ?? "—").replace(/\|/g, "\\|");
    const rows = sorted.map((r) => `| ${cell(r.display_name || r.id)} | ${cell(r.sku || r.reserved_resource_type)} | ${cell(r.term)} | ${cell((r.expiry_date || "").slice(0, 10))} | ${cell(daysLabel(r.days_until))} | ${r.renew === true ? "auto" : r.renew === false ? "no" : "—"} | ${r.utilization_pct ?? "—"}${typeof r.utilization_pct === "number" ? "%" : ""} | ${cell(r.provisioning_state)} |`);
    return [
      `# Azure Reservations Report`,
      "",
      `- **Scope:** ${scopeLabel}`,
      `- **Generated:** ${new Date().toLocaleString()}`,
      `- **Window:** ±${data?.window_days ?? 60} days`,
      `- **Filters:** ${filterLabel()}`,
      `- **Reservations:** ${sorted.length}${counts ? ` of ${counts.total}` : ""}`,
      counts ? `- **Urgent / Not renewing / Low utilization:** ${counts.red} / ${counts.non_renew} / ${counts.low_utilization}` : "",
      "",
      `| Reservation | SKU | Term | Expires | Countdown | Renew | Utilization | Status |`,
      `| --- | --- | --- | --- | --- | --- | --- | --- |`,
      ...rows,
      "",
    ].filter(Boolean).join("\n");
  }

  async function copyMarkdown() {
    try {
      await navigator.clipboard.writeText(buildMarkdown());
      setMsg({ text: `Copied ${sorted.length} reservation${sorted.length === 1 ? "" : "s"} as Markdown`, ok: true });
    } catch {
      // Clipboard unavailable (e.g. non-secure context) — fall back to a file download.
      download(`reservations-${stamp()}.md`, "text/markdown", buildMarkdown());
      setMsg({ text: "Clipboard blocked — downloaded Markdown instead", ok: true });
    }
  }

  function exportHtml() {
    const esc = (v: unknown) =>
      String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
    const badge = (r: ReservationItem) => {
      const color = r.severity === "red" ? "#dc2626" : r.severity === "amber" ? "#d97706" : "#6b7280";
      return `<span style="color:${color};font-weight:600">${esc(daysLabel(r.days_until))}</span>`;
    };
    const renew = (v: boolean | null) =>
      v === true ? `<span class="pill pill-green">Auto-renew</span>` : v === false ? `<span class="pill pill-red">No renew</span>` : "—";
    const util = (v: number | null) =>
      v == null ? "—" : `<span style="${v < 25 ? "color:#d97706;font-weight:600" : ""}">${v}%${v < 25 ? " ⚠" : ""}</span>`;
    const rowBg = (s: string) => (s === "red" ? "background:#fef2f2" : s === "amber" ? "background:#fffbeb" : "");
    const rows = sorted
      .map(
        (r) => `<tr style="${rowBg(r.severity)}">
        <td><div class="name">${esc(r.display_name || r.id)}</div><div class="sub">${esc(r.sku || r.reserved_resource_type)}${r.quantity ? ` ×${r.quantity}` : ""}</div></td>
        <td>${esc(r.term || "—")}</td>
        <td>${esc((r.created_date || "").slice(0, 10) || "—")}</td>
        <td>${esc((r.expiry_date || "").slice(0, 10) || "—")}</td>
        <td>${badge(r)}</td>
        <td>${renew(r.renew)}</td>
        <td>${util(r.utilization_pct)}</td>
        <td>${esc(r.applied_scope_type || "—")}</td>
        <td>${esc(r.provisioning_state || "—")}</td>
      </tr>`,
      )
      .join("");
    const kpi = (label: string, value: number | string, color?: string) =>
      `<div class="kpi"><div class="kpi-val" style="${color ? `color:${color}` : ""}">${value}</div><div class="kpi-lbl">${label}</div></div>`;
    const kpis = counts
      ? [
          kpi("Reservations", counts.total),
          kpi(`Expiring ≤${data?.window_days ?? 60}d`, counts.expiring_soon, "#d97706"),
          kpi("Recently expired", counts.recently_expired, "#dc2626"),
          kpi("Urgent", counts.red, "#dc2626"),
          kpi("Not renewing", counts.non_renew, counts.non_renew ? "#dc2626" : undefined),
          kpi("Low utilization", counts.low_utilization, counts.low_utilization ? "#d97706" : undefined),
        ].join("")
      : "";
    const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Azure Reservations Report — ${esc(new Date().toISOString().slice(0, 10))}</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; color:#111827; margin:0; padding:32px; background:#f9fafb; }
  .wrap { max-width:1100px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 4px; }
  .meta { color:#6b7280; font-size:12px; margin-bottom:20px; }
  .meta b { color:#374151; }
  .kpis { display:grid; grid-template-columns:repeat(6,1fr); gap:8px; margin-bottom:20px; }
  @media (max-width:720px){ .kpis{ grid-template-columns:repeat(2,1fr);} }
  .kpi { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:10px 12px; }
  .kpi-val { font-size:20px; font-weight:700; }
  .kpi-lbl { font-size:11px; color:#6b7280; margin-top:2px; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; font-size:13px; }
  thead th { text-align:left; background:#f3f4f6; color:#6b7280; font-size:11px; text-transform:uppercase; letter-spacing:.04em; padding:8px 10px; }
  td { padding:8px 10px; border-top:1px solid #f0f0f0; vertical-align:top; }
  .name { font-weight:600; }
  .sub { color:#6b7280; font-size:11px; }
  .pill { display:inline-block; padding:1px 6px; border-radius:4px; font-size:11px; font-weight:600; }
  .pill-green { background:#dcfce7; color:#15803d; }
  .pill-red { background:#fee2e2; color:#b91c1c; }
  footer { margin-top:16px; color:#9ca3af; font-size:11px; }
  @media print { body{ background:#fff; padding:0; } .kpi, table { border-color:#d1d5db; } }
</style></head>
<body><div class="wrap">
  <h1>Azure Reservations Report</h1>
  <div class="meta">
    <b>Scope:</b> ${esc(scopeLabel)} &nbsp;·&nbsp;
    <b>Generated:</b> ${esc(new Date().toLocaleString())} &nbsp;·&nbsp;
    <b>Window:</b> ±${data?.window_days ?? 60} days &nbsp;·&nbsp;
    <b>Filters:</b> ${esc(filterLabel())} &nbsp;·&nbsp;
    <b>Showing:</b> ${sorted.length}${counts ? ` of ${counts.total}` : ""}
  </div>
  <div class="kpis">${kpis}</div>
  <table>
    <thead><tr><th>Reservation</th><th>Term</th><th>Created</th><th>Expires</th><th>Countdown</th><th>Renew</th><th>Utilization</th><th>Scope</th><th>Status</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="9" style="text-align:center;color:#9ca3af;padding:24px">No reservations match the current filters.</td></tr>`}</tbody>
  </table>
  <footer>Generated by Azure Support Agent — Reservations Monitor. Countdown colors: red = urgent (≤30d or recently expired), amber = within window, grey = healthy.</footer>
</div></body></html>`;
    download(`reservations-report-${stamp()}.html`, "text/html;charset=utf-8", html);
    setMsg({ text: `Exported rich HTML report (${sorted.length} reservation${sorted.length === 1 ? "" : "s"})`, ok: true });
  }

  function runExport(fn: () => void | Promise<void>) {
    setExportOpen(false);
    void fn();
  }

  async function doRefresh() {
    setRefreshing(true);
    setMsg(null);
    try {
      const fresh = await api.refreshReservations(demo, connId);
      qc.setQueryData(["reservations", demo, connId], fresh);
      if (showDigest) qc.invalidateQueries({ queryKey: ["reservations-digest", demo, connId] });
      if (fresh.error) setMsg({ text: fresh.error, ok: false });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setRefreshing(false);
    }
  }

  const neverLoaded = !!data?.never_loaded && !demo;
  const notConfigured = data && !data.connection_configured && !demo;

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Reservations Monitor</h1>
            <p className="text-xs text-gray-500">
              Azure reservations expiring within, or expired within the last,{" "}
              <b>{data?.window_days ?? 60} days</b> — with auto-renew and utilization. Powers the weekly digest.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {!demo && <ConnectionScopePicker value={connId} onChange={setConnId} />}
            <label className="flex items-center gap-1.5 text-xs text-gray-600">
              <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />
              Demo data
            </label>
            <span className="text-[11px] text-gray-400">Updated {agoText(data?.age_seconds ?? null)}</span>
            <button
              onClick={doRefresh}
              disabled={refreshing}
              className="rounded-lg bg-brand-dark px-3 py-1.5 text-sm font-medium text-white transition hover:opacity-90 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
        {data?.demo && (
          <div className="mt-2 rounded bg-blue-50 px-2.5 py-1 text-[11px] text-blue-700">
            Showing synthetic demo reservations. Untick “Demo data” for your live tenant.
          </div>
        )}
        {/* RU4 — stale-data nudge once past the 6h backend TTL. */}
        {!demo && data && !data.never_loaded && typeof data.age_seconds === "number" && data.age_seconds > 6 * 3600 && (
          <div className="mt-2 flex items-center gap-2 rounded bg-amber-50 px-2.5 py-1 text-[11px] text-amber-700">
            Data is {agoText(data.age_seconds)} — reservation status may have changed.
            <button onClick={doRefresh} disabled={refreshing} className="rounded border border-amber-300 px-1.5 py-0.5 font-medium hover:bg-amber-100 disabled:opacity-50">Refresh</button>
          </div>
        )}
        {msg && (
          <div className={`mt-2 rounded px-2.5 py-1 text-[11px] ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>
            {msg.text}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto p-5">
        {resQ.isLoading ? (
          <div className="p-2"><Skeleton rows={6} /></div>
        ) : notConfigured ? (
          <EmptyCard
            title="No Azure connection configured"
            body="Add a default Azure connection (Settings → Azure Tenants) whose identity can read reservations, then Refresh."
          />
        ) : neverLoaded ? (
          <EmptyCard
            title="Not loaded yet"
            body="Press Refresh to query reservation orders for this connection’s identity. This is the only step that calls Azure."
            action={
              <button
                onClick={doRefresh}
                disabled={refreshing}
                className="mt-3 rounded-lg bg-brand-dark px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                {refreshing ? "Refreshing…" : "Refresh now"}
              </button>
            }
          />
        ) : (
          <>
            {/* Summary */}
            {counts && (
              <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <Stat label="Reservations" value={counts.total} active={statusF === "all" && renewF === "all" && utilF === "all"} onClick={() => { setStatusF("all"); setRenewF("all"); setUtilF("all"); }} />
                <Stat label={`Expiring ≤${data?.window_days ?? 60}d`} value={counts.expiring_soon} tone="text-amber-600" active={statusF === "expiring_soon"} onClick={() => toggle(statusF, setStatusF, "expiring_soon", "all")} />
                <Stat label="Recently expired" value={counts.recently_expired} tone="text-red-600" active={statusF === "recently_expired"} onClick={() => toggle(statusF, setStatusF, "recently_expired", "all")} />
                <Stat label="Urgent" value={counts.red} tone="text-red-600" active={statusF === "urgent"} onClick={() => toggle(statusF, setStatusF, "urgent", "all")} />
                <Stat label="Not renewing" value={counts.non_renew} tone={counts.non_renew ? "text-red-600" : undefined} active={renewF === "none"} onClick={() => toggle(renewF, setRenewF, "none", "all")} />
                <Stat label="Low utilization" value={counts.low_utilization} tone={counts.low_utilization ? "text-amber-600" : undefined} active={utilF === "low"} onClick={() => toggle(utilF, setUtilF, "low", "all")} />
              </div>
            )}

            {data?.error && (
              <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{data.error}</div>
            )}

            {/* Table */}
            {items.length === 0 ? (
              <EmptyCard
                title="No reservations found"
                body={
                  demo
                    ? "Demo set is empty."
                    : "No reservation orders are visible to this connection’s identity. If you expect some, grant it the “Reservations Reader” role at the reservation order or tenant scope."
                }
              />
            ) : (
              <>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <InlineSearch q={q} setQ={setQ} shown={sorted.length} total={items.length} placeholder="Search reservations…" width="w-56" />
                <select value={statusF} onChange={(e) => setStatusF(e.target.value)} title="Filter by status" className="rounded-md border px-2 py-1 text-xs text-gray-600">
                  <option value="all">Status: All</option>
                  <option value="urgent">Urgent</option>
                  <option value="expiring_soon">Expiring soon</option>
                  <option value="recently_expired">Recently expired</option>
                  <option value="active">Active</option>
                </select>
                <select value={renewF} onChange={(e) => setRenewF(e.target.value)} title="Filter by renew" className="rounded-md border px-2 py-1 text-xs text-gray-600">
                  <option value="all">Renew: All</option>
                  <option value="auto">Auto-renew</option>
                  <option value="none">Not renewing</option>
                </select>
                <select value={utilF} onChange={(e) => setUtilF(e.target.value)} title="Filter by utilization" className="rounded-md border px-2 py-1 text-xs text-gray-600">
                  <option value="all">Utilization: All</option>
                  <option value="low">Low (&lt;25%)</option>
                </select>
                <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} title="Sort reservations" className="rounded-md border px-2 py-1 text-xs text-gray-600">
                  <option value="days">Sort: Countdown</option>
                  <option value="utilization">Sort: Utilization</option>
                  <option value="name">Sort: Name</option>
                </select>
                <div ref={exportRef} className="relative">
                  <button
                    onClick={() => setExportOpen((o) => !o)}
                    disabled={sorted.length === 0}
                    title="Export the current view"
                    className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                  >
                    ⬇ Export
                    <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" /></svg>
                  </button>
                  {exportOpen && (
                    <div className="absolute right-0 z-20 mt-1 w-56 rounded-lg border bg-white p-1 shadow-lg">
                      <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-gray-400">
                        Export {sorted.length} reservation{sorted.length === 1 ? "" : "s"}
                      </div>
                      <button onClick={() => runExport(exportHtml)} className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-gray-100">
                        <span>📄</span><span><span className="block font-medium text-gray-800">Rich HTML report</span><span className="block text-[10px] text-gray-400">Styled, printable · Save as PDF</span></span>
                      </button>
                      <button onClick={() => runExport(exportCsv)} className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-gray-100">
                        <span>📊</span><span><span className="block font-medium text-gray-800">CSV (Excel)</span><span className="block text-[10px] text-gray-400">Full columns · UTF-8</span></span>
                      </button>
                      <button onClick={() => runExport(exportJson)} className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-gray-100">
                        <span>🧩</span><span><span className="block font-medium text-gray-800">JSON</span><span className="block text-[10px] text-gray-400">Raw data + summary counts</span></span>
                      </button>
                      <button onClick={() => runExport(copyMarkdown)} className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-gray-100">
                        <span>📋</span><span><span className="block font-medium text-gray-800">Copy as Markdown</span><span className="block text-[10px] text-gray-400">Paste into tickets / wikis</span></span>
                      </button>
                    </div>
                  )}
                </div>
              </div>
              {/* RU2 — active filter chips. */}
              {chips.length > 0 && (
                <div className="mb-2 flex flex-wrap items-center gap-1.5">
                  {chips.map((c) => (
                    <span key={c.key} className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
                      {c.label}
                      <button onClick={c.clear} className="text-brand/60 hover:text-brand">✕</button>
                    </span>
                  ))}
                  <button onClick={() => { setStatusF("all"); setRenewF("all"); setUtilF("all"); setQ(""); }} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear all</button>
                </div>
              )}
              <div className="overflow-hidden rounded-lg border bg-white">
                <table className="w-full text-left text-sm">
                  <thead className="bg-gray-50 text-[11px] uppercase tracking-wide text-gray-500">
                    <tr>
                      <th className="px-3 py-2">Reservation</th>
                      <th className="px-3 py-2">Term</th>
                      <th className="px-3 py-2">Created</th>
                      <th className="px-3 py-2">Expires</th>
                      <th className="px-3 py-2">Countdown</th>
                      <th className="px-3 py-2">Renew</th>
                      <th className="px-3 py-2">Utilization</th>
                      <th className="px-3 py-2">Scope</th>
                      <th className="px-3 py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map((r: ReservationItem) => (
                      <tr key={r.id} className={`border-t ${SEV_ROW[r.severity] ?? ""}`}>
                        <td className="px-3 py-2">
                          <div className="font-medium text-gray-900">{r.display_name || r.id}</div>
                          <div className="text-[11px] text-gray-500">{r.sku || r.reserved_resource_type}{r.quantity ? ` ×${r.quantity}` : ""}</div>
                        </td>
                        <td className="px-3 py-2 text-gray-600">{r.term || "—"}</td>
                        <td className="px-3 py-2 text-gray-600">{(r.created_date || "").slice(0, 10) || "—"}</td>
                        <td className={`px-3 py-2 font-medium ${SEV_TEXT[r.severity] ?? "text-gray-700"}`}>{(r.expiry_date || "").slice(0, 10) || "—"}</td>
                        <td className={`px-3 py-2 ${SEV_TEXT[r.severity] ?? "text-gray-700"}`}>{daysLabel(r.days_until)}</td>
                        <td className="px-3 py-2">{renewBadge(r.renew)}</td>
                        <td className="px-3 py-2">{utilBadge(r.utilization_pct)}</td>
                        <td className="px-3 py-2 text-gray-600">{r.applied_scope_type || "—"}</td>
                        <td className="px-3 py-2 text-gray-600">{r.provisioning_state || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {sorted.length === 0 && <p className="py-4 text-center text-xs text-gray-400">No reservations match the current filters.</p>}
              </>
            )}

            {/* Digest preview */}
            <div className="mt-4 rounded-lg border bg-white">
              <button
                onClick={() => setShowDigest((v) => !v)}
                className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium text-gray-800"
              >
                <span>Weekly digest preview</span>
                <span className="text-xs text-gray-400">{showDigest ? "Hide" : "Show"}</span>
              </button>
              {showDigest && (
                <div className="border-t px-3 py-3">
                  <p className="mb-2 text-[11px] text-gray-500">
                    Exactly what the weekly email + in-app digest would contain. Email delivery stays <b>disabled</b> until
                    enabled in settings, so reviewing this sends nothing.
                  </p>
                  {digestQ.isLoading ? (
                    <div className="text-sm text-gray-400">Loading preview…</div>
                  ) : digestQ.data ? (
                    <>
                      <div className="mb-2 text-sm text-gray-700">{digestQ.data.summary}</div>
                      <div
                        className="overflow-auto rounded border bg-gray-50 p-2 text-sm"
                        // The preview HTML is generated server-side from your own reservation data.
                        dangerouslySetInnerHTML={{ __html: digestQ.data.html }}
                      />
                    </>
                  ) : (
                    <div className="text-sm text-gray-400">No preview available.</div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function EmptyCard({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return (
    <div className="mx-auto mt-10 max-w-md rounded-lg border bg-white p-6 text-center">
      <div className="text-sm font-medium text-gray-900">{title}</div>
      <div className="mt-1 text-xs text-gray-500">{body}</div>
      {action}
    </div>
  );
}
