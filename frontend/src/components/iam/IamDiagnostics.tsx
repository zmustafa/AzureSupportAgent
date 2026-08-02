/** Diagnostics tab — per-collector status and the error/warning rows.
 *
 *  This is where "we could not look" is distinguished from "there is nothing there": a
 *  collector reporting Unauthorized/Throttled must be visible, not silently read as zero.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { StatusPill, useIamConnectionId } from "./IamShared";

export function DiagnosticsTab() {
  const connectionId = useIamConnectionId();
  const q = useQuery({ queryKey: ["iam", "diagnostics", connectionId ?? ""], queryFn: () => api.iamDiagnostics(connectionId), staleTime: 5 * 60 * 1000 });
  const ovQ = useQuery({ queryKey: ["iam", "overview", connectionId ?? ""], queryFn: () => api.iamOverview(connectionId), staleTime: 5 * 60 * 1000 });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  const collectors = q.data?.collectors ?? [];
  const errors = q.data?.errors ?? [];
  const denies = ovQ.data?.kpis?.deny_assignments ?? 0;
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      {denies > 0 && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          <b>{denies} deny assignment{denies === 1 ? "" : "s"} in force.</b> Deny assignments are
          evaluated before role assignments and cannot be overridden — not even by Owner — so some
          of the grants shown elsewhere in this screen are blocked in practice. Filter the access
          grid by the <i>Deny Assignment</i> surface to see them.
        </div>
      )}
      <div className="mb-4 rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Collector status ({collectors.length})</div>
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Collector</th>
              <th className="px-3 py-2">Scope</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Rows</th>
              <th className="px-3 py-2">Message</th>
            </tr>
          </thead>
          <tbody>
            {collectors.map((c, i) => (
              <tr key={i} className="border-t">
                <td className="px-3 py-1.5 font-medium text-gray-800">{c.collector}</td>
                <td className="px-3 py-1.5 text-gray-500">{c.scopeLabel}</td>
                <td className="px-3 py-1.5"><StatusPill status={c.status} /></td>
                <td className="px-3 py-1.5 text-gray-600">{c.rowsAdded}</td>
                <td className="px-3 py-1.5 text-[11px] text-gray-500">{c.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {errors.length > 0 && (
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Errors & warnings ({errors.length})</div>
          <table className="w-full text-sm">
            <tbody>
              {errors.map((e, i) => (
                <tr key={i} className="border-t">
                  <td className="px-3 py-1.5 text-gray-700">{e.collector}</td>
                  <td className="px-3 py-1.5"><StatusPill status={e.status} /></td>
                  <td className="px-3 py-1.5 text-[11px] text-gray-500">{e.errorMessage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
