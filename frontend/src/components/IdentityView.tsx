import { useMemo, useState } from "react";
import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  api,
  type IdentityFinding,
  type IdentityGroupKey,
  type IdentityOverview,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { IDENTITY_NAV, type IdentityTab } from "./navConfig";
import { AppRegistrationsView } from "./AppRegistrationsView";
import { ConnectionScopePicker } from "./ConnectionScopePicker";

const SEV_META: Record<string, { label: string; cls: string; dot: string; rank: number }> = {
  critical: { label: "Critical", cls: "bg-red-100 text-red-700", dot: "bg-red-500", rank: 4 },
  error: { label: "Error", cls: "bg-orange-100 text-orange-700", dot: "bg-orange-500", rank: 3 },
  warning: { label: "Warning", cls: "bg-amber-100 text-amber-700", dot: "bg-amber-500", rank: 2 },
  info: { label: "Info", cls: "bg-sky-100 text-sky-700", dot: "bg-sky-500", rank: 1 },
  ok: { label: "OK", cls: "bg-green-100 text-green-700", dot: "bg-green-500", rank: 0 },
};

const GROUPS: { key: IdentityGroupKey; label: string; icon: string; blurb: string }[] = [
  { key: "expiring_credentials", label: "Expiring secrets & certificates", icon: "🔑", blurb: "App / service-principal credentials at or past expiry." },
  { key: "ca_gaps", label: "Conditional-access gaps", icon: "🚧", blurb: "Disabled or report-only policies leaving access uncovered." },
  { key: "ownerless_apps", label: "App registrations without owners", icon: "👤", blurb: "No one accountable for rotating or retiring these apps." },
  { key: "users_without_mfa", label: "Privileged users without MFA", icon: "🛡️", blurb: "High-privilege accounts whose MFA is not enabled." },
  { key: "keyvault_expiry", label: "Key Vault expiry", icon: "🔐", blurb: "Key Vault certificates/secrets nearing expiry." },
];

const KPIS: { key: keyof IdentityOverview["kpis"]; label: string; group: IdentityGroupKey }[] = [
  { key: "expiring_secrets", label: "Expiring secrets", group: "expiring_credentials" },
  { key: "expiring_certs", label: "Expiring certs", group: "expiring_credentials" },
  { key: "ownerless_apps", label: "Apps w/o owner", group: "ownerless_apps" },
  { key: "users_without_mfa", label: "Users w/o MFA", group: "users_without_mfa" },
  { key: "ca_gaps", label: "CA gaps", group: "ca_gaps" },
  { key: "keyvault_expiring", label: "Key Vault expiry", group: "keyvault_expiry" },
];

const WINDOWS = [30, 60, 90];

function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function SevBadge({ sev }: { sev: string }) {
  const m = SEV_META[sev] ?? SEV_META.info;
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${m.cls}`}>{m.label}</span>;
}

function DaysBadge({ days }: { days?: number | null }) {
  if (days == null) return null;
  const expired = days < 0;
  const cls = expired
    ? "bg-red-100 text-red-700"
    : days <= 30
    ? "bg-orange-100 text-orange-700"
    : days <= 60
    ? "bg-amber-100 text-amber-700"
    : "bg-sky-100 text-sky-700";
  return (
    <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>
      {expired ? `expired ${Math.abs(days)}d` : `${days}d left`}
    </span>
  );
}

export function IdentityPanel({ tab = "overview" }: { tab?: IdentityTab }) {
  const navigate = useNavigate();
  const setTab = (v: IdentityTab) => navigate(v === "overview" ? "/identity" : `/identity/${v}`);
  const [connectionId, setConnectionId] = usePersistedState("azsup.identity.connectionId", "");
  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b bg-white px-4 pt-2">
        {IDENTITY_NAV.map(({ id: v, label }) => (
          <button
            key={v}
            onClick={() => setTab(v)}
            className={`rounded-t-lg px-3 py-1.5 text-sm font-medium ${
              tab === v ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {label}
          </button>
        ))}
        <div className="ml-auto pb-1.5">
          <ConnectionScopePicker value={connectionId} onChange={setConnectionId} />
        </div>
      </div>
      {tab === "app-registrations" ? <AppRegistrationsView connectionId={connectionId || null} /> : <IdentityFindingsPanel connectionId={connectionId || null} />}
    </div>
  );
}

