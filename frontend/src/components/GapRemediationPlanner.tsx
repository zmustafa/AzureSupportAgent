import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type AlertAnalysisGap,
  type AlertsManagerCapabilities,
  type DeploymentPlan,
  type GapDeploymentPlanStatus,
  type DeploymentPlanItem,
  type DeploymentPlanValidation,
  type ManagedActionGroup,
  type ActivityLogDiagnosticDestinationOptions,
} from "../api";
import { formatError } from "../utils/format";
import { queryKeys } from "../queryKeys";

type ScopeParams = { connection_id?: string; workload_id?: string; subscription_id?: string; management_group_id?: string };
type Props = {
  gaps: AlertAnalysisGap[];
  scopeParams: ScopeParams;
  liveActionGroups: ManagedActionGroup[];
  capabilities?: AlertsManagerCapabilities;
  onClose: () => void;
  onSubmitted: (plan: DeploymentPlan) => void;
  onOpenPlan: (planId: string) => void;
};

const inputClass = "mt-1 w-full rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-xs outline-none focus:border-indigo-400";
const secondaryButton = "rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40";
const primaryButton = "rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-40";
const PLACEMENT_STORAGE_KEY = "azsup.alertsManager.remediationPlacement";

function gapId(gap: AlertAnalysisGap): string {
  const target = gap.rule_id || gap.resource_id || gap.action_group_id || gap.resource_name || "unknown-target";
  const signal = gap.alert_key || gap.signal || gap.rule_name || "unknown-signal";
  return [gap.decision_key || gap.type, target, signal].map((value) => String(value).trim().toLowerCase()).join("|");
}

function StatusPill({ value }: { value: string }) {
  const tone = value === "covered" ? "bg-emerald-50 text-emerald-700" : value === "blocked" ? "bg-rose-50 text-rose-700" : value === "missing" || value === "drifted" ? "bg-amber-50 text-amber-700" : "bg-sky-50 text-sky-700";
  return <span className={`rounded px-2 py-0.5 text-[10px] font-medium ${tone}`}>{value}</span>;
}

function itemTitle(item: DeploymentPlanItem): string {
  return item.alert_name || item.alert_key || "Unnamed metric alert";
}

