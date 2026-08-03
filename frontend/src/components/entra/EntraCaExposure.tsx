import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type EntraCaExposureRow } from "../../api";
import { formatError } from "../../utils/format";
import { EntraEmpty } from "./EntraShared";

/**
 * Exposure: the coverage matrix collapsed into an order of work.
 *
 * The matrix is the right shape for auditing and the wrong shape for deciding what to do. Ten
 * classes across fourteen controls is 140 cells, and 140 cells do not sort themselves — the
 * reader has to hold the whole grid in their head to work out which gap matters most. This
 * view collapses the control axis, attaches the findings that actually fired, and orders by
 * severity so the first row is the one worth reading.
 *
 * The ordering is severity-first on purpose. A ratio of covered controls would rank a class
 * with thirteen minor controls satisfied and one critical one missing above a class with a
 * merely mediocre spread, which is precisely backwards.
 */

const SEVERITY_STYLE: Record<string, { chip: string; label: string }> = {
  critical: { chip: "bg-red-100 text-red-800 border-red-200", label: "Critical" },
  high: { chip: "bg-orange-100 text-orange-800 border-orange-200", label: "High" },
  medium: { chip: "bg-amber-100 text-amber-800 border-amber-200", label: "Medium" },
  low: { chip: "bg-sky-100 text-sky-800 border-sky-200", label: "Low" },
  info: { chip: "bg-gray-100 text-gray-600 border-gray-200", label: "No findings" },
};

export function EntraCaExposureView({
  connectionId,
  onOpenSetup,
}: {
  connectionId: string | null;
  onOpenSetup: () => void;
}) {
  const q = useQuery({
    queryKey: ["entra-ca-exposure", connectionId],
    queryFn: () => api.entraCaExposure(connectionId),
  });
  const [open, setOpen] = useState<string | null>(null);

  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading exposure…</div>;
  if (q.isError) return <div className="p-6 text-sm text-red-600">{formatError(q.error)}</div>;
  const data = q.data!;
  if (!data.meta.loaded) return <EntraEmpty kind="cold" />;
  const caDomain = data.meta.domains?.ca;
  if (caDomain && (caDomain.status === "blind" || caDomain.status === "unlicensed")) {
    return (
      <EntraEmpty
        kind={caDomain.status === "blind" ? "blind" : "unlicensed"}
        detail={caDomain.error || `Missing ${(caDomain.missing_permissions ?? []).join(", ")}`}
        onOpenSetup={onOpenSetup}
      />
    );
  }

  const withFindings = data.rows.filter((r) => r.finding_count > 0);

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border bg-white p-4">
        <div>
          {withFindings.length === 0 ? (
            <div className="text-lg font-semibold text-emerald-700">
              No application class has an open exposure finding.
            </div>
          ) : (
            <div className="text-lg font-semibold text-gray-900">
              {withFindings.length} of {data.rows.length} application classes have exposure findings.
            </div>
          )}
          <div className="mt-1 text-xs text-gray-500">
            Cohort: {data.cohort} · {data.app_index?.app_count?.toLocaleString() ?? 0} applications
            classified · taxonomy {data.taxonomy_version}
          </div>
        </div>
        <a
          href={api.entraCaExposureExportUrl(connectionId)}
          className="rounded border px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50"
        >
          Export CSV
        </a>
      </div>

      <div className="overflow-hidden rounded-lg border bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b bg-gray-50/50 text-left text-xs text-gray-500">
              <th className="px-3 py-1.5 font-medium">Application class</th>
              <th className="px-2 py-1.5 font-medium">Worst finding</th>
              <th className="px-2 py-1.5 text-right font-medium">Findings</th>
              <th className="px-2 py-1.5 text-right font-medium">Controls covered</th>
              <th className="px-2 py-1.5 font-medium" />
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <ExposureRow
                key={row.class_id}
                row={row}
                open={open === row.class_id}
                onToggle={() => setOpen(open === row.class_id ? null : row.class_id)}
              />
            ))}
          </tbody>
        </table>
      </div>

      {!data.unattributed?.measured && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900">
          <span className="font-medium">Unattributed applications not measured. </span>
          {data.unattributed?.reason ||
            "Sign-in activity was not collected, so applications being used without policy coverage cannot be listed."}
        </div>
      )}
    </div>
  );
}

function ExposureRow({
  row,
  open,
  onToggle,
}: {
  row: EntraCaExposureRow;
  open: boolean;
  onToggle: () => void;
}) {
  const sev = SEVERITY_STYLE[row.worst_severity] ?? SEVERITY_STYLE.info;
  return (
    <>
      <tr className="border-b last:border-b-0">
        <td className="px-3 py-2">
          <div className="font-medium text-gray-900">{row.label}</div>
          {row.description && <div className="text-xs text-gray-500">{row.description}</div>}
        </td>
        <td className="px-2 py-2">
          <span className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-medium ${sev.chip}`}>
            {sev.label}
          </span>
        </td>
        <td className="px-2 py-2 text-right text-gray-700">{row.finding_count || "—"}</td>
        <td className="px-2 py-2 text-right text-gray-700">
          {row.controls_covered}/{row.controls_total}
          {row.controls_partial > 0 && (
            <span className="ml-1 text-xs text-amber-700">(+{row.controls_partial} partial)</span>
          )}
        </td>
        <td className="px-2 py-2 text-right">
          {row.finding_count > 0 && (
            <button
              onClick={onToggle}
              aria-expanded={open}
              className="rounded border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50"
            >
              {open ? "Hide" : "What this means"}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-b bg-gray-50/60 last:border-b-0">
          <td colSpan={5} className="px-3 py-3">
            <div className="space-y-3">
              {row.findings.map((f) => (
                <div key={f.signal_id} className="rounded border bg-white p-3">
                  <div className="text-[13px] font-semibold text-gray-900">{f.title}</div>
                  <div className="mt-1 text-[13px] text-gray-700">{f.detail}</div>
                  {f.impact ? (
                    <dl className="mt-2 space-y-1 text-[13px]">
                      <div>
                        <dt className="inline font-medium text-gray-600">Impact: </dt>
                        <dd className="inline text-gray-800">{f.impact.impact}</dd>
                      </div>
                      <div>
                        <dt className="inline font-medium text-gray-600">Blast radius: </dt>
                        <dd className="inline text-gray-800">{f.impact.blast_radius}</dd>
                      </div>
                      <div>
                        <dt className="inline font-medium text-gray-600">First step: </dt>
                        <dd className="inline text-gray-800">{f.impact.first_step}</dd>
                      </div>
                    </dl>
                  ) : (
                    /* Say so rather than showing nothing. A blank space where the impact
                       should be reads as "no impact", which is the opposite of the truth. */
                    <div className="mt-2 text-xs italic text-gray-500">
                      A class-specific impact statement has not been written for this
                      combination. The description above applies generally.
                    </div>
                  )}
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
