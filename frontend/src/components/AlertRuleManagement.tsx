import { Fragment, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import {
  api,
  type AlertsManagerCapabilities,
  type EditableAlertRule,
  type ManagedActionGroup,
  type ManagedAlertRule,
  type MetricDefinition,
  type NoiseGuardResult,
} from "../api";
import { formatError } from "../utils/format";
import { ResourcePicker } from "./ResourcePicker";
import { AzurePlacementFields, AzureResourceDropdown, SelectedScopesTable, resolvedScopesToWorkloadNodes, useResolvedAlertScopes } from "./AlertsAuthoringSelectors";

export type RuleFamily = "metric" | "log" | "activity" | "smart" | "prometheus";

type RuleGrouping = "none" | "family" | "category";

const ruleFamilyLabels: Record<RuleFamily, string> = {
  metric: "Metric",
  log: "Log query",
  activity: "Activity Log",
  smart: "Smart Detector",
  prometheus: "Prometheus",
};

const activityCategoryLabels = ["Administrative", "Service Health", "Resource Health", "Security", "Recommendation"] as const;

function activityCategoryLabel(category?: string): string {
  const normalized = (category || "").replace(/[\s_-]/g, "").toLowerCase();
  if (normalized === "administrative") return "Administrative";
  if (normalized === "servicehealth") return "Service Health";
  if (normalized === "resourcehealth") return "Resource Health";
  if (normalized === "security") return "Security";
  if (normalized === "recommendation") return "Recommendation";
  return "Other Activity Log";
}

type DisplayActionGroup = ManagedActionGroup & {
  subscription_name?: string;
  subscription_display_name?: string;
  health_status?: string;
};

function actionGroupSubscriptionId(group: ManagedActionGroup): string {
  return group.subscription_id || group.id.match(/\/subscriptions\/([^/]+)/i)?.[1] || "";
}

function actionGroupSubscriptionName(group: ManagedActionGroup): string {
  const display = group as DisplayActionGroup;
  return display.subscription_display_name || display.subscription_name || "";
}

function actionGroupHealth(group: ManagedActionGroup): string {
  const reported = (group as DisplayActionGroup).health_status;
  if (reported) return reported;
  if (!group.enabled) return "Disabled";
  if (group.receiver_count === 0 || group.active_receiver_count === 0) return "No active receivers";
  if (group.active_receiver_count < group.receiver_count) return "Partially active";
  return "Healthy";
}

function groupedActionGroups(actionGroups: ManagedActionGroup[]) {
  const groups = new Map<string, ManagedActionGroup[]>();
  actionGroups.forEach((group) => {
    const subscriptionId = actionGroupSubscriptionId(group);
    const key = subscriptionId.toLowerCase() || "unknown";
    groups.set(key, [...(groups.get(key) || []), group]);
  });
  return Array.from(groups.values())
    .map((items) => ({
      subscriptionId: actionGroupSubscriptionId(items[0]),
      subscriptionName: actionGroupSubscriptionName(items[0]),
      items: [...items].sort((left, right) => left.name.localeCompare(right.name)),
    }))
    .sort((left, right) => (left.subscriptionName || left.subscriptionId).localeCompare(right.subscriptionName || right.subscriptionId));
}

function subscriptionHeading(subscriptionName: string, subscriptionId: string): string {
  if (!subscriptionId) return "Subscription unavailable";
  return subscriptionName && subscriptionName.toLowerCase() !== subscriptionId.toLowerCase()
    ? `${subscriptionName} — ${subscriptionId}`
    : subscriptionId;
}

function isCrossSubscription(group: ManagedActionGroup, ruleSubscriptionId: string): boolean {
  const groupSubscriptionId = actionGroupSubscriptionId(group);
  return !!groupSubscriptionId && !!ruleSubscriptionId && groupSubscriptionId.toLowerCase() !== ruleSubscriptionId.toLowerCase();
}

function ActionGroupChoices({ actionGroups, selectedIds, ruleSubscriptionId, onToggle }: {
  actionGroups: ManagedActionGroup[];
  selectedIds: string[];
  ruleSubscriptionId: string;
  onToggle: (group: ManagedActionGroup, selected: boolean) => void;
}) {
  return <div className="mt-2 space-y-3">{groupedActionGroups(actionGroups).map((subscription) => <section key={subscription.subscriptionId || "unknown"} className="overflow-hidden rounded-lg border bg-gray-50/60">
    <div className="border-b bg-gray-100 px-3 py-1.5 text-[11px] font-semibold text-gray-700">{subscriptionHeading(subscription.subscriptionName, subscription.subscriptionId)}</div>
    <div className="grid gap-2 p-2 sm:grid-cols-2">{subscription.items.map((group) => {
      const crossSubscription = isCrossSubscription(group, ruleSubscriptionId);
      return <label key={group.id} className={`flex items-start gap-2 rounded border bg-white px-2.5 py-2 ${crossSubscription ? "border-amber-300" : "border-gray-200"}`}>
        <input type="checkbox" className="mt-0.5" checked={selectedIds.includes(group.id)} onChange={(event) => onToggle(group, event.target.checked)} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5"><span className="font-medium text-gray-800">{group.name}</span>{crossSubscription && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-amber-800">Cross-subscription</span>}</span>
          <span className="mt-0.5 block text-[10px] text-gray-500">Resource group: {group.resource_group || "—"} · {group.enabled ? "Enabled" : "Disabled"} · {actionGroupHealth(group)} · {group.active_receiver_count}/{group.receiver_count} active receivers</span>
          {group.receivers.length > 0 && <span className="mt-1 block space-y-0.5 text-[10px] text-gray-500">{group.receivers.map((receiver, index) => <span key={`${receiver.type}:${receiver.name}:${index}`} className="block break-all"><span className={receiver.enabled ? "text-emerald-700" : "text-gray-400"}>{receiver.enabled ? "●" : "○"}</span> {receiver.type} · {receiver.name}{receiver.destination ? ` · ${receiver.destination}` : receiver.masked ? ` · ${receiver.masked}` : ""}</span>)}</span>}
        </span>
      </label>;
    })}</div>
  </section>)}</div>;
}

type Condition = EditableAlertRule["conditions"][number];

