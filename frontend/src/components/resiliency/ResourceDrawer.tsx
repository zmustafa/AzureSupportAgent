import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import type {
  ResiliencyMeta, ResiliencyResource, ResiliencyScope, ResiliencyVerdict,
} from "../../api";
import { PortalLink, bandText, cellState, rpoText } from "./ScenarioHeatmap";

/**
 * One resource, every scenario, with the evidence that produced each answer.
 *
 * A drawer rather than a route: it is opened from a cell and closed again in seconds, and a
 * route would push the heatmap out of the reader's head.
 *
 * Cross-linking is the point. This module derives everything from other modules, so every
 * claim must be one click from its source — a derived verdict the reader cannot trace is a
 * verdict they will not trust, and correctly so.
 */

const STATE_STYLE: Record<string, string> = {
  met: "text-emerald-700",
  breached: "text-amber-700",
  no_path: "text-rose-700 font-semibold",
  unknown: "text-gray-500",
  not_applicable: "text-gray-400",
};

/* Caveats are rendered apart from the basis, and never in the same list. The basis explains
   why the answer is what it is; a caveat explains when the answer stops being true. Merged,
   a warning reads as though it justifies the green cell — the exact inversion this module
   exists to prevent. */
const CAVEAT_STYLE: Record<string, string> = {
  critical: "border-rose-300 bg-rose-50 text-rose-800",
  warning: "border-amber-300 bg-amber-50 text-amber-800",
  info: "border-sky-200 bg-sky-50 text-sky-800",
};

function Caveats({ verdict }: { verdict: ResiliencyVerdict }) {
  const caveats = verdict.caveats ?? [];
  if (!caveats.length) return null;
  return (
    <div className="mt-1.5 space-y-1">
      {caveats.map((c, i) => (
        <div
          key={i}
          data-testid="verdict-caveat"
          data-severity={c.severity}
          className={`rounded border px-1.5 py-1 text-[10px] leading-snug ${CAVEAT_STYLE[c.severity] ?? CAVEAT_STYLE.info}`}
        >
          <span className="font-semibold uppercase tracking-wide">
            {c.kind === "mitigation" ? "Mitigation" : "Does not cover"}
          </span>
          <span className="ml-1">{c.detail}</span>
          {c.doc_url && (
            <a
              href={c.doc_url}
              target="_blank"
              rel="noreferrer noopener"
              className="ml-1 underline"
            >
              docs
            </a>
          )}
        </div>
      ))}
    </div>
  );
}

