import { useEffect, useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import {
  api,
  downloadBlob,
  type ActivityLogCategory,
  type ActivityLogCondition,
  type ActivityLogCoverage,
  type ActivityLogCoverageStatus,
  type ActivityLogPlanOperation,
  type ActivityLogPlanPreview,
  type ActivityLogPlanRequest,
  type ActivityLogPlanValidation,
  type AlertsManagerCapabilities,
  type ManagedActionGroup,
} from "../api";
import type { AlertsManagerScopeParams } from "../queryKeys";
import { formatError } from "../utils/format";

const CATEGORIES: Array<{ id: ActivityLogCategory; label: string; description: string }> = [
  { id: "ServiceHealth", label: "Service Health", description: "Azure service incidents, maintenance, advisories, and security advisories affecting subscriptions." },
  { id: "ResourceHealth", label: "Resource Health", description: "Resource availability transitions such as unavailable, degraded, and unknown." },
  { id: "Security", label: "Security", description: "Security-category Activity Log events routed through an Action Group or SIEM-capable destination." },
  { id: "Recommendation", label: "Recommendation", description: "Azure recommendations emitted through the subscription Activity Log." },
];
const CATEGORY_LABEL = Object.fromEntries(CATEGORIES.map((item) => [item.id, item.label])) as Record<ActivityLogCategory, string>;
const SERVICE_INCIDENT_TYPES = ["Incident", "Maintenance", "Security", "ActionRequired"] as const;
const RESOURCE_HEALTH_STATUSES = ["Available", "Degraded", "Unavailable", "Unknown"] as const;
const PAGE_SIZES = [25, 50, 100] as const;
const STEPS = ["Categories", "Subscriptions", "Conditions & naming", "Routing", "Review"] as const;
const buttonSecondary = "rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40";
const buttonPrimary = "rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-40";

function statusTone(status: string) {
  if (status === "covered") return "bg-emerald-50 text-emerald-700";
  if (status === "partial" || status === "disabled") return "bg-amber-50 text-amber-700";
  if (status === "missing" || status === "blocked") return "bg-rose-50 text-rose-700";
  return "bg-gray-100 text-gray-600";
}

function operationTone(operation: ActivityLogPlanOperation["classification"]) {
  if (operation === "equivalent") return "bg-emerald-50 text-emerald-700";
  if (operation === "blocked" || operation === "invalid") return "bg-rose-50 text-rose-700";
  if (operation === "create") return "bg-sky-50 text-sky-700";
  return "bg-amber-50 text-amber-700";
}

function destinationSummary(group: ManagedActionGroup) {
  const destinations = group.receivers.filter((receiver) => receiver.enabled).map((receiver) => receiver.destination || receiver.masked).filter(Boolean);
  return destinations.join(" · ") || "No enabled destinations";
}

function commaList(value: string) {
  return [...new Set(value.split(",").map((item) => item.replace(/[\u0000-\u001f\u007f]/g, "").trim()).filter(Boolean))].slice(0, 100);
}

function siemRoute(group: ManagedActionGroup) {
  return group.receivers.some((receiver) => receiver.enabled && /webhook|event.?hub|function|logic.?app|automation|runbook|itsm/i.test(`${receiver.type} ${receiver.name} ${receiver.destination ?? ""}`));
}

function subscriptionInfo(coverage: ActivityLogCoverage, subscriptionId: string) {
  const scope = coverage.scopes.find((item) => item.subscription_id === subscriptionId);
  const record = (coverage.metadata.subscriptions ?? []).find((item) => item.id === subscriptionId || item.subscription_id === subscriptionId);
  return {
    name: scope?.subscription_display_name || scope?.subscription_name || record?.display_name || record?.name || coverage.metadata.subscription_names?.[subscriptionId] || subscriptionId,
    state: scope?.subscription_state || record?.state || record?.status || coverage.metadata.subscription_states?.[subscriptionId] || "Unknown",
    environment: record?.environment || record?.tags?.environment || record?.tags?.Environment || "Unspecified",
  };
}


export function ActivityLogCoverageSection({ coverage, loading, error, disabled, onOpen }: { coverage?: ActivityLogCoverage; loading: boolean; error?: unknown; disabled: boolean; onOpen: () => void }) {
  const categories = new Map((coverage?.categories ?? []).map((item) => [item.category, item]));
  return <section className="overflow-hidden rounded-xl border bg-white" aria-labelledby="activity-log-coverage-heading">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3">
      <div><h2 id="activity-log-coverage-heading" className="text-sm font-semibold text-gray-900">Essential Activity Log coverage</h2><p className="text-xs text-gray-500">Subscription-level alerts for Azure health, security, and recommendation events. Activity Log alert rules have no direct alert-rule charge.</p></div>
      <button onClick={onOpen} disabled={disabled || loading} className={buttonPrimary}>{loading ? "Checking coverage…" : "Set up missing alerts"}</button>
    </div>
    {coverage?.partial && <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800" role="status">⚠ Coverage is partial. Some subscriptions or rules could not be inspected; review warnings before planning changes.</div>}
    {error ? <div className="p-4 text-xs text-rose-700">Coverage could not be loaded: {formatError(error)}</div> : loading && !coverage ? <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">{CATEGORIES.map((item) => <div key={item.id} className="h-36 animate-pulse rounded-lg bg-gray-100" />)}</div> : <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
      {CATEGORIES.map((definition) => { const item = categories.get(definition.id); const details = coverage?.scopes.flatMap((scope) => scope.categories.filter((category) => category.category === definition.id)) ?? []; const rules = details.flatMap((category) => category.rules); const enabledRules = rules.filter((rule) => rule.enabled).length; const issues = [...new Set(details.flatMap((category) => category.status === "covered" ? [] : [category.status === "no_routing" ? "Rule exists but has no healthy Action Group route." : category.status === "disabled" ? "Equivalent rule is disabled." : category.status === "unknown" ? "Coverage could not be confirmed." : "Rule is missing."]))]; return <article key={definition.id} className="flex min-h-40 flex-col rounded-lg border p-3">
        <div className="flex items-center justify-between gap-2"><h3 className="text-xs font-semibold text-gray-900">{definition.label}</h3><span className={`rounded px-2 py-0.5 text-[10px] font-medium capitalize ${statusTone(item?.status ?? "unknown")}`}>{item?.status ?? "unknown"}</span></div>
        <p className="mt-1 text-[10px] leading-4 text-gray-500">{definition.description}</p>
        <div className="mt-2 text-xs font-medium text-gray-700">{item ? `${item.covered_subscriptions} of ${item.subscription_count} subscriptions covered` : "Coverage unavailable"}</div>
        {item && <div className="text-[10px] text-gray-500">{enabledRules} enabled · {rules.length} existing rule{rules.length === 1 ? "" : "s"}</div>}
        {!!rules.length && <div className="mt-2 flex flex-wrap gap-1">{rules.slice(0, 3).map((rule) => <a key={rule.id} href={`https://portal.azure.com/#@/resource${rule.id}/overview`} target="_blank" rel="noreferrer" className="max-w-full truncate rounded bg-gray-50 px-1.5 py-0.5 text-[10px] text-brand hover:underline" title={rule.name}>{rule.name} ↗</a>)}</div>}
        <div className="mt-auto pt-2 text-[10px] leading-4"><div className={issues.length ? "text-amber-700" : "text-emerald-700"}>{issues[0] ?? "No coverage issues detected."}</div><div className="mt-0.5 text-gray-500">{issues.length ? "Create a routed rule or review the existing rule." : "No action recommended."}</div></div>
      </article>; })}
    </div>}
  </section>;
}

type WizardProps = {
  coverage: ActivityLogCoverage;
  scopeParams: AlertsManagerScopeParams;
  actionGroups: ManagedActionGroup[];
  capabilities?: AlertsManagerCapabilities;
  onClose: () => void;
  onOpenDiagnostics: () => void;
  onSubmitted: (pendingCount: number) => void;
};

export function ActivityLogSetupWizard({ coverage, scopeParams, actionGroups, capabilities, onClose, onOpenDiagnostics, onSubmitted }: WizardProps) {
  const unhealthyCategories = useMemo(() => coverage.categories.filter((item) => item.status !== "covered").map((item) => item.category), [coverage]);
  const unhealthySubscriptions = useMemo(() => coverage.scopes.filter((item) => !item.covered).map((item) => item.subscription_id), [coverage]);
  const [step, setStep] = useState(0);
  const [categories, setCategories] = useState<ActivityLogCategory[]>(unhealthyCategories.length ? unhealthyCategories : CATEGORIES.map((item) => item.id));
  const [subscriptions, setSubscriptions] = useState<string[]>(unhealthySubscriptions.length ? unhealthySubscriptions : coverage.scopes.map((item) => item.subscription_id));
  const [subscriptionSearch, setSubscriptionSearch] = useState("");
  const [subscriptionFilter, setSubscriptionFilter] = useState<"attention" | ActivityLogCoverageStatus | "all">("attention");
  const [includeCovered, setIncludeCovered] = useState(false);
  const [groupBy, setGroupBy] = useState<"none" | "status" | "environment">("status");
  const [pageSize, setPageSize] = useState<number>(50);
  const [page, setPage] = useState(1);
  const [resourceGroup, setResourceGroup] = useState("");
  const [namePrefix, setNamePrefix] = useState("essential-activity");
  const [serviceIncidentTypes, setServiceIncidentTypes] = useState<string[]>([...SERVICE_INCIDENT_TYPES]);
  const [serviceNames, setServiceNames] = useState("");
  const [serviceRegions, setServiceRegions] = useState("");
  const [resourceHealthStatuses, setResourceHealthStatuses] = useState<string[]>(["Degraded", "Unavailable", "Unknown"]);
  const [resourcePreviousStatuses, setResourcePreviousStatuses] = useState<string[]>([]);
  const [resourceCauseReason, setResourceCauseReason] = useState("");
  const [securityFilters, setSecurityFilters] = useState({ level: "", operation: "", resourceType: "", resourceGroup: "" });
  const [recommendationFilters, setRecommendationFilters] = useState({ level: "", operation: "", resourceType: "", resourceGroup: "" });
  const [routingMode, setRoutingMode] = useState<"common" | "per_category">("common");
  const healthyGroups = useMemo(() => actionGroups.filter((group) => group.enabled && group.active_receiver_count > 0), [actionGroups]);
  const [commonActionGroupId, setCommonActionGroupId] = useState(healthyGroups[0]?.id ?? "");
  const [categoryGroups, setCategoryGroups] = useState<Partial<Record<ActivityLogCategory, string>>>({});
  const [suggestions, setSuggestions] = useState<Array<{ action_group_id: string; name: string; confidence: number; reason: string }>>([]);
  const [suggestionContext, setSuggestionContext] = useState("");
  const [suggesting, setSuggesting] = useState(false);
  const [exporting, setExporting] = useState<"csv" | "json" | "">("");
  const [preview, setPreview] = useState<ActivityLogPlanPreview | null>(null);
  const [validation, setValidation] = useState<ActivityLogPlanValidation | null>(null);
  const [busy, setBusy] = useState<"" | "preview" | "validate" | "submit">("");
  const [error, setError] = useState("");
  const canSubmit = !!capabilities?.can_manage_rules && !capabilities.read_only;
  const clearResult = () => { setPreview(null); setValidation(null); setError(""); };

  const resourceGroupQueries = useQueries({
    queries: subscriptions.map((subscriptionId) => ({
      queryKey: ["alerts-authoring-options", scopeParams.connection_id, subscriptionId, ""],
      queryFn: () => api.alertsAuthoringOptions({ connection_id: scopeParams.connection_id, subscription_id: subscriptionId }),
      staleTime: 5 * 60 * 1000,
    })),
  });
  const resourceGroupsLoading = subscriptions.length > 0 && resourceGroupQueries.some((query) => query.isPending);
  const resourceGroupFailures = useMemo(() => resourceGroupQueries.flatMap((query, index) => {
    const subscriptionId = subscriptions[index];
    const message = query.error ? formatError(query.error) : query.data?.subscription_error;
    return message ? [{ subscriptionId, message }] : [];
  }), [resourceGroupQueries, subscriptions]);
  const commonResourceGroups = useMemo(() => {
    if (!subscriptions.length || resourceGroupsLoading || resourceGroupFailures.length) return [];
    const groupsBySubscription = resourceGroupQueries.map((query) => new Map(
      (query.data?.resource_groups ?? []).map((group) => [group.name.toLowerCase(), group]),
    ));
    const first = groupsBySubscription[0];
    if (!first) return [];
    return [...first.values()]
      .filter((group) => groupsBySubscription.every((groups) => groups.has(group.name.toLowerCase())))
      .sort((left, right) => left.name.localeCompare(right.name));
  }, [resourceGroupFailures.length, resourceGroupQueries, resourceGroupsLoading, subscriptions.length]);
  const resourceGroupAvailability = resourceGroupsLoading ? "Loading existing resource groups…" : resourceGroupFailures.length ? `Could not load resource groups for ${resourceGroupFailures.map((failure) => subscriptionInfo(coverage, failure.subscriptionId).name).join(", ")}.` : !commonResourceGroups.length ? subscriptions.length > 1 ? `No resource group name exists in all ${subscriptions.length} selected subscriptions. Create a consistently named group or change the subscription selection.` : subscriptions.length === 1 ? `No existing resource groups were found in ${subscriptionInfo(coverage, subscriptions[0]).name}.` : "Select at least one subscription first." : subscriptions.length > 1 ? `${commonResourceGroups.length} resource group name${commonResourceGroups.length === 1 ? "" : "s"} exist in all ${subscriptions.length} selected subscriptions.` : `${commonResourceGroups.length} existing resource group${commonResourceGroups.length === 1 ? "" : "s"} available.`;
  const resourceGroupError = resourceGroupsLoading || resourceGroupFailures.length || !commonResourceGroups.length ? "" : !resourceGroup.trim() ? "Resource group is required." : resourceGroup.length > 90 ? "Use 90 characters or fewer." : !/^[A-Za-z0-9_.()-]+$/.test(resourceGroup) || resourceGroup.endsWith(".") ? "Use letters, numbers, _, -, periods, or parentheses; do not end with a period." : !commonResourceGroups.some((group) => group.name.toLowerCase() === resourceGroup.trim().toLowerCase()) ? "Choose an existing resource group available in every selected subscription." : "";
  const namePrefixError = !namePrefix.trim() ? "Name prefix is required." : namePrefix.length > 120 ? "Use 120 characters or fewer." : !/^[A-Za-z0-9_.()-]+$/.test(namePrefix) || /^[.-]|[.]$/.test(namePrefix) ? "Use letters, numbers, _, -, periods, or parentheses; avoid leading/trailing punctuation." : "";
  const conditionsError = categories.includes("ServiceHealth") && !serviceIncidentTypes.length ? "Choose at least one Service Health incident type." : categories.includes("ResourceHealth") && !resourceHealthStatuses.length ? "Choose at least one Resource Health status." : "";
  const conditionsByCategory = useMemo(() => Object.fromEntries(categories.map((category) => {
    const conditions: ActivityLogCondition[] = [{ field: "category", equals: category }];
    if (category === "ServiceHealth") {
      conditions.push({ field: "properties.incidentType", containsAny: serviceIncidentTypes });
      if (commaList(serviceNames).length) conditions.push({ field: "properties.impactedServices[*].ServiceName", containsAny: commaList(serviceNames) });
      if (commaList(serviceRegions).length) conditions.push({ field: "properties.impactedServices[*].ImpactedRegions[*].RegionName", containsAny: commaList(serviceRegions) });
    }
    if (category === "ResourceHealth") {
      conditions.push({ field: "properties.currentHealthStatus", containsAny: resourceHealthStatuses });
      if (resourcePreviousStatuses.length) conditions.push({ field: "properties.previousHealthStatus", containsAny: resourcePreviousStatuses });
      if (commaList(resourceCauseReason).length) conditions.push({ field: "properties.cause", containsAny: commaList(resourceCauseReason) });
    }
    const optional = category === "Security" ? securityFilters : category === "Recommendation" ? recommendationFilters : null;
    if (optional) {
      if (commaList(optional.level).length) conditions.push({ field: "level", containsAny: commaList(optional.level) });
      if (commaList(optional.operation).length) conditions.push({ field: "operationName", containsAny: commaList(optional.operation) });
      if (commaList(optional.resourceType).length) conditions.push({ field: "resourceType", containsAny: commaList(optional.resourceType) });
      if (commaList(optional.resourceGroup).length) conditions.push({ field: "resourceGroupName", containsAny: commaList(optional.resourceGroup) });
    }
    return [category, conditions];
  })) as Partial<Record<ActivityLogCategory, ActivityLogCondition[]>>, [categories, recommendationFilters, resourceCauseReason, resourceHealthStatuses, resourcePreviousStatuses, securityFilters, serviceIncidentTypes, serviceNames, serviceRegions]);
  const actionableSubscriptions = useMemo(() => coverage.scopes.filter((scope) => scope.categories.some((item) => categories.includes(item.category) && item.status !== "covered")).map((scope) => scope.subscription_id), [categories, coverage.scopes]);
  const visibleSubscriptions = useMemo(() => coverage.scopes.filter((scope) => {
    const rows = scope.categories.filter((item) => categories.includes(item.category));
    const info = subscriptionInfo(coverage, scope.subscription_id);
    const search = subscriptionSearch.trim().toLowerCase();
    const matchesSearch = !search || `${info.name} ${scope.subscription_id} ${info.state}`.toLowerCase().includes(search);
    const matchesCovered = includeCovered || rows.some((item) => item.status !== "covered");
    const matchesStatus = subscriptionFilter === "all" || (subscriptionFilter === "attention" ? rows.some((item) => item.status !== "covered") : rows.some((item) => item.status === subscriptionFilter));
    return matchesSearch && matchesCovered && matchesStatus;
  }), [categories, coverage, includeCovered, subscriptionFilter, subscriptionSearch]);
  const sortedSubscriptions = useMemo(() => [...visibleSubscriptions].sort((left, right) => {
    const leftInfo = subscriptionInfo(coverage, left.subscription_id);
    const rightInfo = subscriptionInfo(coverage, right.subscription_id);
    const leftAttention = left.categories.some((item) => categories.includes(item.category) && item.status !== "covered") ? "Needs attention" : "Covered";
    const rightAttention = right.categories.some((item) => categories.includes(item.category) && item.status !== "covered") ? "Needs attention" : "Covered";
    const groupCompare = groupBy === "status" ? leftAttention.localeCompare(rightAttention) : groupBy === "environment" ? leftInfo.environment.localeCompare(rightInfo.environment) : 0;
    return groupCompare || leftInfo.name.localeCompare(rightInfo.name);
  }), [categories, coverage, groupBy, visibleSubscriptions]);
  const pageCount = Math.max(1, Math.ceil(sortedSubscriptions.length / pageSize));
  const pageSubscriptions = useMemo(() => sortedSubscriptions.slice((page - 1) * pageSize, page * pageSize), [page, pageSize, sortedSubscriptions]);
  const missingMetadataSubscriptions = useMemo(() => (coverage.metadata.subscriptions ?? []).filter((item) => {
    const id = item.subscription_id || item.id;
    return !!id && !coverage.scopes.some((scope) => scope.subscription_id === id);
  }), [coverage]);

  useEffect(() => { const escape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); }; window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape); }, [busy, onClose]);
  useEffect(() => { if (!commonActionGroupId && healthyGroups[0]) setCommonActionGroupId(healthyGroups[0].id); }, [commonActionGroupId, healthyGroups]);
  useEffect(() => { setPage(1); }, [categories, groupBy, includeCovered, pageSize, subscriptionFilter, subscriptionSearch]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  useEffect(() => {
    if (resourceGroupsLoading) return;
    const selected = commonResourceGroups.find((group) => group.name.toLowerCase() === resourceGroup.trim().toLowerCase());
    const next = selected?.name ?? commonResourceGroups[0]?.name ?? "";
    if (next === resourceGroup) return;
    setResourceGroup(next);
    setPreview(null);
    setValidation(null);
    setError("");
  }, [commonResourceGroups, resourceGroup, resourceGroupsLoading]);

  const request = useMemo<ActivityLogPlanRequest>(() => ({
    ...scopeParams,
    categories,
    subscription_ids: subscriptions,
    resource_group: resourceGroup.trim(),
    routing_mode: routingMode,
    common_action_group_id: routingMode === "common" ? commonActionGroupId : "",
    action_group_ids_by_category: Object.fromEntries(Object.entries(categoryGroups).map(([category, id]) => [category, id ? [id] : []])),
    name_prefix: namePrefix.trim(),
    conditions_by_category: conditionsByCategory,
  }), [scopeParams, categories, subscriptions, resourceGroup, routingMode, commonActionGroupId, categoryGroups, namePrefix, conditionsByCategory]);
  const routingComplete = routingMode === "common" ? !!commonActionGroupId : categories.every((category) => !!categoryGroups[category]);
  const setupValid = !resourceGroupsLoading && !resourceGroupFailures.length && !!commonResourceGroups.length && !resourceGroupError && !namePrefixError && !conditionsError;

  function toggle<T extends string>(values: T[], value: T, checked: boolean, setter: (next: T[]) => void) { setter(checked ? [...new Set([...values, value])] : values.filter((item) => item !== value)); clearResult(); }
  function replaceSubscriptions(next: string[]) { setSubscriptions([...new Set(next)]); clearResult(); }
  async function previewPlan() { setBusy("preview"); setError(""); setValidation(null); try { const result = await api.previewActivityLogPlan(request); setPreview(result.plan); setStep(4); } catch (cause) { setError(formatError(cause)); } finally { setBusy(""); } }
  async function validatePlan() { if (!preview) return; setBusy("validate"); setError(""); try { setValidation(await api.validateActivityLogPlan({ ...request, plan_token: preview.plan_token })); } catch (cause) { setError(formatError(cause)); } finally { setBusy(""); } }
  async function submitPlan() { if (!preview) return; setBusy("submit"); setError(""); try { const checked = await api.validateActivityLogPlan({ ...request, plan_token: preview.plan_token }); setValidation(checked); if (!checked.valid) return; const result = await api.submitActivityLogPlan({ ...request, plan_token: preview.plan_token, reason: "Set up essential Activity Log alert coverage" }); onSubmitted(result.change_count); } catch (cause) { setError(formatError(cause)); } finally { setBusy(""); } }
  async function suggestRouting() {
    const firstSubscription = subscriptions[0] || coverage.scopes[0]?.subscription_id;
    const subjectKind = scopeParams.workload_id ? "workload" : "resource";
    const subjectId = scopeParams.workload_id || (firstSubscription ? `/subscriptions/${firstSubscription}` : "");
    if (!subjectId) return;
    setSuggesting(true); setError("");
    try {
      const result = await api.suggestActionGroups({ connection_id: scopeParams.connection_id, workload_id: scopeParams.workload_id, subject_kind: subjectKind, subject_id: subjectId });
      setSuggestions([...result.suggestions].sort((a, b) => b.confidence - a.confidence));
      setSuggestionContext(result.owners.length ? `${result.ownership_source || "Ownership"}: ${result.owners.map((owner) => `${owner.display_name} (${owner.role})`).join(", ")}` : `No resolved owner; ranked by ${result.ownership_source || "scope fallback"}.`);
    } catch (cause) { setError(formatError(cause)); } finally { setSuggesting(false); }
  }
  async function exportCoverage(format: "csv" | "json") {
    setExporting(format); setError("");
    try { downloadBlob(await api.exportActivityLogCoverage(scopeParams, format), `activity-log-coverage-${new Date().toISOString().slice(0, 10)}.${format}`); }
    catch (cause) { setError(formatError(cause)); } finally { setExporting(""); }
  }

  return <div className="fixed inset-0 z-50 flex justify-end bg-black/40" role="dialog" aria-modal="true" aria-labelledby="activity-wizard-title">
    <div className="flex h-full w-full max-w-5xl flex-col bg-white shadow-2xl">
      <header className="border-b px-5 py-4"><div className="flex items-start gap-3"><div><h2 id="activity-wizard-title" className="text-base font-semibold text-gray-900">Set up essential Activity Log alerts</h2><p className="mt-0.5 text-xs text-gray-500">Build approval-gated pending changes. This wizard never writes to Azure.</p></div><button onClick={onClose} disabled={!!busy} aria-label="Close Activity Log setup" className="ml-auto text-gray-400 hover:text-gray-700 disabled:opacity-40">✕</button></div>
        <ol className="mt-4 grid grid-cols-5 gap-1" aria-label="Setup progress">{STEPS.map((label, index) => <li key={label} aria-current={step === index ? "step" : undefined} className={`rounded-md px-2 py-1.5 text-center text-[10px] font-medium ${step === index ? "bg-gray-900 text-white" : index < step ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}><span aria-hidden="true">{index < step ? "✓" : index + 1}. </span>{label}</li>)}</ol>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-5">
        {error && <div role="alert" className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">{error}</div>}
        {busy && <div role="status" aria-live="polite" className="mb-4 flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs text-sky-700"><span className="h-3 w-3 animate-spin rounded-full border-2 border-sky-200 border-t-sky-700" />{busy === "preview" ? "Building a read-only operation preview…" : busy === "validate" ? "Validating scope, permissions, destinations, and conflicts…" : "Creating pending managed changes…"}</div>}
        {step === 0 && <section><h3 className="text-sm font-semibold text-gray-900">Choose required categories</h3><p className="mt-1 text-xs text-gray-500">Missing and unhealthy categories are preselected.</p><div className="mt-4 grid gap-3 sm:grid-cols-2">{CATEGORIES.map((definition) => { const item = coverage.categories.find((row) => row.category === definition.id); return <label key={definition.id} className={`rounded-xl border p-4 ${categories.includes(definition.id) ? "border-indigo-300 bg-indigo-50/50" : ""}`}><span className="flex items-start gap-3"><input type="checkbox" checked={categories.includes(definition.id)} onChange={(event) => toggle(categories, definition.id, event.target.checked, setCategories)} /><span><span className="flex items-center gap-2 text-xs font-semibold text-gray-900">{definition.label}<span className={`rounded px-1.5 py-0.5 text-[9px] capitalize ${statusTone(item?.status ?? "unknown")}`}>{item?.status?.replaceAll("_", " ") ?? "unknown"}</span></span><span className="mt-1 block text-[10px] leading-4 text-gray-500">{definition.description}</span></span></span></label>; })}</div></section>}
        {step === 1 && <section><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-900">Choose target subscriptions</h3><p className="mt-1 text-xs text-gray-500">Client paging keeps large management-group scopes responsive. Selection is always explicit.</p></div><div className="flex gap-2"><button onClick={() => void exportCoverage("csv")} disabled={!!exporting} className={buttonSecondary}>{exporting === "csv" ? "Exporting…" : "⬇ CSV"}</button><button onClick={() => void exportCoverage("json")} disabled={!!exporting} className={buttonSecondary}>{exporting === "json" ? "Exporting…" : "JSON"}</button></div></div>
          <div className="mt-4 flex flex-wrap items-center gap-2"><input aria-label="Search subscriptions" value={subscriptionSearch} onChange={(event) => setSubscriptionSearch(event.target.value)} placeholder="Search subscriptions…" className="min-w-56 flex-1 rounded-lg border px-3 py-2 text-xs" /><select aria-label="Filter subscriptions by coverage status" value={subscriptionFilter} onChange={(event) => setSubscriptionFilter(event.target.value as typeof subscriptionFilter)} className="rounded-lg border px-3 py-2 text-xs"><option value="attention">Needs attention</option><option value="all">All statuses</option><option value="missing">Missing</option><option value="disabled">Disabled</option><option value="no_routing">No routing</option><option value="unknown">Unknown</option><option value="covered">Covered</option></select><select aria-label="Group subscriptions" value={groupBy} onChange={(event) => setGroupBy(event.target.value as typeof groupBy)} className="rounded-lg border px-3 py-2 text-xs"><option value="none">No grouping</option><option value="status">Group by status</option><option value="environment">Group by environment</option></select><label className="flex items-center gap-2 rounded-lg border px-3 py-2 text-xs"><input type="checkbox" checked={includeCovered} onChange={(event) => { const checked = event.target.checked; setIncludeCovered(checked); if (!checked) replaceSubscriptions(subscriptions.filter((id) => actionableSubscriptions.includes(id))); }} /> Include covered</label></div>
          {(coverage.partial || coverage.metadata.truncated || missingMetadataSubscriptions.length > 0) && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"><strong>Scope inventory warning:</strong> {missingMetadataSubscriptions.length ? `${missingMetadataSubscriptions.length} subscription${missingMetadataSubscriptions.length === 1 ? "" : "s"} appear in metadata but have no coverage row. ` : ""}{coverage.metadata.truncated ? "The inventory was truncated. " : ""}Unlisted subscriptions are not selected or changed.</div>}
          <div className="mt-3 flex flex-wrap items-center gap-2"><button className={buttonSecondary} onClick={() => replaceSubscriptions([...subscriptions, ...pageSubscriptions.map((item) => item.subscription_id)])}>Select page ({pageSubscriptions.length})</button><button className={buttonSecondary} onClick={() => replaceSubscriptions(subscriptions.filter((id) => !pageSubscriptions.some((item) => item.subscription_id === id)))}>Clear page</button><button className={buttonSecondary} onClick={() => replaceSubscriptions([...subscriptions, ...sortedSubscriptions.map((item) => item.subscription_id)])}>Select all filtered ({sortedSubscriptions.length})</button><button className={buttonSecondary} onClick={() => replaceSubscriptions(subscriptions.filter((id) => !sortedSubscriptions.some((item) => item.subscription_id === id)))}>Clear filtered</button><span className="text-[10px] text-gray-500">{subscriptions.length} selected · {sortedSubscriptions.length} filtered</span></div>
          <div className="mt-3 divide-y rounded-xl border">{pageSubscriptions.length ? pageSubscriptions.map((subscription, index) => { const info = subscriptionInfo(coverage, subscription.subscription_id); const selectedRows = subscription.categories.filter((item) => categories.includes(item.category)); const unhealthy = selectedRows.filter((item) => item.status !== "covered"); const group = groupBy === "status" ? (unhealthy.length ? "Needs attention" : "Covered") : groupBy === "environment" ? info.environment : ""; const previous = index ? pageSubscriptions[index - 1] : undefined; const previousInfo = previous ? subscriptionInfo(coverage, previous.subscription_id) : undefined; const previousUnhealthy = previous?.categories.some((item) => categories.includes(item.category) && item.status !== "covered"); const previousGroup = groupBy === "status" ? (previousUnhealthy ? "Needs attention" : "Covered") : groupBy === "environment" ? previousInfo?.environment : ""; return <div key={subscription.subscription_id}>{group && group !== previousGroup && <div className="bg-gray-100 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-600">{group}</div>}<label className="flex items-start gap-3 p-3 text-xs hover:bg-gray-50"><input type="checkbox" checked={subscriptions.includes(subscription.subscription_id)} onChange={(event) => toggle(subscriptions, subscription.subscription_id, event.target.checked, setSubscriptions)} /><span className="min-w-0 flex-1"><span className="font-medium text-gray-900">{info.name}</span><span className={`ml-2 rounded px-1.5 py-0.5 text-[9px] ${statusTone(unhealthy.length ? "partial" : "covered")}`}>{unhealthy.length ? "needs attention" : "covered"}</span><span className="ml-2 text-[9px] text-gray-500">{info.state} · {info.environment}</span><span className="mt-0.5 block break-all font-mono text-[9px] text-gray-400">{subscription.subscription_id}</span><span className={`mt-1 block text-[10px] ${unhealthy.length ? "text-amber-700" : "text-emerald-700"}`}>{selectedRows.map((item) => `${CATEGORY_LABEL[item.category]}: ${item.status.replaceAll("_", " ")}`).join(" · ") || "No selected categories"}</span></span></label></div>; }) : <div className="p-6 text-center text-xs text-gray-500">No subscriptions match these filters.</div>}</div>
          <div className="mt-3 flex items-center justify-end gap-2 text-xs"><label>Rows <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))} className="rounded border px-2 py-1">{PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}</select></label><button className={buttonSecondary} disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous</button><span>Page {page} of {pageCount}</span><button className={buttonSecondary} disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>Next</button></div>
        </section>}
        {step === 2 && <section className="space-y-5"><div><h3 className="text-sm font-semibold text-gray-900">Conditions and naming</h3><p className="mt-1 text-xs text-gray-500">Set the target resource group and generated rule-name prefix, then tune the supported health presets.</p></div>
          <div className="grid gap-4 sm:grid-cols-2"><label className="text-xs font-medium text-gray-700">Target resource group<select value={resourceGroup} onChange={(event) => { setResourceGroup(event.target.value); clearResult(); }} disabled={resourceGroupsLoading || !!resourceGroupFailures.length || !commonResourceGroups.length} aria-label="Target resource group for Activity Log alerts" aria-required="true" aria-invalid={!!resourceGroupError} aria-describedby={`activity-resource-group-help${resourceGroupError ? " activity-resource-group-error" : ""}`} className={`mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100 ${resourceGroupError && !resourceGroupsLoading ? "border-rose-400" : ""}`}><option value="">{resourceGroupsLoading ? "Loading resource groups…" : resourceGroupFailures.length ? "Resource groups unavailable" : commonResourceGroups.length ? "Select resource group…" : "No eligible resource groups"}</option>{commonResourceGroups.map((group) => <option key={group.name.toLowerCase()} value={group.name}>{group.name}{subscriptions.length > 1 ? ` — in all ${subscriptions.length} subscriptions` : group.location ? ` — ${group.location}` : ""}</option>)}</select><span id="activity-resource-group-help" role="status" aria-live="polite" className={`mt-1 block text-[10px] font-normal ${resourceGroupFailures.length || (!resourceGroupsLoading && !commonResourceGroups.length) ? "text-amber-700" : "text-gray-500"}`}>{resourceGroupAvailability}</span>{resourceGroupFailures.map((failure) => <span key={failure.subscriptionId} className="mt-0.5 block text-[9px] font-normal text-rose-700">{subscriptionInfo(coverage, failure.subscriptionId).name}: {failure.message}</span>)}{resourceGroupError && <span id="activity-resource-group-error" role="alert" className="mt-1 block text-[10px] text-rose-700">{resourceGroupError}</span>}</label><label className="text-xs font-medium text-gray-700">Rule name prefix<input value={namePrefix} onChange={(event) => { setNamePrefix(event.target.value); clearResult(); }} aria-invalid={!!namePrefixError} aria-describedby="activity-name-prefix-error" className={`mt-1 w-full rounded-lg border px-3 py-2 font-normal ${namePrefixError ? "border-rose-400" : ""}`} />{namePrefixError && <span id="activity-name-prefix-error" role="alert" className="mt-1 block text-[10px] text-rose-700">{namePrefixError}</span>}</label></div>
          <div className="grid gap-3 sm:grid-cols-2">{categories.map((category) => <div key={category} className="rounded-xl border p-4"><div className="text-xs font-semibold text-gray-900">{CATEGORY_LABEL[category]}</div><div className="mt-2 text-[10px] text-gray-600">Mandatory: category equals <strong>{category}</strong></div>
            {category === "ServiceHealth" && <><PresetChoices title="Incident types" values={SERVICE_INCIDENT_TYPES} selected={serviceIncidentTypes} onToggle={(value, checked) => toggle(serviceIncidentTypes, value, checked, setServiceIncidentTypes)} /><CommaInput label="Impacted services (optional)" value={serviceNames} onChange={setServiceNames} placeholder="Virtual Machines, Storage" /><CommaInput label="Impacted regions (optional)" value={serviceRegions} onChange={setServiceRegions} placeholder="West Europe, East US" /></>}
            {category === "ResourceHealth" && <><PresetChoices title="Current health statuses" values={RESOURCE_HEALTH_STATUSES} selected={resourceHealthStatuses} onToggle={(value, checked) => toggle(resourceHealthStatuses, value, checked, setResourceHealthStatuses)} /><PresetChoices title="Previous health statuses (optional)" values={RESOURCE_HEALTH_STATUSES} selected={resourcePreviousStatuses} onToggle={(value, checked) => toggle(resourcePreviousStatuses, value, checked, setResourcePreviousStatuses)} /><CommaInput label="Cause or reason (optional)" value={resourceCauseReason} onChange={setResourceCauseReason} placeholder="PlatformInitiated, UserInitiated" /></>}
            {(category === "Security" || category === "Recommendation") && <OptionalEventFilters value={category === "Security" ? securityFilters : recommendationFilters} onChange={category === "Security" ? setSecurityFilters : setRecommendationFilters} />}
          </div>)}</div>
          {conditionsError && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">{conditionsError}</div>}
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs leading-5 text-sky-800"><span className="min-w-0 flex-1"><strong>Security routing:</strong> Security alerts notify an Action Group. Full Activity Log export to a SIEM requires a separately reviewed diagnostic setting and is never combined silently with this wizard.</span><button type="button" onClick={onOpenDiagnostics} className="shrink-0 rounded-lg border border-sky-300 bg-white px-3 py-1.5 font-medium text-sky-800 hover:bg-sky-100">Configure diagnostic settings</button></div>
        </section>}
        {step === 3 && <section><h3 className="text-sm font-semibold text-gray-900">Choose notification routing</h3><p className="mt-1 text-xs text-gray-500">Only enabled Action Groups with at least one active receiver are available. Full destination values are shown for review.</p><div className="mt-4 grid gap-3 sm:grid-cols-2"><label className={`rounded-xl border p-3 text-xs ${routingMode === "common" ? "border-indigo-300 bg-indigo-50" : ""}`}><input type="radio" checked={routingMode === "common"} onChange={() => { setRoutingMode("common"); clearResult(); }} /> <span className="font-medium">One common Action Group</span><span className="mt-1 block text-[10px] text-gray-500">Route every selected category to one destination set.</span></label><label className={`rounded-xl border p-3 text-xs ${routingMode === "per_category" ? "border-indigo-300 bg-indigo-50" : ""}`}><input type="radio" checked={routingMode === "per_category"} onChange={() => { setRoutingMode("per_category"); clearResult(); }} /> <span className="font-medium">Action Group per category</span><span className="mt-1 block text-[10px] text-gray-500">Choose specialized health, security, and recommendation routes.</span></label></div>{routingMode === "common" ? <div className="mt-4 grid gap-2 sm:grid-cols-2">{healthyGroups.map((group) => <ActionGroupChoice key={group.id} group={group} checked={commonActionGroupId === group.id} onChange={() => { setCommonActionGroupId(group.id); clearResult(); }} name="common-group" />)}</div> : <div className="mt-4 space-y-4">{categories.map((category) => <div key={category}><div className="mb-2 text-xs font-semibold text-gray-800">{CATEGORY_LABEL[category]}</div><div className="grid gap-2 sm:grid-cols-2">{healthyGroups.map((group) => <ActionGroupChoice key={group.id} group={group} checked={categoryGroups[category] === group.id} onChange={() => { setCategoryGroups((current) => ({ ...current, [category]: group.id })); clearResult(); }} name={`group-${category}`} />)}</div></div>)}</div>}{!healthyGroups.length && <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">No enabled Action Group with an active destination is available in this scope. Create or enable one before submitting this plan.</div>}</section>}
        {step === 3 && <section className="mt-4 rounded-xl border border-indigo-200 bg-indigo-50 p-3"><div className="flex flex-wrap items-start gap-3"><div className="min-w-0 flex-1"><h4 className="text-xs font-semibold text-indigo-900">Ownership-based suggestions</h4><p className="mt-1 text-[10px] text-indigo-700">Ranks matching Action Groups and explains ownership evidence. SIEM capability is a visible heuristic only.</p></div><button onClick={() => void suggestRouting()} disabled={suggesting || !subscriptions.length} className={buttonSecondary}>{suggesting ? "Matching…" : "Suggest from ownership"}</button></div>{suggestionContext && <div className="mt-2 text-[10px] text-indigo-700">{suggestionContext}</div>}{!!suggestions.length && <div className="mt-2 space-y-1">{suggestions.slice(0, 5).map((item, index) => { const group = healthyGroups.find((candidate) => candidate.id.toLowerCase().replace(/\/$/, "") === item.action_group_id.toLowerCase().replace(/\/$/, "")); return <div key={item.action_group_id} className="flex flex-wrap items-center gap-2 rounded bg-white p-2 text-xs"><span className="font-semibold">#{index + 1} {item.name}</span><span className="text-[10px] text-indigo-700">{Math.round(item.confidence * 100)}% · {item.reason}</span>{group && siemRoute(group) && <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[9px] font-medium text-violet-700" title="Heuristic only; verify receiver and diagnostic-settings configuration">SIEM-capable route?</span>}<button onClick={() => { if (routingMode === "common") setCommonActionGroupId(item.action_group_id); else setCategoryGroups((current) => Object.fromEntries(categories.map((category) => [category, current[category] || item.action_group_id]))); clearResult(); }} disabled={!group} className="ml-auto rounded border border-indigo-300 px-2 py-0.5 text-[10px] text-indigo-700 disabled:opacity-40">Use</button>{group && <span className="basis-full break-all text-[10px] text-gray-700">{destinationSummary(group)}</span>}</div>; })}</div>}</section>}
        {step === 4 && <section>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div><h3 className="text-sm font-semibold text-gray-900">Review server-generated operations</h3><p className="mt-1 text-xs text-gray-500">Create, update, and enable operations become pending managed changes only. No-op items are retained for transparency; blocked items cannot be submitted.</p></div>
            {preview && <div className="flex flex-wrap gap-1">{(["create", "update", "enable", "equivalent", "blocked", "invalid"] as const).map((operation) => <span key={operation} className={`rounded px-2 py-1 text-[10px] font-medium ${operationTone(operation)}`}>{operation} {preview.counts?.[operation] ?? preview.items.filter((item) => item.classification === operation).length}</span>)}</div>}
          </div>
          {!preview ? <div className="mt-8 text-center text-xs text-gray-400">Build the preview to review exact operations.</div> : <>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-xl border bg-gray-50 p-3"><div className="text-[10px] uppercase tracking-wide text-gray-500">Operations</div><div className="mt-1 text-lg font-semibold text-gray-900">{preview.counts?.total ?? preview.items.length}</div></div>
              <div className="rounded-xl border bg-gray-50 p-3"><div className="text-[10px] uppercase tracking-wide text-gray-500">Pending changes</div><div className="mt-1 text-lg font-semibold text-gray-900">{preview.counts?.actionable ?? preview.items.filter((item) => item.actionable).length}</div></div>
              <div className={`rounded-xl border p-3 ${preview.valid ? "border-emerald-200 bg-emerald-50" : "border-rose-200 bg-rose-50"}`}><div className="text-[10px] uppercase tracking-wide text-gray-500">Preview status</div><div className={`mt-1 text-sm font-semibold ${preview.valid ? "text-emerald-700" : "text-rose-700"}`}>{preview.valid ? "Ready to validate" : "Needs attention"}</div></div>
            </div>
            {!!preview.warnings.length && <div className="mt-4 space-y-2" aria-label="Preview warnings">{preview.warnings.map((warning, index) => <div key={`${index}-${warning}`} className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"><span className="font-semibold">Preview warning:</span> {warning}</div>)}</div>}
            <div className="mt-4 space-y-3">{preview.items.map((item) => { const info = subscriptionInfo(coverage, item.subscription_id); const conditions = item.desired.activity_conditions ?? []; return <article key={item.key} className={`overflow-hidden rounded-xl border ${item.validation_status === "valid" ? "border-gray-200" : "border-rose-200"}`}>
              <div className="flex flex-wrap items-start gap-3 bg-gray-50 px-4 py-3">
                <span className={`rounded px-2 py-1 text-[10px] font-semibold uppercase ${operationTone(item.classification)}`}>{item.classification}</span>
                <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h4 className="text-xs font-semibold text-gray-900">{item.category_label || CATEGORY_LABEL[item.category]} · {String(item.desired.name || "Unnamed rule")}</h4><span className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${item.validation_status === "valid" ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"}`}>{item.validation_status}</span></div><div className="mt-1 text-[10px] text-gray-600">{info.name} <span className="text-gray-400">· {info.state}</span></div><div className="mt-0.5 break-all font-mono text-[9px] text-gray-400">{item.subscription_id}</div></div>
                <div className="text-right text-[10px] text-gray-500"><div className="font-medium capitalize text-gray-700">{item.risk} risk</div><div>{item.cost.currency} {item.cost.estimated_monthly_cost.toFixed(2)}/month · {item.cost.classification}</div></div>
              </div>
              <div className="grid gap-4 p-4 md:grid-cols-2">
                <div><div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Why this operation</div><p className="mt-1 text-xs leading-5 text-gray-700">{item.reason || (item.actionable ? "A managed change is required." : "No managed change is required.")}</p></div>
                <div><div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Notification routing</div><div className="mt-1 text-xs text-gray-700">{item.receiver_count} active receiver{item.receiver_count === 1 ? "" : "s"}</div><div className="mt-1 space-y-1">{item.selected_action_groups.length ? item.selected_action_groups.map((group) => { const fullGroup = actionGroups.find((candidate) => candidate.id.toLowerCase().replace(/\/$/, "") === group.id.toLowerCase().replace(/\/$/, "")); return <div key={group.id} title={group.id} className={`rounded px-2 py-1 text-[10px] ${group.enabled && group.active_receiver_count > 0 ? "bg-indigo-50 text-indigo-700" : "bg-amber-50 text-amber-700"}`}><div>{group.name} · {group.active_receiver_count}/{group.receiver_count} active</div>{fullGroup && <div className="mt-0.5 break-all text-gray-700">{destinationSummary(fullGroup)}</div>}</div>; }) : <span className="text-[10px] text-rose-700">No Action Group selected</span>}</div></div>
              </div>
              {(item.errors.length > 0 || item.issues.length > 0 || item.blocker) && <div className="mx-4 mb-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-[10px] leading-4 text-rose-800"><div className="font-semibold">Issues requiring attention</div>{item.errors.map((message, index) => <div key={`error-${index}`}>• {message}</div>)}{item.issues.map((issue, index) => <div key={`issue-${index}`}>• <span className="font-medium uppercase">{issue.severity}</span> {issue.message}</div>)}{item.blocker && <div>• Blocked by change {item.blocker.change_id} ({item.blocker.status}) targeting {item.blocker.target_id}</div>}</div>}
              {!!item.existing_rule_details.length && <details className="border-t px-4 py-3"><summary className="cursor-pointer text-xs font-medium text-gray-700">Existing rules ({item.existing_rule_details.length})</summary><div className="mt-3 space-y-2">{item.existing_rule_details.map((rule) => <div key={rule.id} className="rounded-lg bg-gray-50 p-3 text-[10px] text-gray-600"><div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-gray-800">{rule.name}</span><span className={`rounded px-1.5 py-0.5 ${rule.enabled ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{rule.enabled ? "enabled" : "disabled"}</span></div><div className="mt-1">{rule.reason}</div><div className="mt-1 break-all font-mono text-[9px] text-gray-400">{rule.id}</div><div className="mt-1">{rule.action_group_ids.length} Action Group{rule.action_group_ids.length === 1 ? "" : "s"} · {rule.activity_conditions.length} condition{rule.activity_conditions.length === 1 ? "" : "s"}</div></div>)}</div></details>}
              <details className="border-t px-4 py-3"><summary className="cursor-pointer text-xs font-medium text-gray-700">Technical details</summary><div className="mt-3 grid gap-3 text-[10px] text-gray-600 sm:grid-cols-2"><div><div className="font-semibold text-gray-700">Target resource</div><div className="mt-1 break-all font-mono text-[9px]">{item.target_id}</div></div><div><div className="font-semibold text-gray-700">Desired conditions ({conditions.length})</div><div className="mt-1 space-y-1">{conditions.length ? conditions.map((condition, index) => <div key={`${condition.field}-${index}`} className="rounded bg-gray-50 px-2 py-1"><span className="font-medium">{condition.field}</span>: {condition.equals ?? condition.containsAny?.join(", ") ?? "Configured"}</div>) : <div>None supplied</div>}</div></div>{!!item.issues.length && <div className="sm:col-span-2"><div className="font-semibold text-gray-700">Issue metadata</div>{item.issues.map((issue, index) => <div key={index} className="mt-1">{issue.type} · {issue.severity}{issue.rule_ids.length ? ` · ${issue.rule_ids.join(", ")}` : ""}</div>)}</div>}</div></details>
            </article>; })}</div>
          </>}
          {validation && <div role="status" className={`mt-4 rounded-lg border p-3 text-xs ${validation.valid ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}><div className="font-semibold">{validation.valid ? "Validation passed" : "Validation failed"}</div><div className="mt-1">{validation.valid ? "The plan is ready to create approval-gated pending changes." : validation.errors.join(" · ") || "Resolve the operation errors and build a new preview."}</div></div>}
        </section>}
      </div>
      <footer className="flex flex-wrap items-center gap-2 border-t px-5 py-3"><span className="text-[10px] text-gray-500">{categories.length} categories · {subscriptions.length} subscriptions · direct rule cost $0 · approval required</span><div className="ml-auto flex gap-2"><button onClick={onClose} disabled={!!busy} className={buttonSecondary}>Cancel</button>{step > 0 && <button onClick={() => { setStep((current) => current - 1); setError(""); }} disabled={!!busy} className={buttonSecondary}>Back</button>}{step < 3 && <button onClick={() => setStep((current) => current + 1)} disabled={!!busy || (step === 0 && !categories.length) || (step === 1 && !subscriptions.length) || (step === 2 && !setupValid)} className={buttonPrimary}>Continue</button>}{step === 3 && <button onClick={() => void previewPlan()} disabled={!!busy || !routingComplete || !canSubmit || !setupValid} className={buttonPrimary}>{busy === "preview" ? "Building preview…" : "Review plan"}</button>}{step === 4 && <><button onClick={() => void validatePlan()} disabled={!!busy || !preview} className={buttonSecondary}>{busy === "validate" ? "Validating…" : "Validate"}</button><button onClick={() => void submitPlan()} disabled={!!busy || !preview || !canSubmit || validation?.valid !== true} className={buttonPrimary}>{busy === "submit" ? "Submitting…" : "Submit pending changes"}</button></>}</div></footer>
    </div>
  </div>;
}

function PresetChoices({ title, values, selected, onToggle }: { title: string; values: readonly string[]; selected: string[]; onToggle: (value: string, checked: boolean) => void }) {
  return <fieldset className="mt-3"><legend className="text-[10px] font-medium text-gray-700">{title}</legend><div className="mt-2 flex flex-wrap gap-2">{values.map((value) => <label key={value} className={`flex items-center gap-1.5 rounded-lg border px-2 py-1.5 text-[10px] ${selected.includes(value) ? "border-indigo-300 bg-indigo-50 text-indigo-700" : "text-gray-600"}`}><input type="checkbox" checked={selected.includes(value)} onChange={(event) => onToggle(value, event.target.checked)} />{value}</label>)}</div></fieldset>;
}

function CommaInput({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (value: string) => void; placeholder: string }) {
  return <label className="mt-3 block text-[10px] font-medium text-gray-700">{label}<input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} className="mt-1 w-full rounded-lg border px-2 py-1.5 font-normal" /><span className="mt-1 block text-[9px] font-normal text-gray-400">Comma-separated; whitespace and duplicates are removed.</span></label>;
}

type OptionalFilters = { level: string; operation: string; resourceType: string; resourceGroup: string };
function OptionalEventFilters({ value, onChange }: { value: OptionalFilters; onChange: (value: OptionalFilters) => void }) {
  const update = (field: keyof OptionalFilters, next: string) => onChange({ ...value, [field]: next });
  return <div className="mt-3 grid gap-2 sm:grid-cols-2"><CommaInput label="Level (optional)" value={value.level} onChange={(next) => update("level", next)} placeholder="Error, Warning" /><CommaInput label="Operation (optional)" value={value.operation} onChange={(next) => update("operation", next)} placeholder="Microsoft.Security/..." /><CommaInput label="Resource type (optional)" value={value.resourceType} onChange={(next) => update("resourceType", next)} placeholder="Microsoft.Compute/virtualMachines" /><CommaInput label="Resource group (optional)" value={value.resourceGroup} onChange={(next) => update("resourceGroup", next)} placeholder="production-rg" /></div>;
}

function ActionGroupChoice({ group, checked, onChange, name }: { group: ManagedActionGroup; checked: boolean; onChange: () => void; name: string }) {
  return <label className={`flex items-start gap-3 rounded-lg border p-3 text-xs ${checked ? "border-indigo-300 bg-indigo-50" : ""}`}><input type="radio" name={name} checked={checked} onChange={onChange} /><span className="min-w-0"><span className="flex items-center gap-2 font-medium text-gray-900">{group.name}{siemRoute(group) && <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[9px] text-violet-700" title="Heuristic only; destinations are shown below">SIEM-capable route?</span>}</span><span className="mt-0.5 block text-[10px] text-gray-500">{group.active_receiver_count} active destination{group.active_receiver_count === 1 ? "" : "s"}</span><span className="mt-1 block break-all text-[10px] text-gray-700">{destinationSummary(group)}</span></span></label>;
}
