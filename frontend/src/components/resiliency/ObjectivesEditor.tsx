import { useEffect, useState } from "react";
import type { ResiliencyMeta, ResiliencyReference, ResiliencyRtoClass } from "../../api";

/**
 * The objectives and restore-rate editor.
 *
 * These two live on one screen because they are the same promise: **every number this
 * module prints must be traceable to a constant somebody can see and change.** A duration
 * band derived from an invisible throughput figure is not reviewable, and the first thing a
 * sceptical engineer asks is where it came from.
 *
 * Objectives are per tier PER SCENARIO. A single RTO across all failure modes is the same
 * mistake as a single RTO per resource: nobody demands fifteen-minute recovery from
 * ransomware, and nobody accepts a day of data loss from a zone blip.
 */

const RATE_LABEL: Record<string, string> = {
  vm_restore_mbps: "Virtual machine restore (MB/s)",
  disk_restore_mbps: "Managed disk restore (MB/s)",
  blob_restore_mbps: "Blob restore (MB/s)",
  sql_restore_gb_per_hour: "SQL restore (GB/hour)",
  generic_restore_mbps: "Everything else (MB/s)",
};

const MECHANISM_LABEL: Record<string, string> = {
  asr_failover: "Site Recovery failover",
  sql_failover_group: "SQL failover group",
  sql_geo_restore: "SQL geo-restore",
  cosmos_manual_failover: "Cosmos DB manual failover",
  vault_restore_overhead: "Vault restore, fixed overhead",
  native_pitr_overhead: "Platform point-in-time restore, fixed overhead",
  detect_and_decide: "Detect the failure and decide to act",
};