function Row({ verdict, label, classLabel }: {
  verdict: ResiliencyVerdict; label: string; classLabel: string;
}) {
  const state = cellState(verdict);
  if (!verdict.applicable) {
    return (
      <tr className="border-t align-top">
        <td className="py-1.5 pr-3 text-gray-500">{label}</td>
        <td colSpan={4} className="py-1.5 text-[11px] text-gray-400">
          {verdict.basis[0]?.detail ?? "Does not apply to this resource."}
        </td>
      </tr>
    );
  }
  return (
    <tr className="border-t align-top" data-testid="drawer-scenario" data-state={state}>
      <td className="py-1.5 pr-3 font-medium text-gray-700">{label}</td>
      <td className="py-1.5 pr-3 tabular-nums">{rpoText(verdict)}</td>
      <td className={`py-1.5 pr-3 ${STATE_STYLE[state]}`}>
        {classLabel}
        {bandText(verdict) && (
          <span className="ml-1 text-[11px] text-gray-500 tabular-nums">{bandText(verdict)}</span>
        )}
      </td>
      <td className="py-1.5 pr-3 text-[11px] text-gray-600">
        {verdict.basis.map((b, i) => <div key={i}>{b.detail}</div>)}
        {/* An estimate without its assumptions is a bare number, which is how a guess gets
            copied into a DR plan. */}
        {!!verdict.rto_assumptions?.length && (
          <details className="mt-1">
            <summary className="cursor-pointer text-[10px] text-gray-400">assumptions</summary>
            <ul className="ml-3 list-disc text-[10px] text-gray-500">
              {verdict.rto_assumptions.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          </details>
        )}
        <Caveats verdict={verdict} />
      </td>
      <td className="py-1.5 text-[11px] text-gray-500">{verdict.confidence}</td>
    </tr>
  );
}

export function ResourceDrawer({
  resource, meta, scope, portalHost, onClose,
}: {
  resource: ResiliencyResource;
  meta: ResiliencyMeta | undefined;
  scope: ResiliencyScope;
  portalHost?: string;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"scenarios" | "config">("scenarios");
  const classLabels = Object.fromEntries((meta?.rto_classes ?? []).map((c) => [c.id, c.label]));
  const scenarios = meta?.scenarios ?? [];

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}
         data-testid="resiliency-drawer">
      <div className="h-full w-full max-w-3xl overflow-auto bg-white p-4 shadow-xl"
           onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-1.5 text-base font-semibold text-gray-900">
              <span className="truncate" title={resource.name}>{resource.name}</span>
              <PortalLink resourceId={resource.id} portalHost={portalHost}
                          label={`Open ${resource.name} in the Azure portal`} />
            </h2>
            <div className="text-xs text-gray-500">
              {resource.type} · {resource.location}
              {resource.tier_label && ` · ${resource.tier_label}`}
            </div>
          </div>
          <button onClick={onClose} className="rounded px-2 py-1 text-sm text-gray-500 hover:bg-gray-100">
            Close
          </button>
        </div>

        <div className="mb-3 flex w-fit items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
          {(["scenarios", "config"] as const).map((t) => (
            <button key={t} type="button" aria-pressed={tab === t} onClick={() => setTab(t)}
                    className={`rounded-md px-2.5 py-1 ${tab === t ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}>
              {t === "scenarios" ? "Recovery" : "Configuration"}
            </button>
          ))}
        </div>

        {tab === "scenarios" ? (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500">
                <th className="pb-1 font-medium">Scenario</th>
                <th className="pb-1 font-medium">RPO</th>
                <th className="pb-1 font-medium">RTO</th>
                <th className="pb-1 font-medium">Why</th>
                <th className="pb-1 font-medium">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => {
                const verdict = resource.verdicts[s.id];
                if (!verdict) return null;
                return <Row key={s.id} verdict={verdict} label={s.label}
                            classLabel={classLabels[verdict.rto_class] ?? verdict.rto_class} />;
              })}
            </tbody>
          </table>
        ) : (
          <div className="space-y-3 text-xs">
            <Section title="Redundancy">
              <Fact label="Zone redundant" value={fmtTri(resource.redundancy.zone_redundant)} />
              <Fact label="Zones" value={resource.redundancy.zones.join(", ") || "—"} />
              <Fact label="Replication" value={resource.redundancy.replication || "—"} />
              <Fact label="SKU" value={resource.redundancy.sku || "—"} />
            </Section>
            <Section title="Protection">
              <Fact label="State" value={resource.protection.state} />
              {resource.protection.reason && (
                <div className="rounded border border-gray-200 bg-gray-50 p-2 text-[11px] text-gray-600">
                  {resource.protection.reason}
                </div>
              )}
              <Fact label="Backup frequency" value={resource.protection.frequency || "—"} />
              <Fact label="Retention (days)" value={resource.protection.retention_days ?? "—"} />
              <Fact label="Recovery point age (h)"
                    value={resource.protection.recovery_point_age_hours ?? "—"} />
              <Fact label="Vault redundancy" value={resource.protection.vault_redundancy || "—"} />
              <Fact label="Platform backup" value={resource.protection.native_backup.kind} />
            </Section>
            <Section title="Replication">
              <Fact label="Site Recovery" value={resource.dr.replicated ? "Yes" : "No"} />
              <Fact label="Measured RPO (s)" value={resource.dr.rpo_seconds ?? "—"} />
              <Fact label="Last drill (days)"
                    value={resource.dr.last_test_failover_age_days ?? "never"} />
            </Section>
            {!!resource.advisor.length && (
              <Section title={`Azure Advisor (${resource.advisor.length})`}>
                {resource.advisor.map((a, i) => (
                  <div key={i} className="text-[11px] text-gray-600">
                    {String((a as Record<string, unknown>).problem ?? "")}
                  </div>
                ))}
              </Section>
            )}
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2 border-t pt-3 text-[11px]">
          <a className="text-brand hover:underline" data-testid="drawer-link-backup"
             href={`/backup-manager${scope.workloadId ? `?workload_id=${scope.workloadId}` : ""}`}>
            Open in Backup Manager →
          </a>
          <span className="text-gray-400">
            Derived from configuration. Not proven by a recovery drill.
          </span>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded border p-2">
      <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-800">{value}</span>
    </div>
  );
}

function fmtTri(value: boolean | null): string {
  if (value === null || value === undefined) return "unknown";
  return value ? "Yes" : "No";
}

export function useResource(scope: ResiliencyScope, resourceId: string | null) {
  return useQuery({
    queryKey: ["resiliency", "resource", scope, resourceId],
    queryFn: () => api.resiliencyResource(scope, resourceId as string),
    enabled: !!resourceId,
  });
}
