/** Bypass tab — *"If I revoked every role assignment, who could still get in?"*
 *
 * The whole point of this screen is one sentence: **RBAC is the only door for N% of M assessed
 * resources.** Three rules follow from that and each one is a defect if broken:
 *
 *  - the percentage is NEVER rendered without its denominator, and is rendered as "not assessed"
 *    rather than 0% when nothing was measured. "0% RBAC-only" and "we looked at nothing" are
 *    opposite claims that a bare ratio cannot tell apart;
 *  - a family that could not be read shows its status, not an empty clean list. Blind ≠ zero;
 *  - remediation is never shown without the `breaksIf` warning that qualifies it. "Set
 *    allowSharedKeyAccess to false" without "this breaks every SAS-token client" is how a
 *    read-only tool causes an outage.
 *
 * `reachabilityAvailable` gets the same treatment at row level: an empty `reachableBy` when the
 * join could not run means "we do not know who holds this key", not "nobody does".
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamBypassRow } from "../../api";
import { InvestigateLink, investigatableId } from "../entra/InvestigateLink";
import { useIamConnectionId, StatusPill } from "./IamShared";

const SEV_CLASS: Record<string, string> = {
  critical: "bg-red-100 text-red-800",
  error: "bg-orange-100 text-orange-800",
  warning: "bg-amber-100 text-amber-800",
  info: "bg-sky-100 text-sky-800",
};

// The backend's collector vocabulary is Succeeded / SucceededWithWarnings / PartiallyCollected /
// Skipped / Unauthorized / Throttled / Failed. Only the last three mean "we could not read this";
// listing the healthy ones instead was a bug that red-flagged every family that worked.
const UNREADABLE_STATUSES = new Set(["Unauthorized", "Throttled", "Failed"]);

function Row({ r }: { r: IamBypassRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border bg-white">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline gap-2 px-3 py-2 text-left hover:bg-gray-50"
      >
        <span className={`rounded px-1.5 text-[10px] font-semibold uppercase ${SEV_CLASS[r.severity] ?? "bg-gray-100 text-gray-700"}`}>
          {r.severity}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800" title={r.resourceId}>
          {r.resourceName}
        </span>
        <span className="shrink-0 text-[11px] text-gray-500">{r.title}</span>
        {r.environment && (
          <span className="shrink-0 rounded bg-gray-100 px-1 text-[10px] text-gray-600">{r.environment}</span>
        )}
      </button>
      {open && (
        <div className="space-y-2 border-t px-3 py-2">
          <div className="text-[11px] text-gray-600">{r.detail}</div>
          <div className="text-[11px] text-gray-500">
            {r.resourceType} · {r.resourceGroup}
          </div>

          {/* Blind ≠ nobody. An empty list with the join unavailable must not read as "nobody
              holds this credential". */}
          {r.credentialAction && (
            <div className="rounded bg-gray-50 p-2">
              <div className="text-[11px] font-semibold text-gray-700">Who can fetch the credential</div>
              {!r.reachabilityAvailable ? (
                <div className="text-[11px] text-amber-800">
                  Not determined — role assignments for this scope were not available, so this is
                  unknown, not empty.
                </div>
              ) : r.reachableCount === 0 ? (
                <div className="text-[11px] text-gray-600">
                  No principal holds <code>{r.credentialAction}</code> at a scope covering this resource.
                </div>
              ) : (
                <>
                  <ul className="mt-1 space-y-0.5">
                    {r.reachableBy.map((h) => {
                      // Real tenants contain distinct principals with identical display names
                      // (the live estate has two "Zeeshan Mustafa" objects). Rendering the name
                      // alone produced two identical lines that read as a duplicate-row bug, so
                      // the id is shown only where it is actually needed to tell them apart.
                      const ambiguous =
                        r.reachableBy.filter((o) => o.principalName === h.principalName).length > 1;
                      return (
                        <li key={`${h.principalId}-${h.scope}`}
                            className="flex items-center gap-1 text-[11px] text-gray-700">
                          {/* The affordance sits against the NAME, not at the panel's right
                              edge. These rows span the full width of a wide panel, so a
                              right-aligned glyph ends up inches from the identity it acts on
                              and reads as belonging to the row's last column instead. */}
                          <span className="max-w-[22rem] shrink-0 truncate">
                            {h.principalName}
                            {ambiguous && (
                              <span className="text-gray-400"> ({h.principalId.slice(0, 8)})</span>
                            )}
                          </span>
                          {investigatableId(undefined, h.principalId) && (
                            <InvestigateLink principalId={h.principalId} label={h.principalName} />
                          )}
                          <span className="min-w-0 flex-1 truncate text-gray-400">at {h.scope}</span>
                        </li>
                      );
                    })}
                  </ul>
                  {r.reachableCount > r.reachableBy.length && (
                    <div className="text-[11px] text-gray-500">
                      +{r.reachableCount - r.reachableBy.length} more
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Remediation and its blast radius are one unit. Never render one without the other. */}
          <div className="rounded border border-emerald-200 bg-emerald-50 p-2">
            <div className="text-[11px] font-semibold text-emerald-900">Remediation</div>
            <div className="text-[11px] text-emerald-900">{r.remediation}</div>
            <div className="mt-1 border-t border-emerald-200 pt-1 text-[11px] font-medium text-red-800">
              Breaks if: {r.breaksIf}
            </div>
          </div>

          {r.frameworks.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {r.frameworks.map((f) => (
                <span key={f} className="rounded bg-gray-100 px-1 text-[10px] text-gray-600">{f}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function BypassTab() {
  const connectionId = useIamConnectionId();
  const [family, setFamily] = useState("");
  const [severity, setSeverity] = useState("");

  const q = useQuery({
    queryKey: ["iam", "bypass", connectionId, family, severity],
    queryFn: () => api.iamBypass({ family, severity, connection_id: connectionId }),
  });
  const d = q.data;
  const s = d?.summary;

  const families = useMemo(
    () => (s?.by_family ?? []).slice().sort((a, b) => b.affected - a.affected || a.family.localeCompare(b.family)),
    [s],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        {/* The headline. Denominator is mandatory, and "nothing assessed" is not 0%. */}
        <div className="flex items-baseline gap-3">
          {s && s.rbac_only_pct === null ? (
            <span data-testid="bypass-headline" className="text-sm font-semibold text-amber-800">
              No resources assessed — RBAC-only coverage is unknown, not 100%.
            </span>
          ) : (
            <span data-testid="bypass-headline" className="text-sm text-gray-800">
              <b className="text-lg text-gray-900">{s?.rbac_only_pct ?? "—"}%</b> of{" "}
              <b>{s?.assessed ?? 0}</b> assessed resources have RBAC as the only door
              <span className="text-gray-500">
                {" "}· {s?.bypassed ?? 0} have another way in
              </span>
            </span>
          )}
          {d?.status && <StatusPill status={d.status} />}
        </div>

        {/* Never hidden, never collapsed. */}
        {(s?.limitations?.length ?? 0) > 0 && (
          <div data-testid="bypass-limitations" className="mt-2 rounded border border-amber-300 bg-amber-50 p-2">
            <div className="mb-1 text-[11px] font-semibold text-amber-900">What this does not cover</div>
            <ul className="space-y-1">
              {s?.limitations.map((l) => (
                <li key={l} className="text-[11px] text-amber-900">{l}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="w-64 shrink-0 overflow-auto border-r bg-gray-50 p-2">
          <div className="mb-1 px-1 text-[11px] font-semibold uppercase text-gray-500">Services</div>
          <button
            type="button"
            onClick={() => setFamily("")}
            className={`w-full rounded px-2 py-1 text-left text-xs ${family === "" ? "bg-sky-100 text-sky-900" : "hover:bg-gray-100"}`}
          >
            All services
          </button>
          {families.map((f) => (
            <button
              key={f.family}
              type="button"
              onClick={() => setFamily(f.family)}
              title={f.message || undefined}
              className={`flex w-full items-baseline gap-1 rounded px-2 py-1 text-left text-xs ${family === f.family ? "bg-sky-100 text-sky-900" : "hover:bg-gray-100"}`}
            >
              <span className="min-w-0 flex-1 truncate">{f.family}</span>
              {/* A family that could not be read says so instead of showing a clean zero. */}
              {UNREADABLE_STATUSES.has(f.status) ? (
                <span className="rounded bg-red-100 px-1 text-[10px] text-red-800">{f.status}</span>
              ) : (
                <span className="text-[10px] text-gray-500">
                  {f.affected}/{f.assessed}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-3">
          <div className="mb-2 flex items-center gap-2">
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              aria-label="Severity"
              className="rounded border border-gray-300 px-1.5 py-0.5 text-xs"
            >
              <option value="">All severities</option>
              <option value="critical">Critical</option>
              <option value="error">Error</option>
              <option value="warning">Warning</option>
              <option value="info">Info</option>
            </select>
            <span className="text-[11px] text-gray-500">{d?.rows.length ?? 0} shown</span>
          </div>

          {q.isLoading && <div className="text-sm text-gray-500">Loading…</div>}
          {d?.never_loaded && (
            <div className="rounded border bg-white p-3 text-xs text-gray-600">
              The bypass sweep has not run for this tenant yet. Nothing here is an all-clear.
            </div>
          )}
          {d && !d.never_loaded && d.rows.length === 0 && (
            <div className="rounded border bg-white p-3 text-xs text-gray-600">
              No non-RBAC door was found in what was assessed. Check the coverage above and the
              service statuses on the left before reading that as an all-clear.
            </div>
          )}
          <div className="space-y-1.5">
            {d?.rows.map((r) => (
              <Row key={`${r.resourceId}-${r.key}`} r={r} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
