/** Least Privilege tab — granted versus used.
 *
 * This screen makes a claim about somebody's access that could get them locked out of
 * production, so four things are structural rather than cosmetic:
 *
 *  - **`measured: false` is a wall, not an empty list.** A tenant that has never run a usage scan
 *    must not see "0 over-privileged principals", which is the most reassuring possible rendering
 *    of "we have not looked";
 *  - **usage carries its own age.** The access snapshot can be minutes old while this is weeks
 *    old, and a stale denominator makes "unused" mean something different;
 *  - **both numbers, never the ratio alone.** "998 of 8213 unused" is a fact; "99.8%
 *    over-privileged" is a number designed to be quoted out of context;
 *  - **a proposal is never shown without its residual risk.** "Covers everything you did last
 *    quarter" and "safe" are different claims.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IamRightsizeRecommendation } from "../../api";
import { InvestigateLink, investigatableId } from "../entra/InvestigateLink";
import { useIamConnectionId } from "./IamShared";

/** The Azure Activity Log retains 90 days; `usage.MAX_WINDOW_DAYS` clamps anything longer. */
const MAX_WINDOW_DAYS = 90;
const WINDOW_PRESETS = [7, 14, 30, 60, 90];

/**
 * Usage-window picker — the popover pattern used by the Change Explorer's time range, but
 * bounded to what this collector can actually deliver.
 *
 * It deliberately does NOT offer absolute start/end, sub-day, or multi-month ranges:
 * `POST /iam/refresh-usage` takes a LOOKBACK IN DAYS ending at "now" (1..90, capped by Activity
 * Log retention). A "Last 15 minutes" or "Between 1 and 3 June" option would have to collapse to
 * a day count ending today, so the control would name one window while the scan read another —
 * and this screen decides whether someone's access gets removed. The header beside it already
 * had that bug once, where the selector read "90 days" next to data measured over 30.
 */
function UsageWindowPicker({
  days,
  onChange,
  disabled,
}: {
  days: number;
  onChange: (days: number) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState(String(days));
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  function apply(n: number) {
    const clamped = Math.max(1, Math.min(MAX_WINDOW_DAYS, Math.round(n) || 1));
    onChange(clamped);
    setCustom(String(clamped));
    setOpen(false);
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        aria-label="Usage window"
        title="The window the NEXT usage scan will read. The window the figures below were measured over is stated on the left."
        className="flex items-center gap-1.5 rounded border bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
      >
        <span>🕒</span>
        <span>Last {days} days</span>
        <span className="text-gray-400">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-72 rounded-xl border bg-white p-3 shadow-xl">
          <div className="mb-2 text-[11px] text-gray-400">
            Looking back from now · max {MAX_WINDOW_DAYS} days
          </div>
          <div className="grid grid-cols-2 gap-1">
            {WINDOW_PRESETS.map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => apply(n)}
                className={`rounded px-2 py-1.5 text-left text-sm ${
                  n === days ? "bg-brand/10 font-medium text-brand" : "text-gray-700 hover:bg-brand/5 hover:text-brand"
                }`}
              >
                Last {n} days
              </button>
            ))}
          </div>
          <div className="mt-3 border-t pt-3">
            <div className="text-xs text-gray-500">Custom</div>
            <div className="mt-1 flex items-center gap-2">
              <span className="text-sm text-gray-600">Last</span>
              <input
                type="number"
                min={1}
                max={MAX_WINDOW_DAYS}
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && apply(Number(custom))}
                aria-label="Custom usage window in days"
                className="w-20 rounded border px-2 py-1 text-sm"
              />
              <span className="text-sm text-gray-600">days</span>
              <button
                type="button"
                onClick={() => apply(Number(custom))}
                className="ml-auto rounded bg-brand px-2 py-1 text-xs font-medium text-white hover:bg-brand-dark"
              >
                Apply
              </button>
            </div>
          </div>
          {/* Naming the ceiling is the point: silently clamping a 180-day request to 90 would
              report a shorter window than the reader asked for, and "unused in 90 days" is a
              weaker claim than "unused in 180". */}
          <div className="mt-2 text-[11px] text-gray-400">
            The Azure Activity Log only retains {MAX_WINDOW_DAYS} days, so nothing older can be measured.
          </div>
        </div>
      )}
    </div>
  );
}

