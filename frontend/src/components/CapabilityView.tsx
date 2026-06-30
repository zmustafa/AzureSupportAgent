import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type CapabilityMatrix, type CapCell, type CapStatus } from "../api";
import { Skeleton } from "../utils/perf";

// ---------------------------------------------------------------- status styling
const STATUS_META: Record<CapStatus, { label: string; cell: string; dot: string; text: string }> = {
  full: { label: "Full", cell: "bg-green-50", dot: "bg-green-500", text: "text-green-700" },
  degraded: { label: "Degraded", cell: "bg-amber-50", dot: "bg-amber-500", text: "text-amber-700" },
  blind: { label: "Blind", cell: "bg-red-50", dot: "bg-red-500", text: "text-red-700" },
  disabled: { label: "Disabled", cell: "bg-gray-50", dot: "bg-gray-300", text: "text-gray-400" },
};

const AUTH_LABEL: Record<string, string> = {
  service_principal: "Service principal",
  service_principal_cert: "SP + certificate",
  default_chain: "Managed identity",
  az_cli_token: "Pasted token",
};

function scoreTone(score: number): string {
  if (score >= 100) return "text-green-700";
  if (score >= 60) return "text-amber-700";
  if (score >= 30) return "text-orange-700";
  return "text-red-700";
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

function CapabilityCellView({ cell, colLabel }: { cell: CapCell; colLabel: string }) {
  const meta = STATUS_META[cell.status] ?? STATUS_META.blind;
  const tip = cell.remediation ? `${cell.reason}\n\nFix: ${cell.remediation}` : cell.reason;
  return (
    <td className={`border-l px-2 py-2 align-top ${meta.cell}`} title={`${colLabel} — ${meta.label}\n\n${tip}`}>
      <div className="flex items-start gap-1.5">
        <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${meta.dot}`} />
        <div className="min-w-0">
          <div className={`text-[11px] font-semibold ${meta.text}`}>{meta.label}</div>
          <div className="line-clamp-3 text-[11px] leading-snug text-gray-600">{cell.reason}</div>
          {cell.remediation && (
            <div className="mt-0.5 line-clamp-2 text-[10px] italic leading-snug text-gray-400">
              Fix: {cell.remediation}
            </div>
          )}
        </div>
      </div>
    </td>
  );
}

export function CapabilityMatrixPanel() {
  const [live, setLive] = useState(false);
  const q = useQuery<CapabilityMatrix>({
    queryKey: ["capabilityMatrix", live],
    queryFn: () => api.capabilityMatrix(live),
    staleTime: live ? 0 : 60_000,
  });

  const data = q.data;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              <span>🔌</span> Connection Capability &amp; Blind Spots
            </h1>
            <p className="mt-0.5 max-w-3xl text-[13px] text-gray-500">
              What each Azure connection can and can&apos;t actually reach — ARM, Resource Graph,
              Microsoft Graph, Log Analytics, the Key Vault data plane and gated writes. A pasted
              token reads ARM fine but is <span className="font-medium text-red-700">blind</span> to
              the data planes, so an investigation can look complete while running half-blind.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-[12px] text-gray-600" title="Verify ARM and Microsoft Graph token acquisition for real (makes live Azure calls).">
              <input
                type="checkbox"
                checked={live}
                onChange={(e) => setLive(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-gray-300"
              />
              Verify live
            </label>
            <button
              onClick={() => q.refetch()}
              disabled={q.isFetching}
              className="rounded-lg border bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {q.isFetching ? "Probing…" : "Refresh"}
            </button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        {q.isLoading ? (
          <div className="space-y-3">
            <div className="flex gap-2">
              {[0, 1, 2].map((i) => <Skeleton key={i} className="h-14 w-40" />)}
            </div>
            <Skeleton className="h-64 w-full" />
          </div>
        ) : q.isError ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            Couldn&apos;t load the capability matrix. You may not have the{" "}
            <code className="rounded bg-red-100 px-1">connections.read</code> permission, or the
            backend is unreachable.
            <button onClick={() => q.refetch()} className="ml-2 underline">Retry</button>
          </div>
        ) : !data || data.connections.length === 0 ? (
          <div className="rounded-lg border bg-white p-8 text-center text-sm text-gray-500">
            No Azure connections are configured yet. Add a connection in Settings → Connections to
            see its capability matrix.
          </div>
        ) : (
          <div className="space-y-4">
            {/* Summary */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="Connections" value={data.summary.connections} />
              <Stat
                label="With blind spots"
                value={data.summary.with_blind_spots}
                tone={data.summary.with_blind_spots ? "text-red-700" : "text-green-700"}
              />
              <Stat label="Fully capable" value={data.summary.fully_capable} tone="text-green-700" />
              <Stat label="Mode" value={data.live ? "Live-verified" : "Inferred"} tone={data.live ? "text-blue-700" : "text-gray-700"} />
            </div>

            {/* Legend */}
            <div className="flex flex-wrap items-center gap-3 text-[11px] text-gray-500">
              {(["full", "degraded", "blind", "disabled"] as CapStatus[]).map((s) => (
                <span key={s} className="flex items-center gap-1.5">
                  <span className={`h-2 w-2 rounded-full ${STATUS_META[s].dot}`} />
                  {STATUS_META[s].label}
                </span>
              ))}
              <span className="text-gray-400">· hover any cell for the reason &amp; how to fix it</span>
            </div>

            {/* Matrix */}
            <div className="overflow-x-auto rounded-lg border bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-gray-50 text-left">
                    <th className="sticky left-0 z-10 bg-gray-50 px-3 py-2 font-medium text-gray-700">
                      Connection
                    </th>
                    {data.capabilities.map((c) => (
                      <th key={c.key} className="border-l px-2 py-2 font-medium text-gray-700" title={c.desc}>
                        {c.label}
                      </th>
                    ))}
                    <th className="border-l px-3 py-2 text-right font-medium text-gray-700">Score</th>
                  </tr>
                </thead>
                <tbody>
                  {data.connections.map((conn) => (
                    <tr key={conn.id} className="border-t align-top">
                      <td className="sticky left-0 z-10 max-w-[220px] bg-white px-3 py-2">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate font-medium text-gray-900">{conn.display_name}</span>
                          {conn.is_default && (
                            <span className="rounded bg-blue-100 px-1 text-[10px] font-medium text-blue-700">default</span>
                          )}
                          {conn.disabled && (
                            <span className="rounded bg-gray-100 px-1 text-[10px] font-medium text-gray-500">disabled</span>
                          )}
                        </div>
                        <div className="mt-0.5 text-[11px] text-gray-500">
                          {AUTH_LABEL[conn.auth_method] ?? conn.auth_method}
                          {conn.read_only && <span className="ml-1 text-gray-400">· read-only</span>}
                        </div>
                        {conn.blind_spots.length > 0 && (
                          <div className="mt-0.5 text-[11px] font-medium text-red-600">
                            {conn.blind_spots.length} blind spot{conn.blind_spots.length === 1 ? "" : "s"}
                          </div>
                        )}
                      </td>
                      {data.capabilities.map((c) => (
                        <CapabilityCellView
                          key={c.key}
                          cell={conn.caps[c.key] ?? { status: "blind", reason: "Unknown." }}
                          colLabel={c.label}
                        />
                      ))}
                      <td className="border-l px-3 py-2 text-right">
                        <span className={`text-lg font-semibold ${scoreTone(conn.score)}`}>{conn.score}</span>
                        <span className="text-[11px] text-gray-400">/100</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="text-[11px] text-gray-400">
              Generated {new Date(data.generated_at).toLocaleString()}
              {data.live ? " · ARM & Microsoft Graph reachability verified live" : " · inferred from auth method & token state (toggle “Verify live” to prove it)"}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