export function GapRemediationPlanner({ gaps, scopeParams, liveActionGroups, capabilities, onClose, onSubmitted, onOpenPlan }: Props) {
  const queryClient = useQueryClient();
  const [selectedGaps, setSelectedGaps] = useState(gaps);
  const [commonActionGroupId, setCommonActionGroupId] = useState("");
  const rememberedPlacement = useMemo(() => {
    try { return JSON.parse(localStorage.getItem(PLACEMENT_STORAGE_KEY) || "{}") as { management_group_id?: string; subscription_id?: string; resource_group?: string }; }
    catch { return {}; }
  }, []);
  const [placementManagementGroup, setPlacementManagementGroup] = useState(scopeParams.management_group_id || rememberedPlacement.management_group_id || "");
  const [placementSubscription, setPlacementSubscription] = useState(scopeParams.subscription_id || rememberedPlacement.subscription_id || "");
  const [monitoringResourceGroup, setMonitoringResourceGroup] = useState(rememberedPlacement.resource_group || "");
  const [plan, setPlan] = useState<DeploymentPlan | null>(null);
  const [validation, setValidation] = useState<DeploymentPlanValidation | null>(null);
  const [removedItemIds, setRemovedItemIds] = useState<Set<string>>(new Set());
  const [blockingPlans, setBlockingPlans] = useState<Record<string, GapDeploymentPlanStatus>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const canSubmit = !!capabilities?.can_submit_deployment_plans && !capabilities.read_only;
  const placementQ = useQuery<ActivityLogDiagnosticDestinationOptions>({
    queryKey: ["alerts-manager-remediation-placement", scopeParams.connection_id, placementManagementGroup, placementSubscription],
    queryFn: () => api.activityLogDiagnosticDestinationOptions({
      connection_id: scopeParams.connection_id,
      management_group_id: placementManagementGroup || undefined,
      subscription_id: placementSubscription || undefined,
      kind: "workspace",
    }),
    staleTime: 5 * 60_000,
  });
  const placement = placementQ.data;

  useEffect(() => {
    if (placementSubscription && placement?.subscriptions.some((item) => item.id.toLowerCase() === placementSubscription.toLowerCase())) return;
    if (placementSubscription && placement && !placementQ.isFetching) {
      setPlacementSubscription("");
      setMonitoringResourceGroup("");
    }
  }, [placement, placementQ.isFetching, placementSubscription]);

  useEffect(() => {
    if (!monitoringResourceGroup || !placementSubscription) return;
    localStorage.setItem(PLACEMENT_STORAGE_KEY, JSON.stringify({
      management_group_id: placementManagementGroup,
      subscription_id: placementSubscription,
      resource_group: monitoringResourceGroup,
    }));
  }, [monitoringResourceGroup, placementManagementGroup, placementSubscription]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose]);

  const visibleItems = useMemo(() => (plan?.items ?? []).filter((item) => !removedItemIds.has(item.id)), [plan, removedItemIds]);
  const includedCount = (plan?.items ?? []).filter((item) => item.included && !removedItemIds.has(item.id)).length;
  const validationByItem = useMemo(() => new Map((validation?.errors ?? []).map((item) => [item.item_id, item.errors])), [validation]);

  function removeGap(id: string) {
    setSelectedGaps((current) => current.filter((gap) => gapId(gap) !== id));
  }

  function setIncluded(itemId: string, included: boolean) {
    if (!plan?.items) return;
    setPlan({ ...plan, items: plan.items.map((item) => item.id === itemId ? { ...item, included } : item) });
    setValidation(null);
  }

  function removePlanItem(itemId: string) {
    setIncluded(itemId, false);
    setRemovedItemIds((current) => new Set(current).add(itemId));
  }

  async function preview() {
    if (!selectedGaps.length || !canSubmit) return;
    setBusy("preview"); setError(""); setValidation(null);
    try {
      const result = await api.previewGapsDeploymentPlan({
        ...scopeParams,
        monitoring_resource_group: monitoringResourceGroup.trim(),
        common_action_group_id: commonActionGroupId,
        gaps: selectedGaps.map((gap) => ({ ...gap, decision_key: gapId(gap) })),
      });
      setPlan(result.plan);
      setRemovedItemIds(new Set());
      const blockedGapIds = result.plan.items?.filter((item) => item.classification === "blocked" && item.source_gap_id).map((item) => item.source_gap_id!) ?? [];
      setBlockingPlans(blockedGapIds.length ? (await api.deploymentPlansByGap(blockedGapIds)).by_gap : {});
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setBusy("");
    }
  }

  async function cancelBlockingPlan(blocker: GapDeploymentPlanStatus) {
    if (!capabilities?.can_approve || !["pending", "approved"].includes(blocker.status)) return;
    if (!window.confirm(`Cancel the ${blocker.status} deployment plan blocking this gap? Its pending or approved managed changes will be rejected; applied changes are retained for audit.`)) return;
    setBusy(`cancel:${blocker.plan_id}`); setError("");
    try {
      await api.decideDeploymentPlan(blocker.plan_id, "rejected", "Cancelled from blocked gap remediation preview.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.alertsManager.changesRoot }),
        queryClient.invalidateQueries({ queryKey: queryKeys.alertsManager.summaryRoot }),
      ]);
      await preview();
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setBusy("");
    }
  }

  async function persistItems(): Promise<DeploymentPlan> {
    if (!plan?.items) throw new Error("Preview a deployment plan first.");
    const result = await api.updateDeploymentPlanItems(plan.id, plan.items.map((item) => ({
      item_id: item.id,
      included: item.included && !removedItemIds.has(item.id),
    })));
    setPlan(result.plan);
    return result.plan;
  }

  async function validate() {
    if (!plan) return;
    setBusy("validate"); setError("");
    try {
      const saved = await persistItems();
      const result = await api.validateDeploymentPlan(saved.id);
      setValidation(result);
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setBusy("");
    }
  }

  async function submit() {
    if (!plan) return;
    setBusy("submit"); setError("");
    try {
      const saved = await persistItems();
      const checked = await api.validateDeploymentPlan(saved.id);
      setValidation(checked);
      if (!checked.valid) return;
      const result = await api.submitDeploymentPlan(saved.id);
      onSubmitted(result.plan);
    } catch (cause) {
      setError(formatError(cause));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" role="dialog" aria-modal="true" aria-label="Create selected-gap remediation plan">
      <div className="flex h-full w-full max-w-5xl flex-col bg-white shadow-2xl">
        <div className="flex items-start gap-3 border-b px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-gray-900">Create remediation plan</h2>
            <p className="mt-0.5 text-xs text-gray-500">Create or update enabled Azure Monitor alert rules and attach them to live Action Groups. Findings are never added directly to an Action Group.</p>
          </div>
          <button onClick={onClose} disabled={!!busy} aria-label="Close remediation planner" className="ml-auto text-gray-400 hover:text-gray-700 disabled:opacity-40">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-auto p-5">
          {error && <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">{error}</div>}
          <section className="rounded-xl border">
            <div className="flex items-center border-b bg-gray-50 px-4 py-3">
              <div><h3 className="text-sm font-semibold text-gray-800">Selected findings</h3><p className="text-xs text-gray-500">{selectedGaps.length} finding{selectedGaps.length === 1 ? "" : "s"} from the current analysis snapshot.</p></div>
            </div>
            <div className="max-h-44 divide-y overflow-auto">
              {selectedGaps.map((gap) => <div key={gapId(gap)} className="flex items-center gap-3 px-4 py-2.5 text-xs"><span className="rounded bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">{gap.risk}</span><div className="min-w-0 flex-1"><div className="truncate font-medium text-gray-800">{gap.signal || gap.rule_name || "Metric baseline"}</div><div className="truncate text-[10px] text-gray-400">{gap.resource_name || gap.resource_id} · {gap.type.replaceAll("_", " ")}</div></div>{!plan && <button onClick={() => removeGap(gapId(gap))} className="text-[11px] text-red-600 hover:underline">Remove</button>}</div>)}
              {!selectedGaps.length && <div className="p-6 text-center text-xs text-gray-400">All findings were removed. Close this panel and select findings again.</div>}
            </div>
          </section>

          {!plan ? <section className="rounded-xl border p-4">
            <h3 className="text-sm font-semibold text-gray-800">Action Group and placement</h3>
            <p className="mt-1 text-xs text-gray-500">Select one healthy live Action Group. Every generated alert rule in this plan will attach directly to it.</p>
            <div className="mt-3 grid gap-3 md:grid-cols-3">
              <label className="text-xs">Management group<select aria-label="Remediation management group" value={placementManagementGroup} disabled={placementQ.isFetching} onChange={(event) => { setPlacementManagementGroup(event.target.value); setPlacementSubscription(""); setMonitoringResourceGroup(""); }} className={inputClass}><option value="">All visible subscriptions</option>{placement?.management_groups.map((item) => <option key={item.id} value={item.id}>{`${"  ".repeat(item.depth ?? 0)}${item.name} — ${item.id}`}</option>)}</select></label>
              <label className="text-xs">Subscription <span className="text-red-600">*</span><select aria-label="Remediation subscription" value={placementSubscription} disabled={placementQ.isFetching} onChange={(event) => { setPlacementSubscription(event.target.value); setMonitoringResourceGroup(""); }} className={inputClass}><option value="">{placementQ.isFetching && !placementSubscription ? "Loading subscriptions…" : "Select subscription…"}</option>{placement?.subscriptions.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.id}</option>)}</select></label>
              <label className="text-xs">Monitoring resource group <span className="text-red-600">*</span><select aria-label="Monitoring resource group" value={monitoringResourceGroup} disabled={!placementSubscription || placementQ.isFetching} onChange={(event) => setMonitoringResourceGroup(event.target.value)} className={inputClass}><option value="">{placementQ.isFetching && placementSubscription ? "Loading resource groups…" : "Select resource group…"}</option>{placement?.resource_groups.map((item) => <option key={item.id} value={item.name}>{item.name} — {item.id}</option>)}</select></label>
            </div>
            {placementQ.isError && <div className="mt-2 text-xs text-red-600">Could not load Azure placement options: {formatError(placementQ.error)}</div>}
            <div className="mt-4"><div className="text-xs font-medium text-gray-700">Live Action Group <span className="text-red-600">*</span></div><div className="mt-2 grid gap-2 md:grid-cols-2">{liveActionGroups.map((group) => { const healthy = group.enabled && group.active_receiver_count > 0; return <label key={group.id} className={`flex items-center gap-3 rounded-lg border p-3 text-xs ${commonActionGroupId === group.id ? "border-indigo-300 bg-indigo-50" : healthy ? "" : "border-amber-200 bg-amber-50 opacity-70"}`}><input type="radio" name="common-action-group" disabled={!healthy} checked={commonActionGroupId === group.id} onChange={() => setCommonActionGroupId(group.id)} /><span className="min-w-0 flex-1"><span className="flex items-center gap-2"><span className="truncate font-medium text-gray-800">{group.name}</span><span className={`rounded px-1.5 py-0.5 text-[9px] ${healthy ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>{healthy ? "healthy" : group.enabled ? "no active receivers" : "disabled"}</span></span><span className="mt-0.5 block text-[10px] text-gray-500">{group.active_receiver_count} active / {group.receiver_count} receivers</span></span></label>; })}</div>{!liveActionGroups.length && <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-700">No live Action Groups are available in this scope.</div>}</div>
          </section> : <section className="overflow-hidden rounded-xl border" data-testid="gaps-plan-items-table">
            <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50 px-4 py-3"><div><h3 className="text-sm font-semibold text-gray-800">Server-generated preview</h3><p className="text-xs text-gray-500">Review classifications, block reasons, direct Action Group destinations, and enabled metric rule proposals.</p></div><div className="ml-auto flex flex-wrap gap-1">{(["covered", "equivalent", "drifted", "missing", "blocked"] as const).map((kind) => <span key={kind} className="rounded border bg-white px-2 py-1 text-[10px] text-gray-600">{kind} {plan.counts[kind] ?? 0}</span>)}</div></div>
            {removedItemIds.size > 0 && <div className="flex items-center border-b bg-amber-50 px-4 py-2 text-xs text-amber-800"><span>{removedItemIds.size} item{removedItemIds.size === 1 ? "" : "s"} removed from this submission.</span><button onClick={() => setRemovedItemIds(new Set())} className="ml-auto font-medium hover:underline">Restore all</button></div>}
            <div className="overflow-auto"><table className="w-full min-w-[1050px] text-left text-xs"><thead className="bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Include</th><th className="px-3 py-2">Classification</th><th className="px-3 py-2">Resource / alert</th><th className="px-3 py-2">Action Group</th><th className="px-3 py-2">Reason / proposal</th><th className="px-3 py-2" /></tr></thead><tbody className="divide-y">{visibleItems.map((item) => <tr key={item.id} className="align-top"><td className="px-3 py-3"><input aria-label={`Include ${itemTitle(item)}`} type="checkbox" checked={item.included} disabled={!item.actionable} onChange={(event) => setIncluded(item.id, event.target.checked)} /></td><td className="px-3 py-3"><StatusPill value={item.classification} /></td><td className="max-w-xs px-3 py-3"><div className="font-medium text-gray-800">{item.resource_name || "Unnamed resource"}</div><div className="text-[10px] text-gray-500">{itemTitle(item)} · severity {item.severity}</div><div className="truncate text-[10px] text-gray-400" title={item.resource_id}>{item.resource_type}</div></td><td className="max-w-sm px-3 py-3"><div className="font-medium text-gray-700">{item.routing?.action_groups.map((group) => group.name).join(", ") || "No destination"}</div><div className="mt-1 text-[10px] text-gray-500">{item.routing?.explanation || "No Action Group destination was assigned."}</div>{item.routing?.diagnostics.map((message) => <div key={message} className="mt-1 text-[10px] text-amber-700">{message}</div>)}</td><td className="max-w-sm px-3 py-3">{item.reasons.length > 0 ? <div className="space-y-1 text-rose-700">{item.reasons.map((reason) => <div key={reason}>{reason}</div>)}</div> : item.proposal ? <div><div className="font-medium text-gray-700">Create {item.proposal.desired.enabled === true ? "enabled" : "disabled"} metric alert rule</div><div className="mt-1 break-all text-[10px] text-gray-400">{item.proposal.target_id}</div></div> : <span className="text-gray-400">No change proposed.</span>}{validationByItem.get(item.id)?.map((message) => <div key={message} className="mt-1 text-[10px] text-red-700">{message}</div>)}</td><td className="px-3 py-3"><button onClick={() => removePlanItem(item.id)} className="text-[11px] text-red-600 hover:underline">Remove</button></td></tr>)}</tbody></table>{!visibleItems.length && <div className="p-8 text-center text-xs text-gray-400">No preview items remain.</div>}</div>
            {Object.values(blockingPlans).some((blocker) => blocker.plan_id && (blocker.status === "pending" || blocker.status === "approved")) && <div className="border-t border-rose-200 bg-rose-50 px-4 py-3"><div className="text-xs font-semibold text-rose-800">Blocking deployment plans</div><div className="mt-2 flex flex-wrap gap-2">{[...new Map(Object.values(blockingPlans).filter((blocker) => blocker.plan_id && (blocker.status === "pending" || blocker.status === "approved")).map((blocker) => [blocker.plan_id, blocker])).values()].map((blocker) => <div key={blocker.plan_id} className="flex items-center gap-2 rounded-lg border border-rose-200 bg-white p-2 text-xs"><span className="text-rose-700">{blocker.status} plan {blocker.plan_id.slice(0, 8)}</span><button onClick={() => onOpenPlan(blocker.plan_id)} className={secondaryButton}>Open</button>{capabilities?.can_approve && <button disabled={!!busy} onClick={() => void cancelBlockingPlan(blocker)} className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-40">Cancel blocker</button>}</div>)}</div></div>}
          </section>}

          {validation && <div className={`rounded-lg border p-3 text-xs ${validation.valid ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}>{validation.valid ? `${validation.included_count} alert-rule change${validation.included_count === 1 ? "" : "s"} validated and ready for approval submission.` : `Validation found ${validation.errors.length} item${validation.errors.length === 1 ? "" : "s"} with errors.`}</div>}
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t bg-white px-5 py-3">
          {plan && <span className="text-xs text-gray-500">{includedCount} included · generated rules become enabled when approved and applied</span>}
          <div className="ml-auto flex gap-2"><button onClick={onClose} disabled={!!busy} className={secondaryButton}>Cancel</button>{!plan ? <button data-action="preview-gaps-plan" disabled={!!busy || !canSubmit || !selectedGaps.length || !commonActionGroupId || !placementSubscription || !monitoringResourceGroup || placementQ.isFetching} onClick={() => void preview()} className={primaryButton}>{busy === "preview" ? "Previewing…" : "Preview plan"}</button> : <><button disabled={!!busy} onClick={() => void preview()} className={secondaryButton}>{busy === "preview" ? "Rechecking…" : "Recheck preview"}</button><button disabled={!!busy || !includedCount} onClick={() => void validate()} className={secondaryButton}>{busy === "validate" ? "Validating…" : "Validate"}</button><button data-action="submit-gaps-plan" disabled={!!busy || !includedCount} onClick={() => void submit()} className={primaryButton}>{busy === "submit" ? "Submitting…" : "Submit to approval"}</button></>}</div>
        </div>
      </div>
    </div>
  );
}