function minutesToText(minutes: number): string {
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

export function ObjectivesEditor({
  reference, meta, canEdit, onSave, saving, rejected,
}: {
  reference: ResiliencyReference;
  meta: ResiliencyMeta | undefined;
  canEdit: boolean;
  onSave: (body: Partial<ResiliencyReference>) => Promise<void>;
  saving: boolean;
  rejected: string[];
}) {
  const [draft, setDraft] = useState(reference);
  const [dirty, setDirty] = useState(false);

  // A save bumps the version; re-seed from the server so the editor never drifts from what
  // the analysis will actually use.
  useEffect(() => { setDraft(reference); setDirty(false); }, [reference.version]);

  const scenarios = meta?.scenarios ?? [];
  const classes = (meta?.rto_classes ?? []).filter((c) => c.id !== "unknown");

  const setTarget = (tierId: string, scenario: string, patch: Record<string, unknown>) => {
    setDirty(true);
    setDraft((prev) => ({
      ...prev,
      tiers: prev.tiers.map((t) => t.id !== tierId ? t : {
        ...t,
        scenarios: { ...t.scenarios, [scenario]: { ...t.scenarios[scenario], ...patch } },
      }),
    }));
  };

  const setRate = (key: string, value: number) => {
    setDirty(true);
    setDraft((prev) => ({ ...prev, restore_rates: { ...prev.restore_rates, [key]: value } }));
  };

  const setMechanism = (key: string, value: number) => {
    setDirty(true);
    setDraft((prev) => ({ ...prev, mechanism_minutes: { ...prev.mechanism_minutes, [key]: value } }));
  };

  const save = () => onSave({
    tiers: draft.tiers,
    restore_rates: draft.restore_rates,
    mechanism_minutes: draft.mechanism_minutes,
  });

  return (
    <div className="space-y-3" data-testid="resiliency-objectives-editor">
      {!canEdit && (
        <div className="rounded border border-gray-200 bg-gray-50 p-2 text-[11px] text-gray-600">
          You can see every constant behind these numbers, but changing them needs
          <code className="mx-1">resiliency.admin</code>.
        </div>
      )}

      {!!rejected.length && (
        <div className="rounded border border-rose-200 bg-rose-50 p-2 text-[11px] text-rose-800"
             data-testid="resiliency-rejected">
          <div className="font-semibold">Some values were refused</div>
          <ul className="ml-4 list-disc">{rejected.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}

      {/* ---------------------------------------------------------------- objectives */}
      <div className="rounded-xl border bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900">Recovery objectives</h2>
        <p className="mb-2 text-[11px] text-gray-500">
          Per criticality tier, per failure scenario. A resource inherits its tier from its
          workload's criticality.
        </p>
        {draft.tiers.map((tier) => (
          <div key={tier.id} className="mb-3" data-testid="objective-tier">
            <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">
              {tier.label}
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500">
                  <th className="py-1 font-medium">Scenario</th>
                  <th className="py-1 font-medium">Target RTO</th>
                  <th className="py-1 font-medium">Target RPO (minutes)</th>
                  <th className="py-1 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((s) => {
                  const target = tier.scenarios[s.id];
                  if (!target) return null;
                  return (
                    <tr key={s.id} className="border-t">
                      <td className="py-1 pr-2">{s.label}</td>
                      <td className="py-1 pr-2">
                        <select
                          value={target.rto_class}
                          disabled={!canEdit}
                          data-testid="objective-rto"
                          onChange={(e) => setTarget(tier.id, s.id,
                            { rto_class: e.target.value as ResiliencyRtoClass })}
                          className="rounded border px-1 py-0.5 text-xs disabled:bg-gray-50"
                        >
                          {classes.map((c) => (
                            <option key={c.id} value={c.id}>{c.label}</option>
                          ))}
                        </select>
                      </td>
                      <td className="py-1 pr-2">
                        <input
                          type="number" min={0} value={target.rpo_minutes}
                          disabled={!canEdit}
                          data-testid="objective-rpo"
                          onChange={(e) => setTarget(tier.id, s.id,
                            { rpo_minutes: Number(e.target.value) })}
                          className="w-24 rounded border px-1 py-0.5 text-xs tabular-nums disabled:bg-gray-50"
                        />
                      </td>
                      <td className="py-1 text-[11px] text-gray-400">
                        {minutesToText(target.rpo_minutes)}
                        {!s.redundancy_helps && " · redundancy does not help here"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      {/* -------------------------------------------------------------- restore rates */}
      <div className="rounded-xl border bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900">Restore rates and mechanism times</h2>
        <p className="mb-2 text-[11px] text-gray-500">
          These constants produce every duration band. They are <strong>starting points, not
          truths</strong> — if you have measured your own restore throughput, set it here.
          Each band names the rate that produced it.
        </p>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">Throughput</div>
            {Object.entries(draft.restore_rates).map(([key, value]) => (
              <label key={key} className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="text-gray-600">{RATE_LABEL[key] ?? key}</span>
                <input
                  type="number" min={1} value={value} disabled={!canEdit}
                  data-testid="restore-rate"
                  onChange={(e) => setRate(key, Number(e.target.value))}
                  className="w-24 rounded border px-1 py-0.5 text-xs tabular-nums disabled:bg-gray-50"
                />
              </label>
            ))}
          </div>
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">
              Fixed overheads (minutes)
            </div>
            {Object.entries(draft.mechanism_minutes).map(([key, value]) => (
              <label key={key} className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="text-gray-600">{MECHANISM_LABEL[key] ?? key}</span>
                <input
                  type="number" min={0} value={value} disabled={!canEdit}
                  data-testid="mechanism-minutes"
                  onChange={(e) => setMechanism(key, Number(e.target.value))}
                  className="w-24 rounded border px-1 py-0.5 text-xs tabular-nums disabled:bg-gray-50"
                />
              </label>
            ))}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => void save()}
          disabled={!canEdit || !dirty || saving}
          data-testid="resiliency-save-objectives"
          className="rounded bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save objectives and rates"}
        </button>
        {dirty && (
          <span className="text-[11px] text-amber-700">
            Unsaved. Re-analyze after saving for the new values to appear in verdicts.
          </span>
        )}
        <span className="ml-auto text-[11px] text-gray-400">
          version {reference.version} · updated {String(reference.updated_at).slice(0, 16).replace("T", " ")}
          {reference.updated_by && ` by ${reference.updated_by}`}
        </span>
      </div>
    </div>
  );
}