export function newAlertRule(family: RuleFamily, subscriptionId = ""): EditableAlertRule {
  const base: EditableAlertRule = {
    id: "",
    name: "",
    type: family === "metric" ? "microsoft.insights/metricalerts" : family === "log" ? "microsoft.insights/scheduledqueryrules" : family === "activity" ? "microsoft.insights/activitylogalerts" : family === "smart" ? "microsoft.alertsmanagement/smartdetectoralertrules" : "microsoft.alertsmanagement/prometheusrulegroups",
    family,
    subscription_id: subscriptionId,
    resource_group: "",
    location: family === "log" || family === "prometheus" ? "" : "Global",
    enabled: false,
    severity: family === "activity" || family === "prometheus" ? null : 3,
    description: "",
    scopes: [],
    action_group_ids: [],
    condition_count: 1,
    evaluation_frequency: family === "activity" || family === "prometheus" ? "" : "PT5M",
    window_size: family === "activity" ? "" : "PT15M",
    state_hash: "",
    tags: { "managed-by": "alerts-manager" },
    auto_mitigate: true,
    target_resource_type: "",
    target_resource_region: "",
    identity: {},
    conditions: family === "metric" ? [blankMetricCondition()] : family === "log" ? [blankLogCondition()] : [],
    activity_conditions: family === "activity" ? [{ field: "category", equals: "Administrative" }] : [],
    display_name: "",
    target_resource_types: [],
    detector_id: "",
    detector_parameters: {},
    frequency: family === "smart" ? "PT1M" : "PT5M",
    throttling_duration: "PT0M",
    interval: "PT1M",
    cluster_name: "",
    prometheus_rules: family === "prometheus" ? [{ alert: "", expression: "", enabled: true, for: "PT5M", severity: 3, actions: [] }] : [],
  };
  return base;
}

function blankMetricCondition(): Condition {
  return {
    name: "condition-1",
    metric_name: "",
    metric_namespace: "",
    threshold_type: "static",
    operator: "GreaterThan",
    threshold: 80,
    aggregation: "Average",
    sensitivity: "Medium",
    min_failing_periods: 1,
    evaluation_periods: 1,
    dimensions: [],
  };
}

function blankLogCondition(): Condition {
  return {
    query: "",
    operator: "GreaterThan",
    threshold: 0,
    aggregation: "Count",
    min_failing_periods: 1,
    evaluation_periods: 1,
    dimensions: [],
  };
}

export function ruleFromGap(gap: { resource_id: string; resource_name: string; resource_type?: string; subscription_id?: string; resource_group?: string; location?: string; alert_key?: string; signal: string; rule_name: string; risk: string; recommended?: { metric?: string; operator?: string; threshold?: number | null; window?: string } }, subscriptionId = ""): EditableAlertRule {
  const rule = newAlertRule("metric", subscriptionId);
  const resourceId = gap.resource_id || "";
  const match = resourceId.match(/\/subscriptions\/([^/]+)/i);
  const rg = resourceId.match(/\/resourceGroups\/([^/]+)/i);
  rule.subscription_id = subscriptionId || gap.subscription_id || match?.[1] || "";
  rule.resource_group = gap.resource_group || rg?.[1] || "";
  rule.location = "Global";
  rule.name = (gap.rule_name || `${gap.resource_name}-${gap.alert_key || gap.signal}` || "new-metric-alert").replace(/[^A-Za-z0-9_.-]+/g, "-").slice(0, 120);
  rule.description = "Created from an Alerts Manager coverage gap. Review the live metric definition and threshold before approval.";
  rule.scopes = resourceId ? [resourceId] : [];
  rule.severity = gap.risk === "critical" ? 0 : gap.risk === "error" ? 1 : 2;
  rule.conditions[0].metric_name = gap.recommended?.metric || gap.signal || "";
  rule.conditions[0].metric_namespace = gap.resource_type || "";
  rule.conditions[0].operator = gap.recommended?.operator || "GreaterThan";
  rule.conditions[0].threshold = gap.recommended?.threshold ?? 80;
  rule.window_size = gap.recommended?.window || "PT15M";
  rule.enabled = false;
  return rule;
}

