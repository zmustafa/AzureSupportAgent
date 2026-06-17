import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type RadarEvent, type RadarModelItem, type RadarSnapshot } from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { SubscriptionScopePicker } from "./SubscriptionScopePicker";

const SEV_DOT: Record<string, string> = { red: "bg-red-500", amber: "bg-amber-500", grey: "bg-gray-400" };
const SEV_TEXT: Record<string, string> = { red: "text-red-600", amber: "text-amber-600", grey: "text-gray-500" };
const SEV_BORDER: Record<string, string> = {
  red: "border-red-300 bg-red-50",
  amber: "border-amber-300 bg-amber-50",
  grey: "border-gray-200 bg-white",
};
const STATUS_LABEL: Record<string, string> = {
  new: "New",
  acknowledged: "Acknowledged",
  migration_planned: "Migration planned",
  done: "Done",
  waived: "Waived",
};
const STAGE_TONE: Record<string, string> = {
  preview: "bg-blue-100 text-blue-700",
  ga: "bg-green-100 text-green-700",
  deprecated: "bg-amber-100 text-amber-700",
  retired: "bg-red-100 text-red-700",
  unknown: "bg-gray-100 text-gray-600",
};

function daysLabel(d: number | null): string {
  if (d === null) return "TBD";
  if (d < 0) return `${Math.abs(d)}d overdue`;
  return `${d} days`;
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

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

export function RetirementRadarPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.radar.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.radar.workloadId", "");
  const [subId, setSubId] = usePersistedState("azsup.radar.subId", "");
  const [subName, setSubName] = usePersistedState("azsup.radar.subName", "");
  const [typeFilter, setTypeFilter] = useState<"all" | "retirement" | "breaking_change">("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [onlyUnowned, setOnlyUnowned] = useState(false);
  const [drawer, setDrawer] = useState<RadarEvent | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [runbook, setRunbook] = useState<{ title: string; text: string; usedAi: boolean } | null>(null);
  const [ticketFor, setTicketFor] = useState<string | null>(null);
  const [waiveFor, setWaiveFor] = useState<RadarEvent | null>(null);
  const [waiveReason, setWaiveReason] = useState("");

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );

  const workloads = workloadsQ.data?.workloads ?? [];
  // No default selection: the radar must NOT auto-fetch on page visit. It only loads once
  // the user explicitly picks a workload (or enters a subscription).
  const effectiveWorkloadId = scopeKind === "workload" ? workloadId : "";
  const params = scopeKind === "workload" ? { workload_id: effectiveWorkloadId } : { subscription_id: subId };
  const enabled = scopeKind === "workload" ? !!effectiveWorkloadId : !!subId;

  const radarQ = useQuery({
    queryKey: ["radar", scopeKind, effectiveWorkloadId, subId],
    queryFn: () => api.radarOverview(params),
    enabled,
  });
  const data: RadarSnapshot | undefined = radarQ.data;
  const events = data?.events ?? [];
  const models = data?.model_items ?? [];

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter((e) => {
      if (typeFilter !== "all" && e.change_type !== typeFilter) return false;
      if (statusFilter !== "all" && (e.status || "new") !== statusFilter) return false;
      if (onlyUnowned && !e.unowned) return false;
      if (q && !`${e.title} ${e.service} ${e.tracking_id} ${e.recommended_replacement}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [events, typeFilter, statusFilter, onlyUnowned, query]);

  async function doRefresh() {
    setRefreshing(true);
    setMsg(null);
    try {
      const fresh = await api.refreshRadar(params);
      qc.setQueryData(["radar", scopeKind, effectiveWorkloadId, subId], fresh);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setRefreshing(false);
    }
  }

  async function setState(ev: RadarEvent, patch: { status?: string; assignee?: string; waive_reason?: string }) {
    setBusy(`state:${ev.tracking_id}`);
    setMsg(null);
    try {
      await api.updateRadarState({ tracking_id: ev.tracking_id, ...patch });
      const fresh = await api.radarOverview(params);
      qc.setQueryData(["radar", scopeKind, effectiveWorkloadId, subId], fresh);
      if (drawer && drawer.tracking_id === ev.tracking_id) {
        setDrawer(fresh.events.find((x) => x.tracking_id === ev.tracking_id) ?? null);
      }
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function genRunbook(ev: RadarEvent) {
    setBusy(`runbook:${ev.tracking_id}`);
    setMsg(null);
    try {
      const r = await api.radarRunbook({ event: ev });
      setRunbook({ title: ev.title || ev.service, text: r.runbook, usedAi: r.used_ai });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function createTicket(ev: RadarEvent, connectorId: string) {
    setBusy(`ticket:${ev.tracking_id}`);
    setMsg(null);
    try {
      const r = await api.createRadarTicket({ connector_id: connectorId, item: ev });
      setMsg({ text: r.ok ? `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.` : r.detail || "Ticket failed.", ok: !!r.ok });
      setTicketFor(null);
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
      const r = await api.registerRadarFindings({
        workload_id: effectiveWorkloadId,
        workload_name: data?.scope_name ?? "",
        items: filtered,
      });
      setMsg({ text: `Registered ${r.finding_count} Reliability-pillar finding(s).`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function previewDigest() {
    setBusy("digest");
    setMsg(null);
    try {
      const r = await api.radarDigestPreview(params);
      setMsg({ text: `Scheduled digest would push: ${r.summary} (lead days ${r.lead_days.join("/")}).`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  // Investigate → War Room: deep mode + workload + a pre-seeded prompt carrying impacted list + tracking ID.
  function investigate(ev: RadarEvent) {
    const impacted = ev.impacted_resources.slice(0, 20).map((r) => r.name || r.id).join(", ");
    const prompt =
      `War Room: investigate the upcoming Azure ${ev.change_type === "breaking_change" ? "breaking change" : "retirement"} "${ev.title}" ` +
      `(tracking ID ${ev.tracking_id}, planned ${ev.retirement_date || "TBD"}, ${daysLabel(ev.days_until)} away). ` +
      `Impacted resources: ${impacted || "unknown"}. Recommended replacement: ${ev.recommended_replacement || "see migration guidance"}. ` +
      `Confirm current state in Azure, assess blast radius, and produce a migration plan.`;
    try {
      sessionStorage.setItem("azsup.warRoomHandoff", JSON.stringify({ workloadId: effectiveWorkloadId, prompt }));
    } catch {
      /* ignore */
    }
    navigate("/chat");
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              📡 Retirement &amp; Breaking-Change Radar
            </h1>
            <p className="text-xs text-gray-500">
              Service Health retirements + Advisor recommendations, mapped to your workloads and owners — so nothing
              slips past a deadline.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              <button
                onClick={() => setScopeKind("workload")}
                className={`rounded-md px-2.5 py-1 ${scopeKind === "workload" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}
              >
                Workload
              </button>
              <button
                onClick={() => setScopeKind("subscription")}
                className={`rounded-md px-2.5 py-1 ${scopeKind === "subscription" ? "bg-white font-medium shadow-sm" : "text-gray-500"}`}
              >
                Subscription
              </button>
            </div>
            {scopeKind === "workload" ? (
              <select
                value={effectiveWorkloadId}
                onChange={(e) => setWorkloadId(e.target.value)}
                className="max-w-[240px] rounded-lg border px-2 py-1.5 text-xs"
              >
                <option value="">Select a workload…</option>
                {workloads.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            ) : (
              <SubscriptionScopePicker
                value={subId}
                valueName={subName}
                onPick={(id, name) => {
                  setSubId(id);
                  setSubName(name);
                }}
              />
            )}
            <button
              onClick={doRefresh}
              disabled={refreshing || !enabled}
              className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : "↻ Refresh"}
            </button>
          </div>
        </div>
        {data && (
          <div className="mt-1 text-[11px] text-gray-400">
            {data.never_loaded ? (
              <span className="text-amber-600">Not loaded yet — press Refresh</span>
            ) : (
              <>
                {data.demo ? "Demo data · " : data.connection_configured ? "" : "No Azure connection · "}
                updated {agoText(data.age_seconds)} {data.stale_cache ? "· stale" : ""}
                {data.error ? ` · ${data.error}` : ""}
              </>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {!enabled ? (
          <div className="p-8 text-center text-sm text-gray-500">
            {scopeKind === "workload"
              ? "Select a workload to load the radar."
              : "Enter a subscription to load the radar."}
          </div>
        ) : radarQ.isLoading ? (
          <div className="p-8 text-center text-sm text-gray-500">Loading radar…</div>
        ) : !data ? (
          <div className="p-8 text-center text-sm text-gray-500">Pick a scope to load the radar.</div>
        ) : data.never_loaded ? (
          <div className="mx-auto max-w-2xl py-16 text-center">
            <div className="text-3xl">📡</div>
            <h2 className="mt-2 text-base font-semibold text-gray-900">Radar not loaded yet</h2>
            <p className="mt-1 text-sm text-gray-500">
              Scanning Azure Service Health &amp; Advisor for upcoming retirements and breaking changes
              takes a moment, so it doesn&apos;t run automatically. Press Refresh to build the snapshot —
              it&apos;s then cached until you refresh again.
            </p>
            {msg && !msg.ok && (
              <div className="mx-auto mt-3 max-w-md rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {msg.text}
              </div>
            )}
            <button
              onClick={doRefresh}
              disabled={refreshing}
              className="mt-4 rounded-md border bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {refreshing ? "Scanning…" : "↻ Refresh now"}
            </button>
            {!data.connection_configured && (
              <p className="mt-3 text-xs text-amber-700">
                No default Azure connection — configure one in Settings → Azure Tenants first.
              </p>
            )}
          </div>
        ) : (
          <>
            {msg && (
              <div className={`mb-3 rounded-md border px-3 py-2 text-sm ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>
                {msg.text}
              </div>
            )}

            {/* Countdown rail */}
            <div className="mb-4">
              <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">Nearest deadlines</div>
              {data.rail.length === 0 ? (
                <div className="rounded-lg border bg-white px-3 py-4 text-sm text-gray-400">No upcoming deadlines in scope.</div>
              ) : (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                  {data.rail.map((c) => (
                    <button
                      key={c.id}
                      onClick={() => setDrawer(events.find((e) => e.id === c.id) ?? null)}
                      className={`rounded-lg border px-3 py-2 text-left transition hover:shadow ${SEV_BORDER[c.severity]}`}
                    >
                      <div className={`text-lg font-semibold ${SEV_TEXT[c.severity]}`}>{daysLabel(c.days_until)}</div>
                      <div className="truncate text-[12px] font-medium text-gray-800" title={c.title}>{c.title}</div>
                      <div className="text-[11px] text-gray-500">{c.impacted_count} impacted</div>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* KPIs */}
            <div className="mb-4 grid grid-cols-3 gap-2 sm:grid-cols-6">
              <Stat label="Total events" value={String(data.counts.total)} />
              <Stat label="Retirements" value={String(data.counts.retirement)} />
              <Stat label="Breaking changes" value={String(data.counts.breaking_change)} />
              <Stat label="< 30 days" value={String(data.counts.red)} tone="text-red-600" />
              <Stat label="Unowned" value={String(data.counts.unowned)} tone={data.counts.unowned ? "text-amber-600" : undefined} />
              <Stat label="Impacted resources" value={String(data.counts.impacted_total)} />
            </div>

            {/* Toolbar */}
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <div className="inline-flex overflow-hidden rounded-md border text-sm">
                {(["all", "retirement", "breaking_change"] as const).map((t) => (
                  <button
                    key={t}
                    onClick={() => setTypeFilter(t)}
                    className={`px-3 py-1.5 ${typeFilter === t ? "bg-gray-900 text-white" : "bg-white hover:bg-gray-50"}`}
                  >
                    {t === "all" ? "All" : t === "retirement" ? "Retirements" : "Breaking changes"}
                  </button>
                ))}
              </div>
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="rounded-md border px-2 py-1.5 text-sm">
                <option value="all">All statuses</option>
                {Object.entries(STATUS_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
              <label className="flex items-center gap-1.5 text-sm text-gray-600">
                <input type="checkbox" checked={onlyUnowned} onChange={(e) => setOnlyUnowned(e.target.checked)} />
                Unowned only
              </label>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" className="rounded-md border px-2 py-1.5 text-sm" />
              <div className="ml-auto flex items-center gap-2">
                <button onClick={previewDigest} disabled={busy === "digest"} className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50">
                  📨 Preview digest
                </button>
                <button onClick={registerFindings} disabled={busy === "findings" || filtered.length === 0} className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50">
                  🛡️ Register findings
                </button>
              </div>
            </div>

            {/* Main table */}
            <div className="overflow-x-auto rounded-lg border bg-white">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-3 py-2">Service / feature</th>
                    <th className="px-3 py-2">Type</th>
                    <th className="px-3 py-2">Date</th>
                    <th className="px-3 py-2">Days</th>
                    <th className="px-3 py-2">Impacted</th>
                    <th className="px-3 py-2">Replacement</th>
                    <th className="px-3 py-2">Owner</th>
                    <th className="px-3 py-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr><td colSpan={8} className="px-3 py-6 text-center text-gray-400">No matching items.</td></tr>
                  ) : (
                    filtered.map((e) => (
                      <tr key={e.id} className="cursor-pointer border-t hover:bg-gray-50" onClick={() => setDrawer(e)}>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-2">
                            <span className={`inline-block h-2 w-2 rounded-full ${SEV_DOT[e.severity]}`} />
                            <span className="font-medium text-gray-900">{e.title || e.service}</span>
                          </div>
                          <div className="text-[11px] text-gray-400">{e.tracking_id}</div>
                        </td>
                        <td className="px-3 py-2">
                          <span className={`rounded px-1.5 py-0.5 text-[11px] ${e.change_type === "breaking_change" ? "bg-purple-100 text-purple-700" : "bg-sky-100 text-sky-700"}`}>
                            {e.change_type === "breaking_change" ? "Breaking change" : "Retirement"}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-gray-600">{e.retirement_date || "TBD"}</td>
                        <td className={`px-3 py-2 font-medium ${SEV_TEXT[e.severity]}`}>{daysLabel(e.days_until)}</td>
                        <td className="px-3 py-2 text-gray-600">{e.impacted_count}</td>
                        <td className="px-3 py-2 max-w-[220px] truncate text-gray-600" title={e.recommended_replacement}>{e.recommended_replacement || "—"}</td>
                        <td className="px-3 py-2">
                          {e.unowned ? (
                            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-700">Unowned</span>
                          ) : (
                            <span className="text-gray-700">{e.owner}</span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-600">{STATUS_LABEL[e.status || "new"]}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* AI-model lifecycle lane */}
            <div className="mt-6">
              <div className="mb-2 flex items-center gap-2">
                <h2 className="text-sm font-semibold text-gray-900">🤖 AI model lifecycle</h2>
                <span className="text-[11px] text-gray-400">Azure OpenAI / Foundry deployments — these fail hard (404/410) at retirement</span>
              </div>
              <div className="overflow-x-auto rounded-lg border bg-white">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                    <tr>
                      <th className="px-3 py-2">Deployment</th>
                      <th className="px-3 py-2">Model</th>
                      <th className="px-3 py-2">Stage</th>
                      <th className="px-3 py-2">Retires</th>
                      <th className="px-3 py-2">Days</th>
                      <th className="px-3 py-2">Replacement</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.length === 0 ? (
                      <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">No Azure OpenAI deployments in scope.</td></tr>
                    ) : (
                      models.map((m: RadarModelItem) => (
                        <tr key={m.id} className="border-t">
                          <td className="px-3 py-2 text-gray-900">{m.account}/{m.deployment}</td>
                          <td className="px-3 py-2 text-gray-600">{m.model} {m.model_version}</td>
                          <td className="px-3 py-2">
                            <span className={`rounded px-1.5 py-0.5 text-[11px] ${STAGE_TONE[m.stage] || STAGE_TONE.unknown}`}>{m.stage}</span>
                          </td>
                          <td className="px-3 py-2 text-gray-600">{m.retirement_date || (m.matched ? "—" : "unknown")}</td>
                          <td className={`px-3 py-2 font-medium ${SEV_TEXT[m.severity]}`}>{daysLabel(m.days_until)}</td>
                          <td className="px-3 py-2 max-w-[220px] truncate text-gray-600" title={m.replacement}>{m.replacement || "—"}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Drill-down drawer */}
      {drawer && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={() => setDrawer(null)}>
          <div className="h-full w-full max-w-lg overflow-auto bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="border-b px-5 py-3">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <span className={`inline-block h-2.5 w-2.5 rounded-full ${SEV_DOT[drawer.severity]}`} />
                    <h3 className="text-base font-semibold text-gray-900">{drawer.title || drawer.service}</h3>
                  </div>
                  <div className="text-[11px] text-gray-400">{drawer.tracking_id} · {drawer.sources.join(", ") || "—"}</div>
                </div>
                <button onClick={() => setDrawer(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
              </div>
            </div>
            <div className="space-y-4 px-5 py-4 text-sm">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded border px-2 py-1.5"><div className="text-[11px] text-gray-500">Type</div><div>{drawer.change_type === "breaking_change" ? "Breaking change" : "Retirement"}</div></div>
                <div className="rounded border px-2 py-1.5"><div className="text-[11px] text-gray-500">Planned date</div><div>{drawer.retirement_date || "TBD"} ({daysLabel(drawer.days_until)})</div></div>
              </div>
              {drawer.summary && <p className="text-gray-700">{drawer.summary}</p>}
              {drawer.recommended_replacement && (
                <div><div className="text-[11px] font-medium uppercase text-gray-500">Recommended replacement</div><div className="text-gray-700">{drawer.recommended_replacement}</div></div>
              )}
              {drawer.migration_url && (
                <a href={drawer.migration_url} target="_blank" rel="noreferrer" className="inline-block text-blue-600 hover:underline">Official migration guidance ↗</a>
              )}

              {/* Status controls */}
              <div>
                <div className="mb-1 text-[11px] font-medium uppercase text-gray-500">Status</div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(STATUS_LABEL).filter(([k]) => k !== "waived").map(([k, v]) => (
                    <button
                      key={k}
                      onClick={() => setState(drawer, { status: k })}
                      disabled={busy === `state:${drawer.tracking_id}`}
                      className={`rounded border px-2 py-1 text-xs ${(drawer.status || "new") === k ? "bg-gray-900 text-white" : "bg-white hover:bg-gray-50"}`}
                    >
                      {v}
                    </button>
                  ))}
                  <button onClick={() => { setWaiveFor(drawer); setWaiveReason(drawer.waive_reason || ""); }} className={`rounded border px-2 py-1 text-xs ${drawer.status === "waived" ? "bg-gray-900 text-white" : "bg-white hover:bg-gray-50"}`}>Waive…</button>
                </div>
                {drawer.status === "waived" && drawer.waive_reason && (
                  <div className="mt-1 text-[11px] text-gray-500">Waived: {drawer.waive_reason}</div>
                )}
              </div>

              {/* Impacted resources */}
              <div>
                <div className="mb-1 text-[11px] font-medium uppercase text-gray-500">Impacted resources ({drawer.impacted_count})</div>
                <div className="max-h-48 overflow-auto rounded border">
                  <table className="w-full text-[12px]">
                    <tbody>
                      {drawer.impacted_resources.map((r) => (
                        <tr key={r.id} className="border-t">
                          <td className="px-2 py-1">
                            <div className="text-gray-800">{r.name}</div>
                            <div className="text-[10px] text-gray-400">{r.type} · {r.resource_group} · {r.region}</div>
                          </td>
                          <td className="px-2 py-1 text-right">
                            {r.unowned ? <span className="rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-700">unowned</span> : <span className="text-[11px] text-gray-600">{r.owner}</span>}
                          </td>
                        </tr>
                      ))}
                      {drawer.impacted_resources.length === 0 && (
                        <tr><td className="px-2 py-2 text-center text-gray-400">No impacted resources resolved.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Actions */}
              <div className="flex flex-wrap gap-2 border-t pt-3">
                <button onClick={() => genRunbook(drawer)} disabled={busy === `runbook:${drawer.tracking_id}`} className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50">
                  {busy === `runbook:${drawer.tracking_id}` ? "Drafting…" : "📋 Generate migration runbook"}
                </button>
                <button onClick={() => investigate(drawer)} className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50">🔎 Investigate (War Room)</button>
                <div className="relative">
                  <button onClick={() => setTicketFor(ticketFor === drawer.tracking_id ? null : drawer.tracking_id)} disabled={ticketConnectors.length === 0} className="rounded-md border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50" title={ticketConnectors.length === 0 ? "No Jira/ServiceNow connector configured" : ""}>
                    🎫 Create ticket
                  </button>
                  {ticketFor === drawer.tracking_id && (
                    <div className="absolute z-10 mt-1 w-48 rounded-md border bg-white shadow-lg">
                      {ticketConnectors.map((c) => (
                        <button key={c.id} onClick={() => createTicket(drawer, c.id)} className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-50">{c.name} ({c.type})</button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Waive modal */}
      {waiveFor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setWaiveFor(null)}>
          <div className="w-full max-w-md rounded-lg bg-white p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-2 text-base font-semibold">Waive: {waiveFor.title || waiveFor.service}</h3>
            <textarea value={waiveReason} onChange={(e) => setWaiveReason(e.target.value)} rows={3} placeholder="Reason for waiving (e.g. resource being decommissioned)…" className="w-full rounded border px-2 py-1.5 text-sm" />
            <div className="mt-3 flex justify-end gap-2">
              <button onClick={() => setWaiveFor(null)} className="rounded-md border px-3 py-1.5 text-sm">Cancel</button>
              <button
                onClick={() => { setState(waiveFor, { status: "waived", waive_reason: waiveReason }); setWaiveFor(null); }}
                disabled={!waiveReason.trim()}
                className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              >
                Waive
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Runbook modal */}
      {runbook && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6" onClick={() => setRunbook(null)}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-lg bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <h3 className="text-sm font-semibold">Migration runbook — {runbook.title} {runbook.usedAi ? "🤖" : ""}</h3>
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    const blob = new Blob([runbook.text], { type: "text/markdown" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url; a.download = "migration-runbook.md"; a.click();
                    URL.revokeObjectURL(url);
                  }}
                  className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                >
                  ⬇ Download
                </button>
                <button onClick={() => setRunbook(null)} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
              </div>
            </div>
            <pre className="flex-1 overflow-auto whitespace-pre-wrap px-4 py-3 text-[12px] leading-relaxed text-gray-800">{runbook.text}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