const CONFIDENCE_CLASS: Record<string, string> = {
  high: "bg-red-100 text-red-800",
  medium: "bg-amber-100 text-amber-900",
  low: "bg-gray-100 text-gray-600",
};

function Row({ r }: { r: IamRightsizeRecommendation }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border bg-white">
      {/* The disclosure control and the investigate jump are SIBLINGS, not nested. A button
          inside a button is invalid HTML, and the browser resolves it by swallowing the
          inner click — the link would render and simply never fire. */}
      <div className="flex items-baseline gap-1 pr-2 hover:bg-gray-50">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-baseline gap-2 px-3 py-2 text-left"
        >
          <span className={`shrink-0 rounded px-1.5 text-[10px] font-semibold uppercase ${CONFIDENCE_CLASS[r.confidence] ?? "bg-gray-100"}`}>
            {r.confidence}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800">{r.principalName || r.principalId}</span>
          <span className="shrink-0 text-[11px] text-gray-600">{r.currentRoles.join(", ")}</span>
          {/* The denominator travels with the ratio, everywhere it appears. */}
          <span data-testid="rightsize-ratio" className="shrink-0 text-[11px] text-gray-700">
            used <b>{r.usedActionCount}</b> of <b>{r.grantedActionCount}</b>
          </span>
        </button>
        {investigatableId(undefined, r.principalId) && (
          <InvestigateLink principalId={r.principalId}
                           label={r.principalName || r.principalId} />
        )}
      </div>
      {open && (
        <div className="space-y-2 border-t px-3 py-2">
          <div className="text-[11px] text-gray-600">
            {r.scopeName || r.scope} · window {r.window.days} days
          </div>
          <div className="text-[11px] text-amber-800">{r.confidenceWhy}</div>

          {r.usedActions.length > 0 && (
            <div>
              <div className="text-[11px] font-semibold text-gray-700">What they actually did</div>
              <ul className="mt-0.5 space-y-0.5">
                {r.usedActions.map((a) => (
                  <li key={a} className="truncate text-[11px] text-gray-600">{a}</li>
                ))}
              </ul>
            </div>
          )}

          {r.recommendation ? (
            <div className="rounded border border-emerald-200 bg-emerald-50 p-2">
              <div className="text-[11px] font-semibold text-emerald-900">Narrower proposal</div>
              <div className="text-[11px] text-emerald-900">
                {r.recommendation.roles.join(" + ")} at {r.recommendation.scope}
              </div>
              {/* Never rendered without this. */}
              <div data-testid="residual-risk" className="mt-1 border-t border-emerald-200 pt-1 text-[11px] font-medium text-red-800">
                Gives up: {r.recommendation.residualRisk}
              </div>
            </div>
          ) : (
            <div className="rounded border bg-gray-50 p-2 text-[11px] text-gray-700">{r.note}</div>
          )}
        </div>
      )}
    </div>
  );
}