export function ManagedAlertRulesTable({ rows, caps, busy, actionGroups = [], onCreate, onEdit, onClone, onToggle, onDelete, onBulk }: {
  rows: ManagedAlertRule[];
  caps?: AlertsManagerCapabilities;
  busy: string;
  actionGroups?: ManagedActionGroup[];
  onCreate: (family: RuleFamily) => void;
  onEdit: (row: ManagedAlertRule) => void;
  onClone: (row: ManagedAlertRule) => void;
  onToggle: (row: ManagedAlertRule) => void;
  onDelete: (row: ManagedAlertRule) => void;
  onBulk?: (rows: ManagedAlertRule[], action: "enable" | "disable" | "delete" | "add_action_group", actionGroupId: string) => void;
}) {
  const [family, setFamily] = useState<"all" | RuleFamily>("all");
  const [state, setState] = useState<"all" | "enabled" | "disabled">("all");
  const [search, setSearch] = useState("");
  const [grouping, setGrouping] = useState<RuleGrouping>("none");
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkAction, setBulkAction] = useState<"enable" | "disable" | "delete" | "add_action_group">("disable");
  const [bulkGroup, setBulkGroup] = useState("");
  const bulkActionGroupSubscriptions = useMemo(() => groupedActionGroups(actionGroups), [actionGroups]);
  const segmentButton = (active: boolean) =>
    `px-2.5 py-1.5 text-xs transition ${active ? "bg-brand font-medium text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`;
  const filtered = useMemo(() => rows.filter((row) =>
    (family === "all" || row.family === family)
    && (state === "all" || row.enabled === (state === "enabled"))
    && (!search || `${row.name} ${row.description} ${row.scopes.join(" ")}`.toLowerCase().includes(search.toLowerCase()))
  ), [rows, family, state, search]);
  const grouped = useMemo(() => {
    if (grouping === "none") return [{ key: "all", label: "", rows: filtered }];
    const order = grouping === "family"
      ? Object.keys(ruleFamilyLabels) as RuleFamily[]
      : ["metric", "log", ...activityCategoryLabels, "Other Activity Log", "smart", "prometheus"];
    return order.map((key) => ({
      key,
      label: key in ruleFamilyLabels ? ruleFamilyLabels[key as RuleFamily] : key,
      rows: filtered.filter((row) => grouping === "family"
        ? row.family === key
        : row.family === "activity" ? activityCategoryLabel(row.category) === key : row.family === key),
    })).filter((group) => group.rows.length > 0);
  }, [filtered, grouping]);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border bg-white px-4 py-3">
        <div><h2 className="text-sm font-semibold text-gray-900">Azure Monitor alert rules</h2><p className="text-xs text-gray-500">Metric, Log Analytics, Activity Log, Smart Detector, and Prometheus rules through reviewable Azure changes.</p></div>
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search rules…" className="ml-auto w-52 rounded border px-2 py-1.5 text-xs" />
        <select value={family} onChange={(event) => setFamily(event.target.value as "all" | RuleFamily)} className="rounded border px-2 py-1.5 text-xs"><option value="all">All types</option><option value="metric">Metric</option><option value="log">Log query</option><option value="activity">Activity Log</option><option value="smart">Smart Detector</option><option value="prometheus">Prometheus</option></select>
        <div className="inline-flex items-center gap-1.5 text-xs text-gray-500">
          <span>State:</span>
          <div className="inline-flex overflow-hidden rounded-md border" role="group" aria-label="Filter rules by state">
            {(["all", "enabled", "disabled"] as const).map((value) => <button key={value} type="button" aria-pressed={state === value} onClick={() => setState(value)} className={segmentButton(state === value)}>{value === "all" ? "All" : value === "enabled" ? "Enabled" : "Disabled"}</button>)}
          </div>
        </div>
        <div className="inline-flex items-center gap-1.5 text-xs text-gray-500">
          <span>Group by:</span>
          <div className="inline-flex overflow-hidden rounded-md border" role="group" aria-label="Group alert rules">
            {(["family", "category", "none"] as const).map((value) => <button key={value} type="button" aria-pressed={grouping === value} onClick={() => setGrouping(value)} className={segmentButton(grouping === value)}>{value === "family" ? "Rule type" : value === "category" ? "Activity category" : "None"}</button>)}
          </div>
        </div>
        {caps?.can_manage_rules && !caps.read_only && <div className="flex flex-wrap gap-1"><button onClick={() => onCreate("metric")} className="rounded bg-gray-900 px-2.5 py-1.5 text-xs text-white">+ Metric</button><button onClick={() => onCreate("log")} className="rounded bg-gray-900 px-2.5 py-1.5 text-xs text-white">+ Log</button><button onClick={() => onCreate("activity")} className="rounded bg-gray-900 px-2.5 py-1.5 text-xs text-white">+ Activity</button>{caps.can_manage_advanced_rules && <><button onClick={() => onCreate("smart")} className="rounded bg-indigo-700 px-2.5 py-1.5 text-xs text-white">+ Smart Detector</button><button onClick={() => onCreate("prometheus")} className="rounded bg-indigo-700 px-2.5 py-1.5 text-xs text-white">+ Prometheus</button></>}</div>}
      </div>
      {caps?.can_bulk_manage && selected.length > 0 && <div className="flex flex-wrap items-center gap-2 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2 text-xs"><strong>{selected.length} selected</strong><select value={bulkAction} onChange={(event) => setBulkAction(event.target.value as typeof bulkAction)} className="rounded border px-2 py-1"><option value="enable">Enable</option><option value="disable">Disable</option><option value="add_action_group">Add Action Group</option><option value="delete">Delete</option></select>{bulkAction === "add_action_group" && <select value={bulkGroup} onChange={(event) => setBulkGroup(event.target.value)} className="min-w-96 max-w-full rounded border px-2 py-1"><option value="">Choose Action Group…</option>{bulkActionGroupSubscriptions.map((subscription) => <optgroup key={subscription.subscriptionId || "unknown"} label={subscriptionHeading(subscription.subscriptionName, subscription.subscriptionId)}>{subscription.items.map((group) => <option key={group.id} value={group.id}>{group.name} · RG {group.resource_group || "—"} · {group.enabled ? "Enabled" : "Disabled"} · {actionGroupHealth(group)}</option>)}</optgroup>)}</select>}<button disabled={!!busy || (bulkAction === "add_action_group" && !bulkGroup)} onClick={() => onBulk?.(rows.filter((row) => selected.includes(row.id)), bulkAction, bulkGroup)} className="rounded bg-indigo-700 px-3 py-1 text-white disabled:opacity-50">Create {selected.length} change requests</button><button onClick={() => setSelected([])} className="text-gray-600">Clear</button><span className="ml-auto text-gray-500">Not atomic; each child change is approved, applied, and rolled back independently.</span></div>}
      <div className="overflow-auto rounded-xl border bg-white">
        <table className="w-full min-w-[1100px] text-left text-xs">
          <thead className="sticky top-0 bg-gray-50 text-gray-500"><tr>{caps?.can_bulk_manage && <th className="px-3 py-2"><input aria-label="Select all visible rules" type="checkbox" checked={filtered.length > 0 && filtered.every((row) => selected.includes(row.id))} onChange={(event) => setSelected(event.target.checked ? Array.from(new Set([...selected, ...filtered.map((row) => row.id)])).slice(0, 50) : selected.filter((id) => !filtered.some((row) => row.id === id)))} /></th>}<th className="px-3 py-2">Rule</th><th className="px-3 py-2">Type / state</th><th className="px-3 py-2">Scope</th><th className="px-3 py-2">Evaluation</th><th className="px-3 py-2">Actions</th><th className="px-3 py-2">Manage</th></tr></thead>
          <tbody className="divide-y">{grouped.map((group) => <Fragment key={group.key}>
            {grouping !== "none" && <tr className="bg-gray-50/80"><th colSpan={caps?.can_bulk_manage ? 7 : 6} scope="rowgroup" className="px-3 py-1.5 text-left text-[11px] font-semibold text-gray-700">{group.label}<span className="ml-1.5 font-normal text-gray-400">{group.rows.length}</span></th></tr>}
            {group.rows.map((row) => <tr key={row.id} className="align-top hover:bg-gray-50">
            {caps?.can_bulk_manage && <td className="px-3 py-3"><input aria-label={`Select ${row.name}`} type="checkbox" checked={selected.includes(row.id)} onChange={(event) => setSelected(event.target.checked ? [...selected, row.id].slice(0, 50) : selected.filter((id) => id !== row.id))} /></td>}
            <td className="max-w-sm px-3 py-3"><div className="font-medium text-gray-800">{row.name}</div><div className="mt-0.5 line-clamp-2 text-[10px] text-gray-500">{row.description}</div><div className="text-[10px] text-gray-400">{row.resource_group}</div></td>
            <td className="px-3 py-3"><span className="rounded bg-indigo-50 px-2 py-0.5 capitalize text-indigo-700">{row.family}</span><span className={`ml-1 rounded px-2 py-0.5 ${row.enabled ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-600"}`}>{row.enabled ? "enabled" : "disabled"}</span>{row.family === "activity" && <div className="mt-1 text-[10px] text-gray-500">{activityCategoryLabel(row.category)}</div>}{row.severity != null && <div className="mt-1 text-[10px] text-gray-500">Severity {row.severity}</div>}</td>
            <td className="px-3 py-3"><div>{row.scopes.length} target{row.scopes.length === 1 ? "" : "s"}</div><div className="max-w-[220px] truncate text-[10px] text-gray-400" title={row.scopes.join("\n")}>{row.scopes[0]}</div></td>
            <td className="px-3 py-3 text-gray-600"><div>{row.condition_count} condition{row.condition_count === 1 ? "" : "s"}</div><div className="text-[10px] text-gray-400">{row.evaluation_frequency || "event-driven"}{row.window_size ? ` / ${row.window_size}` : ""}</div></td>
            <td className="px-3 py-3 text-gray-600">{row.action_group_ids.length} Action Group{row.action_group_ids.length === 1 ? "" : "s"}</td>
            <td className="px-3 py-3"><div className="flex flex-wrap gap-1"><a href={`https://portal.azure.com/#@/resource${row.id}/overview`} target="_blank" rel="noreferrer" className="rounded border px-2 py-1 text-[10px] text-brand">Portal ↗</a>{caps?.can_manage_rules && !caps.read_only && (!(["smart", "prometheus"].includes(row.family)) || caps.can_manage_advanced_rules) && <><button disabled={!!busy} onClick={() => onEdit(row)} className="rounded border px-2 py-1 text-[10px] text-indigo-700 disabled:opacity-50">Edit</button><button disabled={!!busy} onClick={() => onClone(row)} className="rounded border px-2 py-1 text-[10px] text-gray-700 disabled:opacity-50">Clone</button><button disabled={!!busy} onClick={() => onToggle(row)} className="rounded border px-2 py-1 text-[10px] text-gray-700 disabled:opacity-50">{row.enabled ? "Disable" : "Enable"}</button></>}{caps?.can_delete && !caps.read_only && <button disabled={!!busy} onClick={() => onDelete(row)} className="rounded border px-2 py-1 text-[10px] text-red-700 disabled:opacity-50">Delete</button>}</div></td>
          </tr>)}
          </Fragment>)}</tbody>
        </table>
        {filtered.length === 0 && <div className="p-10 text-center text-sm text-gray-400">No alert rules match this scope and filter.</div>}
      </div>
    </div>
  );
}