function IdentityFindingsPanel({ connectionId = null }: { connectionId?: string | null }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [days, setDays] = useState(90);
  const [customDays, setCustomDays] = useState("");
  const [query, setQuery] = useState("");
  const [sevFilter, setSevFilter] = useState("all");
  const [mappedOnly, setMappedOnly] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<IdentityGroupKey>>(new Set());
  const [ticketFor, setTicketFor] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ id: string; text: string; ok: boolean } | null>(null);

  const overviewQ = useQuery({
    queryKey: ["identity", days, connectionId],
    queryFn: () => api.identityOverview(days, connectionId),
  });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );

  const data = overviewQ.data;

  // The force re-scan (slow EntraID/Graph collect) runs as a React Query MUTATION rather
  // than a local useState flag, so its in-flight status survives navigating away and back:
  // the mutation lives in the MutationCache (not the component), and useIsMutating reads it.
  // The cache write happens INSIDE the mutationFn so a result still lands if the user is on
  // another screen when the scan finishes.
  const refreshMutation = useMutation({
    mutationKey: ["identity-refresh", days, connectionId],
    mutationFn: async (d: number) => {
      const fresh = await api.refreshIdentity(d, connectionId);
      qc.setQueryData(["identity", d, connectionId], fresh);
      return fresh;
    },
    onError: (e) => setMsg({ id: "refresh", text: formatError(e), ok: false }),
  });
  // In-flight refresh for the CURRENT day window — survives unmount/remount.
  const refreshing = useIsMutating({ mutationKey: ["identity-refresh", days, connectionId] }) > 0;

  function doRefresh() {
    if (refreshing) return; // re-entrancy guard (buttons are also disabled while busy)
    refreshMutation.mutate(days);
  }

  function applyCustom() {
    const n = parseInt(customDays, 10);
    if (!isNaN(n) && n > 0) setDays(Math.min(365, n));
  }

  function toggleGroup(k: IdentityGroupKey) {
    setCollapsed((p) => {
      const n = new Set(p);
      n.has(k) ? n.delete(k) : n.add(k);
      return n;
    });
  }

  function matchesFilters(f: IdentityFinding): boolean {
    if (sevFilter !== "all" && f.severity !== sevFilter) return false;
    if (mappedOnly && !f.workload_id) return false;
    const q = query.trim().toLowerCase();
    if (q && !(`${f.title} ${f.subject} ${f.detail} ${f.workload_name ?? ""}`.toLowerCase().includes(q)))
      return false;
    return true;
  }

  function investigate(f: IdentityFinding) {
    const prompt =
      `Investigate this identity finding and recommend remediation steps: "${f.title}". ` +
      `${f.detail} ${f.workload_name ? `Owning workload: ${f.workload_name}.` : ""} ` +
      `Confirm the current state in Microsoft Entra / Azure and propose the exact fix.`;
    try {
      sessionStorage.setItem(
        "azsup.identityHandoff",
        JSON.stringify({ workloadId: f.workload_id ?? "", prompt }),
      );
    } catch {
      /* ignore */
    }
    navigate("/chat");
  }

  async function createTicket(f: IdentityFinding, connectorId: string) {
    setBusy(f.id);
    setMsg(null);
    try {
      const r = await api.createIdentityTicket({ connector_id: connectorId, finding: f });
      if (r.ok) {
        setMsg({ id: f.id, text: `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.`, ok: true });
        setTicketFor(null);
      } else {
        setMsg({ id: f.id, text: r.detail || "Ticket creation failed.", ok: false });
      }
    } catch (e) {
      setMsg({ id: f.id, text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  const scrollToGroup = (k: IdentityGroupKey) => {
    setCollapsed((p) => {
      const n = new Set(p);
      n.delete(k);
      return n;
    });
    document.getElementById(`idgroup-${k}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const visibleByGroup = useMemo(() => {
    const out: Record<string, IdentityFinding[]> = {};
    if (!data) return out;
    for (const g of GROUPS) out[g.key] = (data.groups[g.key] ?? []).filter(matchesFilters);
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, query, sevFilter, mappedOnly]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-gray-50">
      {/* Header */}
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900">Identity</h1>
            <p className="text-xs text-gray-500">
              Recurring identity risks — expiring credentials, ownerless apps, MFA &amp; conditional-access gaps.
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              {WINDOWS.map((w) => (
                <button
                  key={w}
                  onClick={() => setDays(w)}
                  className={`rounded-md px-2.5 py-1 transition ${
                    days === w ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
                  }`}
                >
                  {w}d
                </button>
              ))}
              <input
                value={customDays}
                onChange={(e) => setCustomDays(e.target.value.replace(/\D/g, ""))}
                onBlur={applyCustom}
                onKeyDown={(e) => e.key === "Enter" && applyCustom()}
                placeholder="custom"
                className="w-16 rounded-md bg-transparent px-2 py-1 text-gray-700 outline-none placeholder:text-gray-400"
              />
            </div>
            <span className="text-xs text-gray-500">
              {!data ? (
                "—"
              ) : data.never_loaded ? (
                <span className="text-amber-600">Not loaded yet — press Refresh</span>
              ) : (
                <>
                  Updated {agoText(data.age_seconds)}
                  {data.stale && <span className="ml-1 text-amber-600">· stale</span>}
                  <span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">cached</span>
                </>
              )}
            </span>
            <button
              onClick={() => void doRefresh()}
              disabled={refreshing}
              className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : "↻ Refresh"}
            </button>
          </div>
        </div>

        {/* KPI row */}
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          {KPIS.map((kpi) => {
            const count = data?.kpis[kpi.key] ?? 0;
            const sev = data?.group_severity[kpi.group] ?? "ok";
            const m = SEV_META[sev] ?? SEV_META.ok;
            return (
              <button
                key={kpi.key}
                onClick={() => scrollToGroup(kpi.group)}
                className="rounded-lg border bg-white px-3 py-2 text-left transition hover:border-gray-300 hover:shadow-sm"
              >
                <div className="flex items-center gap-1.5">
                  <span className={`h-2 w-2 rounded-full ${count > 0 ? m.dot : "bg-gray-300"}`} />
                  <span className="text-xl font-semibold text-gray-900">{count}</span>
                </div>
                <div className="truncate text-[11px] text-gray-500">{kpi.label}</div>
              </button>
            );
          })}
        </div>

        {/* Filters */}
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search findings…"
            className="w-48 rounded-lg border px-2.5 py-1.5 outline-none focus:border-gray-400"
          />
          <select
            value={sevFilter}
            onChange={(e) => setSevFilter(e.target.value)}
            className="rounded-lg border px-2 py-1.5 outline-none focus:border-gray-400"
          >
            <option value="all">All severities</option>
            <option value="critical">Critical</option>
            <option value="error">Error</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
          <label className="flex items-center gap-1.5 text-gray-600">
            <input type="checkbox" checked={mappedOnly} onChange={(e) => setMappedOnly(e.target.checked)} />
            Mapped to workload only
          </label>
          {!data?.connection_configured && (
            <span className="rounded bg-amber-50 px-2 py-1 text-amber-700">
              No default Azure connection — configure one in Settings → Azure Tenants.
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        {overviewQ.isLoading ? (
          <div className="py-16 text-center text-sm text-gray-400">Loading identity posture…</div>
        ) : overviewQ.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {formatError(overviewQ.error)}
          </div>
        ) : data?.never_loaded ? (
          <div className="mx-auto max-w-2xl py-16 text-center">
            <div className="text-3xl">🔐</div>
            <h2 className="mt-2 text-base font-semibold text-gray-900">Identity posture not loaded yet</h2>
            <p className="mt-1 text-sm text-gray-500">
              Scanning Microsoft Entra &amp; Azure for expiring credentials, ownerless apps, MFA and
              conditional-access gaps takes a moment, so it doesn&apos;t run automatically. Press Refresh
              to build the snapshot — it&apos;s then cached until you refresh again.
            </p>
            {msg && !msg.ok && (
              <div className="mx-auto mt-3 max-w-md rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700">
                {msg.text}
              </div>
            )}
            <button
              onClick={() => void doRefresh()}
              disabled={refreshing}
              className="mt-4 rounded-lg border bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
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
          <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
            {msg && msg.id === "refresh" && (
              <div
                className={`rounded-lg border p-2 text-xs ${
                  msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"
                }`}
              >
                {msg.text}
              </div>
            )}
            {GROUPS.map((g) => {
              const items = visibleByGroup[g.key] ?? [];
              const total = data?.groups[g.key]?.length ?? 0;
              const err = data?.errors[g.key];
              const isCollapsed = collapsed.has(g.key);
              return (
                <section key={g.key} id={`idgroup-${g.key}`} className="rounded-xl border bg-white">
                  <button
                    onClick={() => toggleGroup(g.key)}
                    className="flex w-full items-center gap-2 px-4 py-3 text-left"
                  >
                    <span className="text-lg">{g.icon}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <h2 className="text-sm font-semibold text-gray-900">{g.label}</h2>
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">{total}</span>
                        {g.key === "users_without_mfa" && data?.meta.mfa_sampled && (
                          <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] text-sky-700">
                            sampled {data.meta.mfa_scanned}
                          </span>
                        )}
                      </div>
                      <p className="truncate text-[11px] text-gray-500">{g.blurb}</p>
                    </div>
                    <span className="text-gray-400">{isCollapsed ? "▸" : "▾"}</span>
                  </button>

                  {err && (
                    <div className="mx-4 mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
                      {err}
                    </div>
                  )}

                  {!isCollapsed && (
                    <div className="divide-y border-t">
                      {items.length === 0 ? (
                        <div className="px-4 py-6 text-center text-xs text-gray-400">
                          {total === 0 ? "No findings — looking good." : "No findings match the current filters."}
                        </div>
                      ) : (
                        items.map((f) => (
                          <div key={f.id} className="flex flex-wrap items-start gap-2 px-4 py-2.5">
                            <div className="min-w-0 flex-1">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <SevBadge sev={f.severity} />
                                <DaysBadge days={f.days_left} />
                                <span className="text-sm text-gray-900">{f.title}</span>
                              </div>
                              <div className="mt-0.5 text-xs text-gray-500">{f.detail}</div>
                              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
                                {f.workload_name ? (
                                  <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-700">
                                    ⬡ {f.workload_name}
                                  </span>
                                ) : (
                                  <span className="text-gray-400">— no mapped workload</span>
                                )}
                                {f.expires_at && (
                                  <span className="text-gray-400">
                                    expires {new Date(f.expires_at).toLocaleDateString()}
                                  </span>
                                )}
                              </div>
                            </div>
                            <div className="flex items-center gap-1.5">
                              {ticketFor === f.id ? (
                                <div className="flex items-center gap-1">
                                  {ticketConnectors.length > 0 ? (
                                    <select
                                      autoFocus
                                      disabled={busy === f.id}
                                      onChange={(e) => e.target.value && void createTicket(f, e.target.value)}
                                      defaultValue=""
                                      className="rounded-md border px-1.5 py-1 text-[11px]"
                                    >
                                      <option value="" disabled>
                                        {busy === f.id ? "Creating…" : "Pick connector…"}
                                      </option>
                                      {ticketConnectors.map((c) => (
                                        <option key={c.id} value={c.id}>
                                          {c.name} ({c.type})
                                        </option>
                                      ))}
                                    </select>
                                  ) : (
                                    <span className="text-[11px] text-gray-400">No Jira/ServiceNow connector</span>
                                  )}
                                  <button
                                    onClick={() => setTicketFor(null)}
                                    className="rounded-md px-1.5 py-1 text-[11px] text-gray-400 hover:bg-gray-100"
                                  >
                                    ✕
                                  </button>
                                </div>
                              ) : (
                                <button
                                  onClick={() => setTicketFor(f.id)}
                                  title="Create remediation ticket"
                                  className="rounded-md border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
                                >
                                  🎫 Ticket
                                </button>
                              )}
                              <button
                                onClick={() => investigate(f)}
                                title="Investigate in a new chat"
                                className="rounded-md border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
                              >
                                🔎 Investigate
                              </button>
                            </div>
                            {msg && msg.id === f.id && (
                              <div
                                className={`basis-full text-right text-[11px] font-medium ${
                                  msg.ok ? "text-green-700" : "text-red-600"
                                }`}
                              >
                                {msg.text}
                              </div>
                            )}
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
