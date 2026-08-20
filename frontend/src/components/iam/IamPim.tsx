/** PIM tab — standing privilege versus JIT.
 *
 *  This is the number the screen exists to produce. "Privileged" alone cannot tell an Owner
 *  someone holds permanently from one they must request and that expires, and only the second
 *  is what buying PIM actually bought.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamRow } from "../../api";
import { useDebounced } from "../../utils/perf";
import { InvestigateLink, investigatableId } from "../entra/InvestigateLink";
import { IAM_PAGE, KpiTile, useIamConnectionId } from "./IamShared";

/** The principal a PIM row is about: the EFFECTIVE holder where a group was expanded, so the
 *  jump lands on the person who actually elevates rather than the group they came through. */
function principalCell(r: IamRow): { name: string; id: string | null } {
  const name = String(r.effectivePrincipalName || r.principalDisplayName || r.effectivePrincipalId || "");
  const id = String(r.effectivePrincipalId || r.principalId || "");
  return { name, id: investigatableId(undefined, id) };
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}

/** "in 3h", "in 2d", or "expired" for a JIT window. */
function untilText(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return "expired";
  const h = ms / 3_600_000;
  if (h < 1) return `in ${Math.round(h * 60)}m`;
  if (h < 48) return `in ${Math.round(h)}h`;
  return `in ${Math.round(h / 24)}d`;
}

function YesNo({ on, label }: { on: boolean; label: string }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${on ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}
      title={on ? `${label} is required to activate` : `${label} is NOT required to activate`}
    >
      {on ? "✓" : "✗"} {label}
    </span>
  );
}

