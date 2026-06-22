import { SubscriptionScopePicker } from "./SubscriptionScopePicker";
import { AzureIcon } from "./AzureIcon";

export type ScopeKind = "workload" | "subscription";

/**
 * Unified "Workload | Subscription" scope selector shared by every scoped dashboard
 * (Monitoring / Telemetry / Backup-DR coverage, Performance Profiler, Retirement Radar,
 * Telemetry Intelligence). Replaces the per-view copies that rendered the toggle + a bare
 * `<select>` + the subscription picker inconsistently. Icons come from the canonical
 * `AzureIcon`, so subscriptions and workloads look the same everywhere.
 */
export function ScopePicker({
  scopeKind,
  onScopeKindChange,
  workloads,
  workloadId,
  onWorkloadChange,
  subId,
  subName,
  onSubPick,
  workloadOnly = false,
  workloadPlaceholder,
  connectionId,
}: {
  scopeKind: ScopeKind;
  onScopeKindChange: (k: ScopeKind) => void;
  workloads: { id: string; name: string }[];
  workloadId: string;
  onWorkloadChange: (id: string) => void;
  subId: string;
  subName: string;
  onSubPick: (id: string, name: string) => void;
  /** Hide the Subscription option (a couple of views are workload-only). */
  workloadOnly?: boolean;
  /** Leading empty option label (for views that must not auto-select a workload). */
  workloadPlaceholder?: string;
  /** Enumerate subscriptions from THIS connection/tenant (defaults to the default connection). */
  connectionId?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      {!workloadOnly && (
        <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
          <button
            onClick={() => onScopeKindChange("workload")}
            className={`flex items-center gap-1 rounded-md px-2.5 py-1 ${scopeKind === "workload" ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}
          >
            <AzureIcon kind="workload" className="h-3.5 w-3.5" />
            Workload
          </button>
          <button
            onClick={() => onScopeKindChange("subscription")}
            className={`flex items-center gap-1 rounded-md px-2.5 py-1 ${scopeKind === "subscription" ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}
          >
            <AzureIcon kind="subscription" className="h-3.5 w-3.5" />
            Subscription
          </button>
        </div>
      )}
      {scopeKind === "workload" || workloadOnly ? (
        <div className="relative">
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2">
            <AzureIcon kind="workload" className="h-3.5 w-3.5" />
          </span>
          <select
            value={workloadId}
            onChange={(e) => onWorkloadChange(e.target.value)}
            className="max-w-[240px] rounded-lg border py-1.5 pl-7 pr-2 text-xs"
          >
            {workloadPlaceholder !== undefined && <option value="">{workloadPlaceholder}</option>}
            {workloads.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <SubscriptionScopePicker value={subId} valueName={subName} onPick={onSubPick} connectionId={connectionId} />
      )}
    </div>
  );
}