export function LeastPrivilegeTab() {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [days, setDays] = useState<number | null>(null);

  const usage = useQuery({
    queryKey: ["iam", "usage", connectionId],
    queryFn: () => api.iamUsage(connectionId),
  });
  const rs = useQuery({
    queryKey: ["iam", "rightsizing", connectionId],
    queryFn: () => api.iamRightsizing(connectionId),
  });
  const scan = useMutation({
    mutationFn: () => api.iamRefreshUsage(scanDays, connectionId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["iam", "usage"] });
      qc.invalidateQueries({ queryKey: ["iam", "rightsizing"] });
    },
  });

  const u = usage.data;
  const d = rs.data;
  // The selector must open on the window the data on screen was ACTUALLY measured over. It
  // defaulted to 90 while the collected window was 30, so the control sat next to "over 30 days"
  // reading "90 days" — the reader takes the number beside the data as the window of the data.
  const scanDays = days ?? (u?.measured ? u.window_days : 90);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <span className="text-sm font-semibold text-gray-800">Granted vs used</span>
          {u?.measured && (
            <span className="text-[11px] text-gray-600">
              {u.event_count} operation(s) by {u.principal_count} principal(s) over {u.window_days} days
              {/* Usage has its OWN freshness. Implying the access snapshot's age would be wrong. */}
              {u.generated_at && <> · usage collected {new Date(u.generated_at).toLocaleString()}</>}
            </span>
          )}
          <div className="ml-auto flex items-center gap-1">
            <UsageWindowPicker days={scanDays} onChange={setDays} disabled={scan.isPending} />
            <button
              type="button"
              onClick={() => scan.mutate()}
              disabled={scan.isPending}
              className="rounded border bg-white px-2 py-1 text-xs text-gray-700 shadow-sm hover:bg-gray-50 disabled:opacity-50"
              title="Reads the Activity Log per subscription. Slow, and separate from the access refresh."
            >
              {scan.isPending ? "Scanning…" : "Scan usage"}
            </button>
          </div>
        </div>

        {d?.measured && (
          <div className="mt-1 text-[11px] text-gray-600">
            {d.recommendations.length} of {d.assessed} assessed assignment(s) are over-privileged,
            measured against the {d.action_universe_size} distinct actions this tenant's roles can grant.
            {(d.unresolved_roles ?? 0) > 0 && (
              <span className="text-amber-800"> · {d.unresolved_roles} assignment(s) could not be assessed — their role's actions were never collected</span>
            )}
            {(d.break_glass_excluded ?? 0) > 0 && (
              <span className="text-amber-800"> · {d.break_glass_excluded} break-glass account(s) reported but never recommended for removal</span>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {(usage.isLoading || rs.isLoading) && <div className="text-sm text-gray-500">Loading…</div>}

        {/* Not an empty list. A tenant that has never scanned must not read "nothing is
            over-privileged". The two ways of knowing nothing are different failures and get
            different instructions: usage was never collected, or usage WAS collected but no
            role could be resolved to its actions so there was nothing to compare it against. */}
        {d && !d.measured && (d.unresolved_roles ?? 0) > 0 && (
          <div data-testid="ciem-no-role-catalogue" className="rounded border border-red-300 bg-red-50 p-3">
            <div className="text-xs font-semibold text-red-900">
              Nothing could be assessed — the role catalogue is missing
            </div>
            <p className="mt-1 text-[11px] text-red-900">
              Usage was collected, but none of the {(d.unresolved_roles ?? 0).toLocaleString()} assignment(s)
              could be compared against it because the actions their roles grant were never
              collected for this tenant. This is <b>not</b> a clean result. Run a full access
              refresh from the Overview tab to re-collect the role definitions, then re-run the
              usage scan.
            </p>
          </div>
        )}

        {d && !d.measured && (d.unresolved_roles ?? 0) === 0 && (
          <div data-testid="usage-unmeasured" className="rounded border border-amber-300 bg-amber-50 p-3">
            <div className="text-xs font-semibold text-amber-900">Usage has not been collected</div>
            <p className="mt-1 text-[11px] text-amber-900">
              Nothing on this screen is a claim about what is unused. Run a usage scan to compare
              granted access against exercised access — until then, an empty list here means
              “we have not looked”, not “nothing is over-privileged”.
            </p>
          </div>
        )}

        {d?.measured && (
          <>
            {(d.excluded.length > 0 || d.limitations.length > 0) && (
              <div data-testid="ciem-limitations" className="mb-2 rounded border border-amber-300 bg-amber-50 p-2">
                <div className="mb-1 text-[11px] font-semibold text-amber-900">What this cannot see</div>
                <ul className="space-y-1">
                  {[...new Set([...d.excluded, ...d.limitations])].map((l) => (
                    <li key={l} className="text-[11px] text-amber-900">{l}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="space-y-1.5">
              {d.recommendations.map((r) => (
                <Row key={r.id} r={r} />
              ))}
            </div>
            {d.recommendations.length === 0 && (
              <div className="rounded border bg-white p-3 text-xs text-gray-600">
                Nothing crossed the over-privilege threshold in this window. Read the limitations
                above before taking that as a clean result.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