export function PimTab() {
  const connectionId = useIamConnectionId();
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 250);

  const ovQ = useQuery({
    queryKey: ["iam", "overview", connectionId ?? ""],
    queryFn: () => api.iamOverview(connectionId),
    staleTime: 5 * 60 * 1000,
  });
  // Server-side lenses, NOT a client-side filter of a page. Filtering the first 200 rows of a
  // 5,506-grant estate rendered "Eligible assignments (3)" beside a KPI reading 137 — the list
  // silently excluded 154 of the tenant's eligible assignments while presenting itself as the
  // complete set.
  const eligQ = useQuery({
    queryKey: ["iam", "access", "eligible", dSearch, connectionId ?? ""],
    queryFn: () => api.iamAccess({ tab: "eligible", search: dSearch, limit: IAM_PAGE, connection_id: connectionId }),
    staleTime: 60 * 1000,
  });
  const elevQ = useQuery({
    queryKey: ["iam", "access", "elevated", dSearch, connectionId ?? ""],
    queryFn: () => api.iamAccess({ tab: "elevated", search: dSearch, limit: IAM_PAGE, connection_id: connectionId }),
    staleTime: 60 * 1000,
  });

  const eligible = useMemo(() => (eligQ.data?.rows ?? []) as IamRow[], [eligQ.data]);
  const elevated = useMemo(() => (elevQ.data?.rows ?? []) as IamRow[], [elevQ.data]);
  const eligibleTotal = eligQ.data?.total ?? eligible.length;
  const elevatedTotal = elevQ.data?.total ?? elevated.length;
  const k = ovQ.data?.kpis;
  // The KPI counts eligible AND privileged; the grid lists every eligible assignment. Held
  // separately so the grid can say which it is instead of leaving two different numbers under
  // near-identical labels on the same screen.
  const privilegedEligible = k?.eligible_privileged ?? null;
  const ratio = k?.standing_ratio;
  // Nothing has ever been collected for this connection: every figure below would be an
  // artefact of not having looked. The Overview tab shows the same wall (IamView).
  const neverLoaded = !!ovQ.data?.never_loaded;

  if (neverLoaded) {
    return (
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <b>No access scan has been loaded for this connection.</b> Standing privilege, JIT
          eligibility and active elevations are therefore unknown — not zero. Run an access scan
          from the Overview tab, or check Diagnostics if the collectors could not read.
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <KpiTile label="Standing privileged" value={k?.standing_privileged} tone="red" />
        <KpiTile label="Eligible privileged (JIT)" value={k?.eligible_privileged} tone="sky" />
        <KpiTile label="Elevated right now" value={k?.active_elevations} tone="amber" />
        <KpiTile label="Total privileged" value={k?.privileged} />
        <div className="rounded-lg border bg-white px-3 py-2">
          <div className={`text-xl font-semibold ${ratio == null ? "text-gray-400" : ratio > 0.5 ? "text-red-600" : "text-green-600"}`}>
            {pct(ratio)}
          </div>
          <div className="text-[11px] uppercase tracking-wide text-gray-500">Standing ratio</div>
        </div>
      </div>

      {/* Three different situations that must NOT render alike: eligibility was never
          collected, there is nothing to measure, and the posture is genuinely bad. */}
      {k && !k.pim_collected ? (
        <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <b>PIM eligibility was not collected for the cached scopes.</b> Every privileged grant
          below therefore looks permanent, but that is an artefact of not having looked — not a
          finding. Run a refresh from the Overview tab, and check Diagnostics if the PIM
          collectors report <i>Unauthorized</i>.
        </div>
      ) : ratio == null ? (
        <div className="mb-4 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-600">
          No privileged access found in the cached scan, so there is no standing-vs-JIT ratio to
          report. This is not a clean bill of health — run a scan, or check Diagnostics for
          collectors that could not read.
        </div>
      ) : ratio > 0.5 ? (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          <b>{pct(ratio)} of privileged access is permanent.</b> Standing privilege is held all the
          time, whether or not it is being used. Converting these to eligible (JIT) assignments is
          the single highest-value change available on this screen.
        </div>
      ) : null}

      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search principal / role / scope…"
        className="mb-3 w-72 rounded border px-2 py-1 text-sm"
      />

      {elevated.length > 0 && (
        <div className="mb-4 rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">
            Elevated right now ({elevatedTotal.toLocaleString()})
            {elevated.length < elevatedTotal && (
              <span className="ml-2 text-[11px] font-normal text-amber-800">
                showing the first {elevated.length} — search to narrow
              </span>
            )}
          </div>
          <table className="w-full text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Principal</th>
                <th className="px-3 py-2">Role</th>
                <th className="px-3 py-2">Scope</th>
                <th className="px-3 py-2">Expires</th>
              </tr>
            </thead>
            <tbody>
              {elevated.map((r, i) => {
                const p = principalCell(r);
                return (
                  <tr key={i} className="border-t">
                    <td className="px-3 py-1.5 font-medium text-gray-800">
                      <div className="flex items-center gap-1">
                        <span className="min-w-0 truncate">{p.name}</span>
                        {p.id && <InvestigateLink principalId={p.id} label={p.name} />}
                      </div>
                    </td>
                    <td className="px-3 py-1.5">{String(r.roleName)}</td>
                    <td className="px-3 py-1.5 text-gray-600">{String(r.scopeDisplayName || r.scope)}</td>
                    <td className="px-3 py-1.5">
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                        {untilText(String(r.activationExpiresOn))}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">
          {/* The KPI above counts eligible AND PRIVILEGED; this grid lists EVERY eligible
              assignment. On a real tenant that is 137 against 174 — two numbers a reader
              would take for the same measure, sitting a few inches apart under near-identical
              labels. Both are correct; only the labeling was wrong, so the grid states its own
              scope rather than hiding the 37 non-privileged rows to make the numbers agree. */}
          All eligible assignments ({eligibleTotal.toLocaleString()})
          {privilegedEligible !== null && privilegedEligible !== eligibleTotal && (
            <span className="ml-2 text-[11px] font-normal text-gray-600">
              including {privilegedEligible.toLocaleString()} privileged — the KPI above counts only those
            </span>
          )}
          {eligible.length < eligibleTotal && (
            <span className="ml-2 text-[11px] font-normal text-amber-800">
              showing the first {eligible.length} — search to narrow
            </span>
          )}
        </div>
        {eligQ.isLoading ? (
          <div className="p-4 text-sm text-gray-500">Loading…</div>
        ) : eligible.length === 0 ? (
          <div className="p-4 text-sm text-gray-500">
            {k && !k.pim_collected
              ? "Eligibility has not been collected yet, so this list is empty by construction — not because there is no JIT access."
              : "No eligible (JIT) assignments in the cached scan. Either PIM is not in use here, or the connection could not read the PIM schedules — check Diagnostics before concluding the former."}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Principal</th>
                <th className="px-3 py-2">Role</th>
                <th className="px-3 py-2">Scope</th>
                <th className="px-3 py-2">Eligibility</th>
                <th className="px-3 py-2">Activation requires</th>
              </tr>
            </thead>
            <tbody>
              {eligible.map((r, i) => {
                const permanent = !!r.isPermanentEligible;
                const weak = !r.requiresApproval && !r.requiresMfa;
                const p = principalCell(r);
                return (
                  <tr key={i} className="border-t">
                    <td className="px-3 py-1.5 font-medium text-gray-800">
                      <div className="flex items-center gap-1">
                        <span className="min-w-0 truncate">{p.name}</span>
                        {p.id && <InvestigateLink principalId={p.id} label={p.name} />}
                      </div>
                    </td>
                    <td className="px-3 py-1.5">{String(r.roleName)}</td>
                    <td className="px-3 py-1.5 text-gray-600">{String(r.scopeDisplayName || r.scope)}</td>
                    <td className="px-3 py-1.5">
                      {permanent ? (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800" title="Eligible with no end date">
                          permanent
                        </span>
                      ) : (
                        <span className="text-[11px] text-gray-500">until {String(r.eligibilityEndDateTime).slice(0, 10) || "—"}</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5">
                      <div className="flex flex-wrap items-center gap-1">
                        <YesNo on={!!r.requiresApproval} label="approval" />
                        <YesNo on={!!r.requiresMfa} label="MFA" />
                        {r.activationMaxHours ? (
                          <span className="text-[11px] text-gray-500">max {String(r.activationMaxHours)}h</span>
                        ) : null}
                        {weak && permanent && (
                          <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700" title="Permanently eligible with no approval and no MFA — JIT in name only">
                            JIT in name only
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
