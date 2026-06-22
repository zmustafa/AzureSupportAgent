import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type ReservationItem,
  type ReservationsSnapshot,
  type ReservationsDigestPreview,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
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

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

export function ReservationsMonitorPanel() {
  const qc = useQueryClient();
  const [demo, setDemo] = useState(false);
  const [connId, setConnId] = usePersistedState<string>("azsup.reservations.connId", "");
  const [refreshing, setRefreshing] = useState(false);
  const [showDigest, setShowDigest] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // Selecting the view only READS the server cache (no Azure call), so it's safe to load
  // on mount. A miss returns never_loaded so we prompt for Refresh.
  const resQ = useQuery({
    queryKey: ["reservations", demo, connId],
    queryFn: () => api.reservationsOverview(demo, connId),
  });
  const data: ReservationsSnapshot | undefined = resQ.data;
  const items = data?.items ?? [];
  const counts = data?.counts;

  const digestQ = useQuery<ReservationsDigestPreview>({
    queryKey: ["reservations-digest", demo, connId],
    queryFn: () => api.reservationsDigestPreview(demo, connId),
    enabled: showDigest,
  });

  const sorted = useMemo(() => items, [items]);

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
        {msg && (
          <div className={`mt-2 rounded px-2.5 py-1 text-[11px] ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>
            {msg.text}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-auto p-5">
        {resQ.isLoading ? (
          <div className="text-sm text-gray-400">Loading…</div>
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
                <Stat label="Reservations" value={counts.total} />
                <Stat label={`Expiring ≤${data?.window_days ?? 60}d`} value={counts.expiring_soon} tone="text-amber-600" />
                <Stat label="Recently expired" value={counts.recently_expired} tone="text-red-600" />
                <Stat label="Urgent" value={counts.red} tone="text-red-600" />
                <Stat label="Not renewing" value={counts.non_renew} tone={counts.non_renew ? "text-red-600" : undefined} />
                <Stat label="Low utilization" value={counts.low_utilization} tone={counts.low_utilization ? "text-amber-600" : undefined} />
              </div>
            )}

            {data?.error && (
              <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{data.error}</div>
            )}

            {/* Table */}
            {sorted.length === 0 ? (
              <EmptyCard
                title="No reservations found"
                body={
                  demo
                    ? "Demo set is empty."
                    : "No reservation orders are visible to this connection’s identity. If you expect some, grant it the “Reservations Reader” role at the reservation order or tenant scope."
                }
              />
            ) : (
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