export function AlertRuleEditor({ initial, connectionId, workloadId = "", actionGroups, canPreview, busy, onClose, onSave }: {
  initial: EditableAlertRule;
  connectionId: string;
  workloadId?: string;
  actionGroups: ManagedActionGroup[];
  canPreview: boolean;
  busy: boolean;
  onClose: () => void;
  onSave: (value: EditableAlertRule, reason: string) => Promise<void>;
}) {
  const [value, setValue] = useState(initial);
  const [reason, setReason] = useState(initial.id ? "Update alert rule" : "Create alert rule disabled for review");
  const [signalResource, setSignalResource] = useState(initial.scopes[0] || "");
    const [logWorkspace, setLogWorkspace] = useState(() => initial.scopes.find((scope) => scope.toLowerCase().includes("/providers/microsoft.operationalinsights/workspaces/")) || "");
  const [metrics, setMetrics] = useState<MetricDefinition[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState("");
  const [validation, setValidation] = useState<{ valid: boolean; errors: string[]; warnings: string[] } | null>(null);
  const [noise, setNoise] = useState<NoiseGuardResult | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [suggestions, setSuggestions] = useState<{ action_group_id: string; name: string; confidence: number; reason: string }[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const [error, setError] = useState("");
  const [saveStage, setSaveStage] = useState<"idle" | "validating" | "analyzing" | "submitting">("idle");
  const family = value.family;
  const resolvedScopes = useResolvedAlertScopes(connectionId, value.scopes);
  const changeCondition = (index: number, patch: Partial<Condition>) => setValue((current) => ({ ...current, conditions: current.conditions.map((item, i) => i === index ? { ...item, ...patch } : item) }));
  const activityValue = (field: string) => {
    const item = (value.activity_conditions || []).find((condition) => String(condition.field || "").toLowerCase() === field.toLowerCase());
    return String(item?.equals || "");
  };
  const activityContains = (field: string) => {
    const item = (value.activity_conditions || []).find((condition) => String(condition.field || "").toLowerCase() === field.toLowerCase());
    return Array.isArray(item?.containsAny) ? (item.containsAny as unknown[]).join(",") : "";
  };
  const setActivity = (field: string, equals: string) => setValue((current) => ({ ...current, activity_conditions: [...(current.activity_conditions || []).filter((condition) => String(condition.field || "").toLowerCase() !== field.toLowerCase()), ...(equals ? [{ field, equals }] : [])] }));
  const setActivityContains = (field: string, raw: string) => setValue((current) => ({ ...current, activity_conditions: [...(current.activity_conditions || []).filter((condition) => String(condition.field || "").toLowerCase() !== field.toLowerCase()), ...(raw.trim() ? [{ field, containsAny: raw.split(",").map((item) => item.trim()).filter(Boolean) }] : [])] }));
  const identity = value.identity || {};
  const identityType = String(identity.type || "None");
  const identityId = Object.keys((identity.userAssignedIdentities as Record<string, unknown> | undefined) || {})[0] || "";
  async function loadMetrics() {
    if (!signalResource) return;
    setLoadingCatalog(true); setError("");
    try { setMetrics((await api.metricDefinitions(connectionId, signalResource)).metrics); }
    catch (cause) { setError(formatError(cause)); }
    finally { setLoadingCatalog(false); }
  }
  function chooseMetric(name: string) {
    const metric = metrics.find((item) => item.name === name);
    if (!metric) return;
    changeCondition(0, { metric_name: metric.name, metric_namespace: metric.namespace, aggregation: metric.primary_aggregation || "Average" });
  }
  async function runPreview() {
    setPreviewing(true); setError(""); setPreview("");
    try {
      if (family === "metric") {
        const condition = value.conditions[0];
        const result = await api.previewMetricSignal({ connection_id: connectionId, resource_id: signalResource || value.scopes[0], metric_name: condition.metric_name || "", aggregation: condition.aggregation, interval: value.evaluation_frequency || "PT5M" });
        setPreview(`${result.count} points · min ${result.minimum ?? "—"} · avg ${result.average?.toFixed(2) ?? "—"} · max ${result.maximum ?? "—"}`);
      } else if (family === "log") {
        const result = await api.previewLogAlertQuery({ connection_id: connectionId, workspace_id: logWorkspace, query: value.conditions[0]?.query || "", timespan: value.window_size || "PT1H" });
        setPreview(`${result.row_count} rows${result.truncated ? " · showing first 100" : ""}\n${JSON.stringify(result.rows.slice(0, 5), null, 2)}`);
      }
    } catch (cause) { setError(formatError(cause)); }
    finally { setPreviewing(false); }
  }
  async function validateAndSave() {
    if (saveStage !== "idle" || busy) return;
    setError(""); setValidation(null); setNoise(null); setSaveStage("validating");
    try {
      const result = await api.validateManagedAlertRule(family, value, !value.id);
      setValidation({ valid: result.valid, errors: result.errors, warnings: result.cost.warnings });
      if (!result.valid) return;
      setSaveStage("analyzing");
      const guard = await api.alertRuleNoiseGuard({ connection_id: connectionId, workload_id: workloadId || undefined, family, desired: value });
      setNoise(guard);
      if (guard.overlap && !window.confirm(`${guard.count} possible overlap(s) found. Continue creating a reviewed change request?`)) return;
      setSaveStage("submitting");
      await onSave(value, reason);
    } catch (cause) { setError(formatError(cause)); }
    finally { setSaveStage("idle"); }
  }
  async function suggestRouting() {
    const subjectId = value.scopes[0] || workloadId;
    if (!subjectId) return;
    setSuggesting(true); setError("");
    try {
      const result = await api.actionGroupSuggestions({ connection_id: connectionId, workload_id: workloadId || undefined, subject_kind: value.scopes[0] ? "resource" : "workload", subject_id: subjectId });
      setSuggestions(result.suggestions);
    } catch (cause) { setError(formatError(cause)); }
    finally { setSuggesting(false); }
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={`${initial.id ? "Edit" : "Create"} ${family} alert rule`}>
    <div className="max-h-[94vh] w-full max-w-5xl overflow-auto rounded-xl bg-white shadow-xl">
      <div className="sticky top-0 z-10 flex items-center border-b bg-white px-5 py-4"><div><h2 className="font-semibold capitalize text-gray-900">{initial.id ? "Edit" : "Create"} {family === "log" ? "log-query" : family} alert rule</h2><p className="text-xs text-gray-500">New rules default to disabled. Saving creates an encrypted change request; Azure changes only after approval and Apply.</p></div><button onClick={onClose} className="ml-auto rounded px-2 py-1 text-gray-500">✕</button></div>
      <div className="space-y-5 p-5 text-xs">
        {saveStage !== "idle" && <div role="status" aria-live="polite" className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-sky-900">
          <div className="flex items-center gap-2"><span className="h-4 w-4 animate-spin rounded-full border-2 border-sky-200 border-t-sky-600" /><span className="font-semibold">{saveStage === "validating" ? "Validating rule configuration…" : saveStage === "analyzing" ? "Checking firing history, routing, and possible overlaps…" : "Creating the approval-gated change request…"}</span></div>
          <div role="progressbar" aria-label="Create alert rule change progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={saveStage === "validating" ? 25 : saveStage === "analyzing" ? 65 : 90} className="mt-2 h-1.5 overflow-hidden rounded-full bg-sky-100"><div className="h-full rounded-full bg-sky-500 transition-all duration-300" style={{ width: saveStage === "validating" ? "25%" : saveStage === "analyzing" ? "65%" : "90%" }} /></div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-[10px]">
            <span className={saveStage !== "validating" ? "font-medium text-emerald-700" : "font-medium text-sky-700"}>{saveStage !== "validating" ? "✓ " : "● "}Validate</span>
            <span className={saveStage === "submitting" ? "font-medium text-emerald-700" : saveStage === "analyzing" ? "font-medium text-sky-700" : "text-sky-400"}>{saveStage === "submitting" ? "✓ " : saveStage === "analyzing" ? "● " : "○ "}Noise guard</span>
            <span className={saveStage === "submitting" ? "font-medium text-sky-700" : "text-sky-400"}>{saveStage === "submitting" ? "● " : "○ "}Change request</span>
          </div>
        </div>}
        {error && <div className="rounded border border-red-200 bg-red-50 p-3 text-red-700">{error}</div>}
        {validation && <div className={`rounded border p-3 ${validation.valid ? "border-green-200 bg-green-50 text-green-800" : "border-red-200 bg-red-50 text-red-700"}`}>{validation.errors.map((item) => <div key={item}>• {item}</div>)}{validation.warnings.map((item) => <div key={item} className="text-amber-700">⚠ {item}</div>)}{validation.valid && !validation.warnings.length && "Validation passed."}</div>}
        {noise && (noise.overlap || noise.layered_count > 0) && <div className="rounded border border-amber-300 bg-amber-50 p-3 text-amber-800"><div className="font-semibold">Noise guard: {noise.count} actionable overlap{noise.count === 1 ? "" : "s"}{noise.layered_count ? ` · ${noise.layered_count} intentional escalation layer(s)` : ""}</div><div className="text-[10px]">Projected duplicate receiver deliveries from current 30-day firing history: {noise.projected_duplicate_notifications_30d}</div>{noise.findings.slice(0, 8).map((item) => <div key={item.rule_id} className="mt-2 rounded border border-amber-200 bg-white/60 p-2"><strong>{item.rule_name}</strong> — {item.type} · dimensions {item.dimension_overlap}{item.threshold_delta_pct != null ? ` · threshold delta ${item.threshold_delta_pct}%` : ""}<div className="text-[10px]">{item.explanation} {item.historical_firings_30d} fires / 30d · {item.shared_receiver_count} shared receiver(s) · projected duplicates {item.projected_duplicate_notifications_30d}</div></div>)}</div>}
        <section className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-4"><label>Name<input disabled={!!initial.id} value={value.name} onChange={(event) => setValue({ ...value, name: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5 disabled:bg-gray-100" /></label><AzurePlacementFields connectionId={connectionId} subscriptionId={value.subscription_id} resourceGroup={value.resource_group} location={value.location} disabled={!!initial.id} globalLocation={family === "metric" || family === "activity" || family === "smart"} onChange={(patch) => setValue({ ...value, ...patch })} />{family !== "activity" && family !== "prometheus" && <label>Severity<select value={value.severity ?? 3} onChange={(event) => setValue({ ...value, severity: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5">{[0, 1, 2, 3, 4].map((item) => <option key={item} value={item}>Sev {item}</option>)}</select></label>}<label className="flex items-end gap-2 pb-1"><input type="checkbox" checked={value.enabled} onChange={(event) => setValue({ ...value, enabled: event.target.checked })} /> Enabled after apply</label><label className="sm:col-span-2 lg:col-span-4">Description<textarea value={value.description} onChange={(event) => setValue({ ...value, description: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label></section>
        {family !== "log" && <section className="rounded-lg border p-3"><div className="mb-2 flex items-center"><div><h3 className="font-semibold text-gray-800">Scopes</h3><p className="text-[10px] text-gray-500">Choose Azure targets; resolved names and resource types are shown below.</p></div><button onClick={() => setShowPicker(true)} className="ml-auto rounded border px-2 py-1 text-indigo-700">Select targets…</button></div><SelectedScopesTable resources={resolvedScopes.resources} loading={resolvedScopes.isFetching} onRemove={(id) => { const scopes = value.scopes.filter((scope) => scope !== id); setValue({ ...value, scopes }); if (signalResource === id) setSignalResource(scopes[0] || ""); }} /></section>}
        {family === "metric" && <section className="space-y-3 rounded-lg border p-3"><div className="flex flex-wrap items-end gap-2"><label className="min-w-0 flex-1">Signal resource<input value={signalResource} onChange={(event) => setSignalResource(event.target.value)} placeholder="Resource ID used to discover metrics" className="mt-1 w-full rounded border px-2 py-1.5" /></label><button onClick={() => void loadMetrics()} disabled={loadingCatalog || !signalResource} className="rounded border px-3 py-1.5 text-indigo-700 disabled:opacity-50">{loadingCatalog ? "Loading…" : "Discover metrics"}</button>{metrics.length > 0 && <select value={value.conditions[0]?.metric_name || ""} onChange={(event) => chooseMetric(event.target.value)} className="max-w-xs rounded border px-2 py-1.5"><option value="">Select metric…</option>{metrics.map((metric) => <option key={`${metric.namespace}:${metric.name}`} value={metric.name}>{metric.display_name} ({metric.unit})</option>)}</select>}</div>{value.conditions.map((condition, index) => <MetricConditionEditor key={index} condition={condition} index={index} metric={metrics.find((item) => item.name === condition.metric_name)} onChange={(patch) => changeCondition(index, patch)} onRemove={() => setValue({ ...value, conditions: value.conditions.filter((_item, i) => i !== index) })} />)}<div className="flex gap-2"><button onClick={() => setValue({ ...value, conditions: [...value.conditions, { ...blankMetricCondition(), name: `condition-${value.conditions.length + 1}` }] })} className="rounded border px-2 py-1 text-indigo-700">+ Condition</button>{canPreview && <button onClick={() => void runPreview()} disabled={previewing || !value.conditions[0]?.metric_name} className="rounded border px-2 py-1 text-amber-700 disabled:opacity-50">{previewing ? "Previewing…" : "Preview last 6h"}</button>}</div>{preview && <pre className="overflow-auto rounded bg-gray-950 p-3 text-[10px] text-gray-100">{preview}</pre>}<div className="grid gap-2 sm:grid-cols-2"><label>Target resource type<input value={value.target_resource_type} onChange={(event) => setValue({ ...value, target_resource_type: event.target.value })} placeholder="Required for multi-resource rules" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Target resource region<input value={value.target_resource_region} onChange={(event) => setValue({ ...value, target_resource_region: event.target.value })} placeholder="Required for multi-resource rules" className="mt-1 w-full rounded border px-2 py-1.5" /></label></div></section>}
        {family === "log" && <section className="space-y-3 rounded-lg border p-3"><h3 className="font-semibold text-gray-800">KQL condition</h3><AzureResourceDropdown label="Log Analytics workspace" types={["microsoft.operationalinsights/workspaces"]} connectionId={connectionId} subscriptionId={value.subscription_id} resourceGroup={value.resource_group} value={logWorkspace} onChange={(workspace, resource) => { setLogWorkspace(workspace); setValue({ ...value, scopes: resource ? [resource.id] : [], location: resource?.location || "" }); }} /><textarea value={value.conditions[0]?.query || ""} onChange={(event) => changeCondition(0, { query: event.target.value })} rows={8} className="w-full rounded border bg-gray-950 px-3 py-2 font-mono text-gray-100" placeholder="ContainerAppConsoleLogs_CL | where TimeGenerated > ago(15m) | summarize count()" /><div className="grid gap-2 sm:grid-cols-4"><ConditionLogic condition={value.conditions[0]} onChange={(patch) => changeCondition(0, patch)} /><label>Resource ID column<input value={value.conditions[0]?.resource_id_column || ""} onChange={(event) => changeCondition(0, { resource_id_column: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label></div><DimensionsEditor dimensions={value.conditions[0]?.dimensions || []} onChange={(dimensions) => changeCondition(0, { dimensions })} /><div className="grid gap-2 sm:grid-cols-2"><label>Rule identity<select value={identityType} onChange={(event) => setValue({ ...value, identity: event.target.value === "None" ? {} : { type: event.target.value } })} className="mt-1 w-full rounded border px-2 py-1.5"><option>None</option><option>SystemAssigned</option><option>UserAssigned</option></select></label>{identityType === "UserAssigned" && <label>User-assigned identity resource ID<input value={identityId} onChange={(event) => setValue({ ...value, identity: { type: "UserAssigned", userAssignedIdentities: event.target.value ? { [event.target.value]: {} } : {} } })} className="mt-1 w-full rounded border px-2 py-1.5" /></label>}</div>{canPreview && <button onClick={() => void runPreview()} disabled={previewing || !logWorkspace || !value.conditions[0]?.query} className="rounded border px-2 py-1 text-amber-700 disabled:opacity-50">{previewing ? "Running query…" : "Validate and preview query"}</button>}{preview && <pre className="max-h-60 overflow-auto rounded bg-gray-950 p-3 text-[10px] text-gray-100">{preview}</pre>}<div className="rounded bg-amber-50 px-3 py-2 text-amber-800">Log alert cost depends on evaluation frequency and data scanned. Keep KQL bounded and use the longest acceptable frequency.</div></section>}
        {family === "activity" && <section className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-3"><label>Category<select value={activityValue("category")} onChange={(event) => setActivity("category", event.target.value)} className="mt-1 w-full rounded border px-2 py-1.5"><option>Administrative</option><option>ServiceHealth</option><option>ResourceHealth</option><option>Security</option><option>Recommendation</option></select></label><label>Operation name<input value={activityValue("operationName")} onChange={(event) => setActivity("operationName", event.target.value)} placeholder="Microsoft.Compute/virtualMachines/write" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Level<input value={activityValue("level")} onChange={(event) => setActivity("level", event.target.value)} placeholder="Error" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Status<input value={activityValue("status")} onChange={(event) => setActivity("status", event.target.value)} placeholder="Failed" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Resource type<input value={activityValue("resourceType")} onChange={(event) => setActivity("resourceType", event.target.value)} placeholder="Microsoft.Compute/virtualMachines" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Resource group<input value={activityValue("resourceGroup")} onChange={(event) => setActivity("resourceGroup", event.target.value)} className="mt-1 w-full rounded border px-2 py-1.5" /></label>{activityValue("category") === "ServiceHealth" && <><label>Incident types<input value={activityContains("properties.incidentType")} onChange={(event) => setActivityContains("properties.incidentType", event.target.value)} placeholder="Incident,Maintenance,ActionRequired" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Impacted services<input value={activityContains("properties.impactedServices[*].ServiceName")} onChange={(event) => setActivityContains("properties.impactedServices[*].ServiceName", event.target.value)} placeholder="Virtual Machines,Storage" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Impacted regions<input value={activityContains("properties.impactedServices[*].ImpactedRegions[*].RegionName")} onChange={(event) => setActivityContains("properties.impactedServices[*].ImpactedRegions[*].RegionName", event.target.value)} placeholder="South Central US,Central US" className="mt-1 w-full rounded border px-2 py-1.5" /></label></>}{activityValue("category") === "ResourceHealth" && <><label>Current health status<input value={activityValue("properties.currentHealthStatus")} onChange={(event) => setActivity("properties.currentHealthStatus", event.target.value)} placeholder="Unavailable" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Previous health status<input value={activityValue("properties.previousHealthStatus")} onChange={(event) => setActivity("properties.previousHealthStatus", event.target.value)} placeholder="Available" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Cause<input value={activityValue("properties.cause")} onChange={(event) => setActivity("properties.cause", event.target.value)} placeholder="PlatformInitiated" className="mt-1 w-full rounded border px-2 py-1.5" /></label></>}</section>}
        {family === "smart" && <section className="grid gap-3 rounded-lg border p-3 sm:grid-cols-3"><label>Detector ID<input value={value.detector_id || ""} onChange={(event) => setValue({ ...value, detector_id: event.target.value })} placeholder="FailureAnomaliesDetector" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Frequency<select value={value.frequency || "PT5M"} onChange={(event) => setValue({ ...value, frequency: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT60M"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Throttle duration<select value={value.throttling_duration || "PT0M"} onChange={(event) => setValue({ ...value, throttling_duration: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["PT0M", "PT5M", "PT15M", "PT30M", "PT60M"].map((item) => <option key={item}>{item}</option>)}</select></label><label className="sm:col-span-3">Detector parameters (JSON)<textarea rows={4} defaultValue={JSON.stringify(value.detector_parameters || {}, null, 2)} onBlur={(event) => { try { setValue({ ...value, detector_parameters: JSON.parse(event.target.value) }); } catch { setError("Detector parameters must be valid JSON."); } }} className="mt-1 w-full rounded border bg-gray-950 px-2 py-1.5 font-mono text-gray-100" /></label></section>}
        {family === "prometheus" && <PrometheusRulesEditor value={value} setValue={setValue} actionGroups={actionGroups} ruleSubscriptionId={value.subscription_id} />}
        {(family === "metric" || family === "log") && <section className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2"><label>Evaluate every<select value={value.evaluation_frequency} onChange={(event) => setValue({ ...value, evaluation_frequency: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Lookback window<select value={value.window_size} onChange={(event) => setValue({ ...value, window_size: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT1H", "PT6H", "PT12H", "P1D", "P2D"].map((item) => <option key={item}>{item}</option>)}</select></label></section>}
        {family !== "prometheus" && <section className="rounded-lg border p-3"><div className="flex items-center"><div><h3 className="font-semibold text-gray-800">Action Groups</h3><p className="text-[10px] text-gray-500">Grouped by subscription. Ownership routing compares resolved owners server-side; receiver destinations are shown in full.</p></div><button disabled={suggesting || (!value.scopes[0] && !workloadId)} onClick={() => void suggestRouting()} className="ml-auto rounded border px-2 py-1 text-indigo-700 disabled:opacity-50">{suggesting ? "Matching…" : "Suggest from ownership"}</button></div>{suggestions.length > 0 && <div className="mt-2 rounded border border-indigo-200 bg-indigo-50 p-2"><div className="mb-1 font-medium text-indigo-800">Suggested routing</div>{suggestions.slice(0, 5).map((item) => <div key={item.action_group_id} className="flex items-center gap-2 py-1"><span>{item.name}</span><span className="text-[10px] text-indigo-600">{Math.round(item.confidence * 100)}% · {item.reason}</span><button disabled={value.action_group_ids.includes(item.action_group_id)} onClick={() => setValue({ ...value, action_group_ids: [...value.action_group_ids, item.action_group_id] })} className="ml-auto rounded border border-indigo-300 px-2 py-0.5 text-[10px] text-indigo-700 disabled:opacity-40">Add</button></div>)}</div>}<ActionGroupChoices actionGroups={actionGroups} selectedIds={value.action_group_ids} ruleSubscriptionId={value.subscription_id} onToggle={(group, selected) => setValue({ ...value, action_group_ids: selected ? [...value.action_group_ids, group.id] : value.action_group_ids.filter((id) => id !== group.id) })} />{actionGroups.length === 0 && <p className="mt-2 text-gray-400">No Action Groups in the visible subscriptions. Create one from the Action groups tab first.</p>}</section>}
        <label className="block">Reason <span className="text-red-600">*</span><textarea required aria-required="true" value={reason} onChange={(event) => setReason(event.target.value)} className="mt-1 w-full rounded border px-2 py-1.5" /></label>
      </div>
      <div className="sticky bottom-0 flex items-center justify-end gap-2 border-t bg-white px-5 py-3">{saveStage !== "idle" && <div className="mr-auto min-w-48 max-w-sm flex-1"><div className="mb-1 flex justify-between text-[10px] font-medium text-sky-700"><span>{saveStage === "validating" ? "Phase 1 of 3 · Validate" : saveStage === "analyzing" ? "Phase 2 of 3 · Noise guard" : "Phase 3 of 3 · Change request"}</span><span>{saveStage === "validating" ? "25%" : saveStage === "analyzing" ? "65%" : "90%"}</span></div><div className="h-1 overflow-hidden rounded-full bg-sky-100"><div className="h-full rounded-full bg-sky-500 transition-all duration-300" style={{ width: saveStage === "validating" ? "25%" : saveStage === "analyzing" ? "65%" : "90%" }} /></div></div>}<button disabled={busy || saveStage !== "idle"} onClick={onClose} className="rounded border px-3 py-1.5 text-xs disabled:opacity-50">Cancel</button><button disabled={busy || saveStage !== "idle" || !reason.trim()} onClick={() => void validateAndSave()} className="inline-flex min-w-48 items-center justify-center gap-2 rounded bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-70">{(busy || saveStage !== "idle") && <span className="h-3 w-3 animate-spin rounded-full border border-gray-400 border-t-white" />}{saveStage === "validating" ? "Validating…" : saveStage === "analyzing" ? "Checking overlaps…" : saveStage === "submitting" || busy ? "Creating change…" : "Validate & create change"}</button></div>
    </div>
    {showPicker && <ResourcePicker connectionId={connectionId} initialNodes={resolvedScopesToWorkloadNodes(resolvedScopes.resources)} onApply={(nodes) => { const first = nodes[0]; setValue({ ...value, scopes: nodes.map((node) => node.id), subscription_id: first?.subscription_id || value.subscription_id, resource_group: first?.resource_group || value.resource_group, ...(nodes.length === 1 && first?.resource_type ? { target_resource_type: first.resource_type, target_resource_region: first.location || "" } : {}) }); setSignalResource(first?.id || ""); setShowPicker(false); }} onCancel={() => setShowPicker(false)} />}
  </div>;
}

function PrometheusRulesEditor({ value, setValue, actionGroups, ruleSubscriptionId }: { value: EditableAlertRule; setValue: Dispatch<SetStateAction<EditableAlertRule>>; actionGroups: ManagedActionGroup[]; ruleSubscriptionId: string }) {
  const rows = value.prometheus_rules || [];
  const update = (index: number, patch: Partial<(typeof rows)[number]>) => setValue((current) => ({ ...current, prometheus_rules: (current.prometheus_rules || []).map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) }));
  return <section className="space-y-3 rounded-lg border p-3">
    <div className="grid gap-3 sm:grid-cols-2"><label>Evaluation interval<select value={value.interval || "PT1M"} onChange={(event) => setValue({ ...value, interval: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["PT1M", "PT2M", "PT5M", "PT10M", "PT15M"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Cluster name (optional)<input value={value.cluster_name || ""} onChange={(event) => setValue({ ...value, cluster_name: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label></div>
    {rows.map((rule, index) => {
      const selected = (rule.actions || []).map((item) => typeof item === "string" ? item : item.actionGroupId);
      return <div key={index} className="space-y-2 rounded border bg-gray-50 p-3"><div className="flex items-center"><h4 className="font-medium">Prometheus rule {index + 1}</h4>{rows.length > 1 && <button onClick={() => setValue({ ...value, prometheus_rules: rows.filter((_item, itemIndex) => itemIndex !== index) })} className="ml-auto text-red-600">Remove</button>}</div><div className="grid gap-2 sm:grid-cols-4"><label>Alert name<input value={rule.alert || ""} onChange={(event) => update(index, { alert: event.target.value, record: "" })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Recording name<input value={rule.record || ""} onChange={(event) => update(index, { record: event.target.value, alert: "" })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>For<input value={rule.for || ""} onChange={(event) => update(index, { for: event.target.value })} placeholder="PT5M" className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Severity<select disabled={!rule.alert} value={rule.severity ?? 3} onChange={(event) => update(index, { severity: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5">{[0, 1, 2, 3, 4].map((item) => <option key={item} value={item}>Sev {item}</option>)}</select></label></div><label className="block">PromQL expression<textarea rows={5} value={rule.expression || ""} onChange={(event) => update(index, { expression: event.target.value })} className="mt-1 w-full rounded border bg-gray-950 px-3 py-2 font-mono text-gray-100" placeholder="sum(rate(http_requests_total[5m])) by (job) > 10" /></label><div className="grid gap-2 sm:grid-cols-2"><label>Labels (JSON)<textarea rows={3} defaultValue={JSON.stringify(rule.labels || {}, null, 2)} onBlur={(event) => { try { update(index, { labels: JSON.parse(event.target.value) }); } catch { /* validation will retain prior value */ } }} className="mt-1 w-full rounded border px-2 py-1.5 font-mono" /></label><label>Annotations (JSON)<textarea rows={3} defaultValue={JSON.stringify(rule.annotations || {}, null, 2)} onBlur={(event) => { try { update(index, { annotations: JSON.parse(event.target.value) }); } catch { /* validation will retain prior value */ } }} className="mt-1 w-full rounded border px-2 py-1.5 font-mono" /></label></div>{rule.alert && <div><div className="font-medium text-gray-600">Action Groups</div><ActionGroupChoices actionGroups={actionGroups} selectedIds={selected} ruleSubscriptionId={ruleSubscriptionId} onToggle={(group, isSelected) => update(index, { actions: isSelected ? [...(rule.actions || []), { actionGroupId: group.id }] : (rule.actions || []).filter((item) => (typeof item === "string" ? item : item.actionGroupId) !== group.id) })} /></div>}</div>;
    })}
    <button onClick={() => setValue({ ...value, prometheus_rules: [...rows, { alert: "", expression: "", enabled: true, for: "PT5M", severity: 3, actions: [] }] })} className="rounded border px-2 py-1 text-indigo-700">+ Prometheus rule</button>
  </section>;
}

function ConditionLogic({ condition, onChange }: { condition: Condition; onChange: (patch: Partial<Condition>) => void }) {
  return <><label>Aggregation<select value={condition.aggregation} onChange={(event) => onChange({ aggregation: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["Count", "Average", "Total", "Minimum", "Maximum"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Operator<select value={condition.operator} onChange={(event) => onChange({ operator: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{["GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual", "Equals"].map((item) => <option key={item}>{item}</option>)}</select></label><label>Threshold<input type="number" value={condition.threshold ?? 0} onChange={(event) => onChange({ threshold: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5" /></label></>;
}

function MetricConditionEditor({ condition, index, metric, onChange, onRemove }: { condition: Condition; index: number; metric?: MetricDefinition; onChange: (patch: Partial<Condition>) => void; onRemove: () => void }) {
  return <div className="space-y-2 rounded border bg-gray-50 p-3"><div className="flex items-center"><h4 className="font-medium text-gray-800">Condition {index + 1}</h4>{index > 0 && <button onClick={onRemove} className="ml-auto text-red-600">Remove</button>}</div><div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6"><label>Metric<input value={condition.metric_name || ""} onChange={(event) => onChange({ metric_name: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Namespace<input value={condition.metric_namespace || ""} onChange={(event) => onChange({ metric_namespace: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Threshold type<select value={condition.threshold_type || "static"} onChange={(event) => onChange({ threshold_type: event.target.value as "static" | "dynamic", operator: event.target.value === "dynamic" ? "GreaterOrLessThan" : "GreaterThan" })} className="mt-1 w-full rounded border px-2 py-1.5"><option value="static">Static</option><option value="dynamic">Dynamic</option></select></label><label>Aggregation<select value={condition.aggregation} onChange={(event) => onChange({ aggregation: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{(metric?.supported_aggregations.length ? metric.supported_aggregations : ["Average", "Total", "Minimum", "Maximum", "Count"]).map((item) => <option key={item}>{item}</option>)}</select></label><label>Operator<select value={condition.operator} onChange={(event) => onChange({ operator: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5">{(condition.threshold_type === "dynamic" ? ["GreaterOrLessThan", "GreaterThan", "LessThan"] : ["GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual", "Equals"]).map((item) => <option key={item}>{item}</option>)}</select></label>{condition.threshold_type === "dynamic" ? <label>Sensitivity<select value={condition.sensitivity || "Medium"} onChange={(event) => onChange({ sensitivity: event.target.value })} className="mt-1 w-full rounded border px-2 py-1.5"><option>High</option><option>Medium</option><option>Low</option></select></label> : <label>Threshold<input type="number" value={condition.threshold ?? 0} onChange={(event) => onChange({ threshold: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5" /></label>}</div>{condition.threshold_type === "dynamic" && <div className="grid gap-2 sm:grid-cols-3"><label>Minimum failures<input type="number" min={1} value={condition.min_failing_periods} onChange={(event) => onChange({ min_failing_periods: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5" /></label><label>Evaluation periods<input type="number" min={1} value={condition.evaluation_periods} onChange={(event) => onChange({ evaluation_periods: Number(event.target.value) })} className="mt-1 w-full rounded border px-2 py-1.5" /></label></div>}{metric?.dimensions.length ? <div><span className="text-gray-500">Available dimensions: </span>{metric.dimensions.map((item) => item.display_name || item.name).join(", ")}</div> : null}<DimensionsEditor dimensions={condition.dimensions} onChange={(dimensions) => onChange({ dimensions })} /></div>;
}

function DimensionsEditor({ dimensions, onChange }: { dimensions: Condition["dimensions"]; onChange: (value: Condition["dimensions"]) => void }) {
  return <div className="space-y-1"><div className="flex items-center"><span className="font-medium text-gray-600">Dimension filters</span><button onClick={() => onChange([...dimensions, { name: "", operator: "Include", values: [""] }])} className="ml-auto rounded border px-2 py-0.5 text-[10px] text-indigo-700">+ Dimension</button></div>{dimensions.map((dimension, index) => <div key={index} className="grid gap-1 sm:grid-cols-[1fr_120px_2fr_auto]"><input aria-label="Dimension name" value={dimension.name} onChange={(event) => onChange(dimensions.map((item, i) => i === index ? { ...item, name: event.target.value } : item))} placeholder="Dimension name" className="rounded border px-2 py-1" /><select value={dimension.operator} onChange={(event) => onChange(dimensions.map((item, i) => i === index ? { ...item, operator: event.target.value as "Include" | "Exclude" } : item))} className="rounded border px-2 py-1"><option>Include</option><option>Exclude</option></select><input aria-label="Dimension values" value={dimension.values.join(",")} onChange={(event) => onChange(dimensions.map((item, i) => i === index ? { ...item, values: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) } : item))} placeholder="Comma-separated values" className="rounded border px-2 py-1" /><button onClick={() => onChange(dimensions.filter((_item, i) => i !== index))} className="text-red-600">Remove</button></div>)}</div>;
}
