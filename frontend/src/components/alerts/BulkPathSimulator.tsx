import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { api, type BulkNotificationSimulation, type ManagedAlertRule, type Workload } from "../../api";
import { formatError } from "../../utils/format";
import { SankeyExplorer, type FlowLink, type FlowNode } from "../shared/SankeyExplorer";

type ScopeParams = { connection_id?: string; workload_id?: string; subscription_id?: string; management_group_id?: string };
type ScopeKind = "workload" | "subscription" | "management_group";
type DisplayMode = "all" | "workloads" | "shared" | "unmapped" | "alerted" | "no-alert" | "gaps" | "healthy";
type Density = "auto" | "detailed" | "compact" | "summary";
type ResourceRecord = { id: string; name: string; type: string; resourceGroup: string; subscriptionId: string; subscriptionName: string; workloadIds: string[]; accessible: boolean };
type Catalog = { workloads: Workload[]; subscriptions: { id: string; name: string }[] };
type SimulationRoute = BulkNotificationSimulation["routes"][number];

const FAMILY_OPTIONS: ManagedAlertRule["family"][] = ["metric", "log", "activity", "smart", "prometheus"];
const NODE_COLORS: Record<string, string> = { scope: "#0f172a", subscription: "#2563eb", workload: "#7c3aed", bucket: "#64748b", resource: "#0891b2", alert: "#4f46e5", action_group: "#16a34a", receiver: "#d97706", outcome: "#64748b" };
// Alert families map onto the ARM types the Azure icon set is keyed by.
const ALERT_ICON_TYPES: Record<string, string> = {
  metric: "microsoft.insights/metricalerts",
  log: "microsoft.insights/scheduledqueryrules",
  activity: "microsoft.insights/activitylogalerts",
  smart: "microsoft.alertsmanagement/smartdetectoralertrules",
  prometheus: "microsoft.alertsmanagement/prometheusrulegroups",
};
const ICON_KINDS = new Set(["resource", "alert", "action_group"]);
const LABEL_RIGHT_KINDS = new Set(["scope", "workload", "resource", "alert", "action_group"]);

function normalizeId(value: string): string { return String(value || "").toLowerCase().replace(/\/$/, ""); }
function subscriptionFromId(value: string): string { return value.split("/").filter(Boolean)[1] || ""; }
function scopeContains(left: string, right: string): boolean {
  const a = normalizeId(left); const b = normalizeId(right);
  return !!a && !!b && (a === b || a.startsWith(b + "/") || b.startsWith(a + "/"));
}
function scopeKindOf(params: ScopeParams): ScopeKind { return params.workload_id ? "workload" : params.management_group_id ? "management_group" : "subscription"; }
function effectiveDensity(requested: Density, count: number): Exclude<Density, "auto"> {
  if (requested !== "auto") return requested;
  return count <= 30 ? "detailed" : count <= 250 ? "compact" : "summary";
}
function displayLabel(value: DisplayMode): string {
  return ({ all: "All", workloads: "Workloads", shared: "Shared", unmapped: "Unmapped", alerted: "Alerted", "no-alert": "No alert", gaps: "Flow gaps", healthy: "Healthy" } as Record<DisplayMode, string>)[value];
}

function armTypeFromId(resourceId: string): string {
  const parts = resourceId.split("/").filter(Boolean);
  const providerIndex = parts.findIndex((part) => part.toLowerCase() === "providers");
  if (providerIndex < 0 || providerIndex + 2 >= parts.length) return "";
  const tail = parts.slice(providerIndex + 1);
  return [tail[0], ...tail.slice(1).filter((_part, index) => index % 2 === 0)].join("/").toLowerCase();
}

function Kpi({ label, value, tone = "text-gray-900", active = false, onClick }: { label: string; value: number; tone?: string; active?: boolean; onClick?: () => void }) {
  const body = <><div className={`text-base font-semibold leading-4 tabular-nums ${tone}`}>{value}</div><div className="whitespace-nowrap text-[8px] font-medium uppercase leading-3 tracking-wide text-gray-400">{label}</div></>;
  return onClick ? <button type="button" onClick={onClick} aria-pressed={active} className={`h-8 w-max min-w-16 flex-none rounded-lg border px-2 py-px text-left transition ${active ? "border-blue-400 bg-blue-50 ring-1 ring-blue-200" : "bg-white hover:border-blue-300"}`} title={`Filter by ${label}`}>{body}</button> : <div className="h-8 w-max min-w-16 flex-none rounded-lg border bg-white px-2 py-px" title={label}>{body}</div>;
}

function MultiSelectFilter({ label, values, selected, onChange }: { label: string; values: { id: string; name: string; count: number }[]; selected: string[]; onChange: (values: string[]) => void }) {
  const [search, setSearch] = useState("");
  const detailsRef = useRef<HTMLDetailsElement | null>(null);
  const visible = values.filter((value) => !search || (value.name + " " + value.id).toLowerCase().includes(search.toLowerCase()));
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id]);
  useEffect(() => {
    const closeOnOutsideInteraction = (event: PointerEvent) => {
      const details = detailsRef.current;
      if (details?.open && event.target instanceof Node && !details.contains(event.target)) details.open = false;
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !detailsRef.current?.open) return;
      detailsRef.current.open = false;
      detailsRef.current.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutsideInteraction);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideInteraction);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);
  return <details ref={detailsRef} className="relative"><summary className="list-none cursor-pointer rounded border bg-white px-2 py-1 text-xs">{label}: {selected.length ? selected.length + "/" + values.length : "All (" + values.length + ")"} ▾</summary><div className="absolute right-0 z-40 mt-1 w-72 rounded-lg border bg-white p-2 shadow-xl"><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={"Search " + label.toLowerCase() + "…"} className="mb-2 w-full rounded border px-2 py-1 text-xs" /><div className="mb-1 flex justify-between text-[10px]"><button type="button" onClick={() => onChange(values.map((value) => value.id))} className="text-blue-600">Select all</button><button type="button" onClick={() => onChange([])} className="text-gray-500">Clear</button></div><div className="max-h-56 space-y-0.5 overflow-auto">{visible.map((value) => <label key={value.id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs hover:bg-gray-50"><input type="checkbox" checked={selected.includes(value.id)} onChange={() => toggle(value.id)} /><span className="min-w-0 flex-1 truncate" title={value.id}>{value.name}</span><span className="tabular-nums text-gray-400">{value.count}</span></label>)}</div>{!visible.length && <div className="py-3 text-center text-xs text-gray-400">No matches</div>}</div></details>;
}

function download(text: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click(); URL.revokeObjectURL(url);
}

function csv(result: BulkNotificationSimulation): string {
  const columns = ["resource_ids", "rule_name", "family", "severity", "rule_enabled", "action_group_name", "receiver_type", "receiver_name", "receiver_destination", "payload_schema", "outcome", "issues"];
  const quote = (value: unknown) => {
    const raw = String(value ?? "");
    const stripped = raw.trimStart();
    const safe = stripped && "=+-@".includes(stripped[0]) ? `'${raw}` : raw;
    return `"${safe.replaceAll('"', '""')}"`;
  };
  return [columns.join(","), ...result.routes.map((row) => columns.map((key) => quote(key === "resource_ids" ? row.resource_ids.join(" | ") : key === "issues" ? row.issues.join(" | ") : key === "receiver_destination" ? row.receiver_destination || row.receiver_masked : row[key as keyof typeof row])).join(","))].join("\n");
}

function elapsedLabel(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function BulkPathSimulator({ params }: { params: ScopeParams }) {
  const [result, setResult] = useState<BulkNotificationSimulation | null>(null);
  const [busy, setBusy] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [error, setError] = useState("");
  const [family, setFamily] = useState<"all" | ManagedAlertRule["family"]>("all");
  const [severity, setSeverity] = useState("all");
  const [includeDisabled, setIncludeDisabled] = useState(true);
  const [condition, setCondition] = useState<"Fired" | "Resolved">("Fired");
  const [inventoryFacets, setInventoryFacets] = useState<BulkNotificationSimulation["facets"]>();
  const [outcome, setOutcome] = useState("all");
  const [displayMode, setDisplayMode] = useState<DisplayMode>("all");
  const [groupBy, setGroupBy] = useState("resource_type");
  const [density, setDensity] = useState<Density>("auto");
  const [selectedWorkloads, setSelectedWorkloads] = useState<string[]>([]);
  const [selectedSubscriptions, setSelectedSubscriptions] = useState<string[]>([]);
  const [resourceType, setResourceType] = useState("all");
  const [catalog, setCatalog] = useState<Catalog>({ workloads: [], subscriptions: [] });
  const [catalogWarning, setCatalogWarning] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const simulationRequest = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const simulationSequence = useRef(0);
  const simulationStartedAt = useRef(0);

  useEffect(() => {
    if (!busy || !simulationStartedAt.current) return;
    const update = () => setElapsedSeconds(Math.floor((Date.now() - simulationStartedAt.current) / 1000));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    let active = true;
    setCatalogWarning("");
    Promise.allSettled([api.workloads(), api.alertsAuthoringOptions({ connection_id: params.connection_id })]).then(([workloadsResult, subscriptionsResult]) => {
      if (!active) return;
      setCatalog({
        workloads: workloadsResult.status === "fulfilled" ? workloadsResult.value.workloads : [],
        subscriptions: subscriptionsResult.status === "fulfilled" ? subscriptionsResult.value.subscriptions.map((item) => ({ id: item.id, name: item.name })) : [],
      });
      const failures = [workloadsResult, subscriptionsResult].filter((item) => item.status === "rejected").length;
      if (failures) setCatalogWarning("Some workload or subscription metadata is unavailable; hierarchy and mapping counts use only readable inventory.");
    });
    return () => { active = false; };
  }, [params.connection_id]);
  const selectKpiDisplay = (mode: DisplayMode) => setDisplayMode(mode);


  async function run() {
    simulationRequest.current?.controller.abort();
    const request = { sequence: ++simulationSequence.current, controller: new AbortController() };
    simulationRequest.current = request;
    simulationStartedAt.current = Date.now();
    setElapsedSeconds(0);
    setBusy(true); setError(""); setPage(1);
    try {
      const next = await api.bulkSimulateNotificationPaths({ ...params, monitor_condition: condition, include_disabled: includeDisabled, families: family === "all" ? [] : [family], severities: severity === "all" ? [] : [Number(severity)] }, request.controller.signal);
      if (simulationRequest.current?.sequence === request.sequence) {
        setInventoryFacets(next.facets);
        setResult(next);
      }
    } catch (cause) {
      if (!request.controller.signal.aborted && simulationRequest.current?.sequence === request.sequence) setError(formatError(cause));
    } finally {
      if (simulationRequest.current?.sequence === request.sequence) setBusy(false);
    }
  }

  const invalidateSimulationForFilterChange = () => {
    simulationSequence.current += 1;
    simulationRequest.current?.controller.abort();
    simulationRequest.current = null;
    // The explorer keeps its own graph mounted; clearing the result here would unmount it
    // mid-simulation and drop fullscreen. Showing stale nodes briefly is the lesser evil.
    setResult(null);
    setBusy(true);
    setError("");
    setDisplayMode("all");
    setSelectedWorkloads([]);
    setSelectedSubscriptions([]);
    setResourceType("all");
    setQuery("");
    setOutcome("all");
    setPage(1);
  };

  const availableFamilies = FAMILY_OPTIONS.filter((value) => Number(inventoryFacets?.families?.[value] || 0) > 0);
  const availableSeverities = [0, 1, 2, 3, 4].filter((value) => Number(inventoryFacets?.severities?.[value as 0 | 1 | 2 | 3 | 4] || 0) > 0);
  useEffect(() => {
    if (family !== "all" && inventoryFacets && !availableFamilies.includes(family)) {
      invalidateSimulationForFilterChange();
      setFamily("all");
      setSeverity("all");
    } else if (severity !== "all" && inventoryFacets && !availableSeverities.includes(Number(severity))) {
      invalidateSimulationForFilterChange();
      setSeverity("all");
    }
  }, [inventoryFacets]);

  useEffect(() => {
    const hasScope = !!(params.workload_id || params.subscription_id || params.management_group_id);
    if (!hasScope) return;
    const timer = window.setTimeout(() => void run(), 350);
    return () => window.clearTimeout(timer);
  }, [params.connection_id, params.workload_id, params.subscription_id, params.management_group_id, family, severity, condition, includeDisabled]);

  useEffect(() => () => simulationRequest.current?.controller.abort(), []);

  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const searchableRoutes = useMemo(() => (result?.routes ?? []).map((route) => ({
    route,
    text: `${route.rule_name} ${route.action_group_name} ${route.receiver_type} ${route.receiver_destination || route.receiver_masked} ${route.resource_ids.join(" ")}`.toLowerCase(),
  })), [result]);
  const scopeKind = scopeKindOf(params);
  useEffect(() => {
    setSelectedWorkloads([]); setSelectedSubscriptions([]); setResourceType("all");
    setInventoryFacets(undefined); setFamily("all"); setSeverity("all");
    if (scopeKind === "workload" && ["workloads", "shared", "unmapped"].includes(displayMode)) setDisplayMode("all");
  }, [params.workload_id, params.subscription_id, params.management_group_id, scopeKind]);
  const resourceModel = useMemo(() => {
    if (!result) return { resources: [] as ResourceRecord[], states: new Map<string, { routes: SimulationRoute[]; alerted: boolean; healthy: boolean; gap: boolean }>(), partial: false };
    const workloads = result.workloads?.length ? result.workloads.map((item) => ({ ...item, resource_ids: item.resource_ids || [] })) : catalog.workloads.map((item) => ({ id: item.id, name: item.name, resource_ids: item.nodes.map((node) => node.id), subscription_ids: [...new Set(item.nodes.map((node) => node.subscription_id || subscriptionFromId(node.id)).filter(Boolean))] }));
    const candidates = new Map<string, ResourceRecord>();
    const add = (item: Partial<ResourceRecord> & { id: string }) => {
      const id = item.id; if (!id || id === "unscoped") return;
      const normalized = normalizeId(id); const existing = candidates.get(normalized);
      const workloadIds = workloads.filter((workload) => (workload.resource_ids || []).some((member) => scopeContains(member, id))).map((workload) => workload.id).sort();
      candidates.set(normalized, { id, name: item.name || existing?.name || id.split("/").filter(Boolean).pop() || id, type: item.type || existing?.type || armTypeFromId(id) || "unknown", resourceGroup: item.resourceGroup || existing?.resourceGroup || (id.match(/\/resourcegroups\/([^/]+)/i)?.[1] || ""), subscriptionId: item.subscriptionId || existing?.subscriptionId || subscriptionFromId(id), subscriptionName: item.subscriptionName || existing?.subscriptionName || "", workloadIds: item.workloadIds || existing?.workloadIds || workloadIds, accessible: item.accessible ?? existing?.accessible ?? true });
    };
    for (const item of result.resources || []) add({ id: item.id, name: item.name, type: item.resource_type || item.type, resourceGroup: item.resource_group, subscriptionId: item.subscription_id, subscriptionName: item.subscription_name, workloadIds: item.workload_ids, accessible: item.accessible });
    for (const node of result.nodes) if (node.kind === "resource" && node.resource_id && node.resource_id !== "unscoped") add({ id: node.resource_id, name: node.name });
    if (scopeKind === "workload") {
      const selected = catalog.workloads.find((item) => item.id === params.workload_id);
      for (const node of selected?.nodes || []) add({ id: node.id, name: node.name, type: node.resource_type || undefined, resourceGroup: node.resource_group || undefined, subscriptionId: node.subscription_id || undefined, subscriptionName: node.subscription_name || undefined, workloadIds: [selected!.id] });
    }
    let resources = [...candidates.values()].sort((a, b) => a.id.localeCompare(b.id));
    if (scopeKind === "workload") resources = resources.filter((item) => item.workloadIds.includes(params.workload_id || ""));
    if (scopeKind === "subscription") resources = resources.filter((item) => normalizeId(item.subscriptionId) === normalizeId(params.subscription_id || ""));
    const mgSubscriptions = new Set((result.subscriptions || []).map((item) => normalizeId(item.id)));
    if (scopeKind === "management_group" && mgSubscriptions.size) resources = resources.filter((item) => mgSubscriptions.has(normalizeId(item.subscriptionId)));
    const states = new Map<string, { routes: SimulationRoute[]; alerted: boolean; healthy: boolean; gap: boolean }>();
    for (const item of resources) {
      const routes = result.routes.filter((route) => route.resource_ids.some((scope) => scopeContains(scope, item.id)));
      const alerted = routes.length > 0; const healthy = routes.some((route) => route.would_run === true || route.outcome === "deliver");
      states.set(normalizeId(item.id), { routes, alerted, healthy, gap: !healthy });
    }
    const partial = result.completeness?.partial === true || result.completeness?.complete === false || (!result.resources?.length && scopeKind !== "workload");
    return { resources, states, partial };
  }, [result, catalog.workloads, params.workload_id, params.subscription_id, scopeKind]);
  const facetFilteredResources = useMemo(() => resourceModel.resources.filter((item) => {
    if (resourceType !== "all" && item.type !== resourceType) return false;
    if (selectedWorkloads.length && !item.workloadIds.some((id) => selectedWorkloads.includes(id))) return false;
    if (selectedSubscriptions.length && !selectedSubscriptions.includes(item.subscriptionId)) return false;
    return true;
  }), [resourceModel.resources, resourceType, selectedWorkloads, selectedSubscriptions]);
  const filteredResources = useMemo(() => facetFilteredResources.filter((item) => {
    const state = resourceModel.states.get(normalizeId(item.id)); const shared = item.workloadIds.length > 1;
    return displayMode === "all" || (displayMode === "workloads" && item.workloadIds.length > 0) || (displayMode === "shared" && shared) || (displayMode === "unmapped" && !item.workloadIds.length) || (displayMode === "alerted" && !!state?.alerted) || (displayMode === "no-alert" && !state?.alerted) || (displayMode === "healthy" && !!state?.healthy) || (displayMode === "gaps" && !!state?.gap);
  }), [facetFilteredResources, resourceModel.states, displayMode]);
  const resourceKpis = useMemo(() => ({ total: facetFilteredResources.length, mapped: facetFilteredResources.filter((item) => item.workloadIds.length).length, shared: facetFilteredResources.filter((item) => item.workloadIds.length > 1).length, unmapped: facetFilteredResources.filter((item) => !item.workloadIds.length).length, alerted: facetFilteredResources.filter((item) => resourceModel.states.get(normalizeId(item.id))?.alerted).length, noAlert: facetFilteredResources.filter((item) => !resourceModel.states.get(normalizeId(item.id))?.alerted).length, healthy: facetFilteredResources.filter((item) => resourceModel.states.get(normalizeId(item.id))?.healthy).length, gaps: facetFilteredResources.filter((item) => resourceModel.states.get(normalizeId(item.id))?.gap).length }), [facetFilteredResources, resourceModel.states]);
  const displayCounts: Record<DisplayMode, number> = { all: resourceKpis.total, workloads: resourceKpis.mapped, shared: resourceKpis.shared, unmapped: resourceKpis.unmapped, alerted: resourceKpis.alerted, "no-alert": resourceKpis.noAlert, gaps: resourceKpis.gaps, healthy: resourceKpis.healthy };
  const clearGraphFilters = () => { setDisplayMode("all"); setSelectedWorkloads([]); setSelectedSubscriptions([]); setResourceType("all"); };
  const workloadOptions = useMemo(() => catalog.workloads.map((item) => ({ id: item.id, name: item.name, count: resourceModel.resources.filter((resource) => resource.workloadIds.includes(item.id)).length })).filter((item) => item.count).sort((a, b) => a.name.localeCompare(b.name)), [catalog.workloads, resourceModel.resources]);
  const subscriptionOptions = useMemo(() => { const names = new Map(catalog.subscriptions.map((item) => [normalizeId(item.id), item.name])); return [...new Set(resourceModel.resources.map((item) => item.subscriptionId).filter(Boolean))].map((id) => ({ id, name: names.get(normalizeId(id)) || resourceModel.resources.find((item) => item.subscriptionId === id)?.subscriptionName || id, count: resourceModel.resources.filter((item) => item.subscriptionId === id).length })).sort((a, b) => a.name.localeCompare(b.name)); }, [catalog.subscriptions, resourceModel.resources]);
  const resourceTypes = useMemo(() => [...new Set(resourceModel.resources.map((item) => item.type).filter(Boolean))].sort(), [resourceModel.resources]);
  const resolvedDensity = effectiveDensity(density, filteredResources.length);
  const filteredRoutes = useMemo(() => {
    return searchableRoutes.filter(({ route, text }) => (outcome === "all" || route.outcome === outcome) && (!deferredQuery || text.includes(deferredQuery))).map(({ route }) => route);
  }, [searchableRoutes, outcome, deferredQuery]);
  const scopeAwareGraph = useMemo(() => {
    if (!result) return { nodes: [] as Array<Record<string, unknown> & { id: string; name: string; kind: string; status: string }>, links: [] as Array<{ source: string; target: string; value: number; status: string; receiver_type?: string }> };
    const nodes = new Map<string, Record<string, unknown> & { id: string; name: string; kind: string; status: string }>();
    const links = new Map<string, { source: string; target: string; value: number; status: string; receiver_type?: string }>();
    const addNode = (value: Record<string, unknown> & { id: string; name: string; kind: string; status: string }) => { if (!nodes.has(value.id)) nodes.set(value.id, value); };
    const addLink = (source: string, target: string, status = "ok", value = 1, receiver_type?: string) => { const key = source + "|" + target + "|" + status; const old = links.get(key); if (old) old.value += value; else links.set(key, { source, target, value, status, receiver_type }); };
    const rootId = "scope:" + scopeKind + ":" + (params.workload_id || params.subscription_id || params.management_group_id || "selected");
    const rootName = result.scope?.name || (scopeKind === "workload" ? catalog.workloads.find((item) => item.id === params.workload_id)?.name : scopeKind === "subscription" ? subscriptionOptions.find((item) => item.id === params.subscription_id)?.name : undefined) || (scopeKind === "management_group" ? "Management group" : scopeKind === "subscription" ? "Subscription" : "Workload");
    addNode({ id: rootId, name: rootName, kind: "scope", status: resourceModel.partial ? "warning" : "ok" });
    const selectedRuleIds = new Set<string>();
    const resourceTargets = new Map<string, { id: string; count: number; names: string[] }>();
    for (const resource of filteredResources) {
      let parentId = rootId;
      if (scopeKind === "management_group") {
        const subscriptionId = "subscription:" + normalizeId(resource.subscriptionId || "unknown");
        addNode({ id: subscriptionId, name: resource.subscriptionName || subscriptionOptions.find((item) => item.id === resource.subscriptionId)?.name || resource.subscriptionId || "Unknown subscription", kind: "subscription", status: resource.accessible ? "ok" : "error" });
        addLink(rootId, subscriptionId, resource.accessible ? "ok" : "error"); parentId = subscriptionId;
      }
      if (scopeKind !== "workload") {
        const shared = resource.workloadIds.length > 1;
        const workload = !shared && resource.workloadIds.length ? catalog.workloads.find((item) => item.id === resource.workloadIds[0]) : undefined;
        const bucketKey = shared ? "shared" : workload ? "workload:" + workload.id : "unmapped";
        const bucketId = parentId + ":bucket:" + bucketKey;
        addNode({ id: bucketId, name: shared ? "Shared resources" : workload?.name || "Unmapped resources", kind: shared ? "bucket" : workload ? "workload" : "bucket", status: "ok" });
        addLink(parentId, bucketId); parentId = bucketId;
      }
      const state = resourceModel.states.get(normalizeId(resource.id));
      const grouping = groupBy === "resource_group" ? resource.resourceGroup || "No resource group" : groupBy === "workload" ? resource.workloadIds.map((id) => catalog.workloads.find((item) => item.id === id)?.name || id).join(" + ") || "Unmapped" : groupBy === "alert_state" ? state?.healthy ? "Healthy" : state?.alerted ? "Flow gap" : "No alert" : resource.type;
      let targetId = "resource:" + normalizeId(resource.id);
      if (resolvedDensity === "detailed") {
        const groupId = parentId + ":detail-group:" + groupBy + ":" + normalizeId(grouping);
        addNode({ id: groupId, name: grouping, kind: "bucket", status: state?.healthy ? "ok" : "warning" });
        addLink(parentId, groupId, resource.accessible ? "ok" : "error");
        addNode({ id: targetId, name: resource.name, kind: "resource", status: resource.accessible ? "ok" : "error", resource_type: resource.type, resource_id: resource.id });
        addLink(groupId, targetId, resource.accessible ? "ok" : "error");
      } else {
        targetId = parentId + ":group:" + groupBy + ":" + normalizeId(grouping);
        const aggregate = resourceTargets.get(targetId) || { id: targetId, count: 0, names: [] }; aggregate.count += 1; aggregate.names.push(resource.name); resourceTargets.set(targetId, aggregate);
        addNode({ id: targetId, name: grouping, kind: "resource", status: state?.healthy ? "ok" : "warning", resource_type: groupBy === "resource_type" ? resource.type : "", resource_id: resource.id });
        addLink(parentId, targetId, resource.accessible ? "ok" : "error");
      }
      const ruleIds = [...new Set((state?.routes || []).map((route) => route.rule_id).filter(Boolean))].sort();
      if (!ruleIds.length) {
        const noAlertId = "alert:none"; const gapId = "outcome:coverage-gap";
        addNode({ id: noAlertId, name: "No alert rule", kind: "alert", status: "warning", resource_id: "" }); addNode({ id: gapId, name: "Coverage gap", kind: "outcome", status: "error" }); addLink(targetId, noAlertId, "warning"); addLink(noAlertId, gapId, "error");
      } else for (const ruleId of ruleIds) { selectedRuleIds.add(normalizeId(ruleId)); addLink(targetId, "alert:" + normalizeId(ruleId), state?.healthy ? "ok" : "warning"); }
    }
    for (const aggregate of resourceTargets.values()) { const node = nodes.get(aggregate.id); if (node) { node.name = String(node.name) + " (" + aggregate.count + ")"; node.path_count = aggregate.count; node.paths = aggregate.names.slice(0, 3); } }
    const sourceNodes = new Map(result.nodes.map((node) => [node.id, node]));
    const allowed = new Set([...selectedRuleIds].map((id) => "alert:" + id)); const queue = [...allowed];
    while (queue.length) { const id = queue.shift()!; for (const link of result.links) if (link.source === id && !allowed.has(link.target)) { allowed.add(link.target); queue.push(link.target); } }
    for (const id of allowed) { const node = sourceNodes.get(id); if (node) addNode({ ...node }); }
    for (const link of result.links) if (allowed.has(link.source) && allowed.has(link.target) && !link.source.startsWith("resource:")) addLink(link.source, link.target, link.status, link.value, link.receiver_type);
    for (const subscriptionId of result.completeness?.inaccessible_subscription_ids || []) {
      if (scopeKind !== "management_group") continue; const id = "subscription:" + normalizeId(subscriptionId); addNode({ id, name: subscriptionId + " (inaccessible)", kind: "subscription", status: "error" }); addLink(rootId, id, "error");
    }
    return { nodes: [...nodes.values()].sort((a, b) => a.id.localeCompare(b.id)), links: [...links.values()].sort((a, b) => (a.source + a.target + a.status).localeCompare(b.source + b.target + b.status)) };
  }, [result, filteredResources, resourceModel, scopeKind, params.workload_id, params.subscription_id, params.management_group_id, catalog.workloads, subscriptionOptions, resolvedDensity, groupBy]);
  // The shared explorer owns budgeting, search, paths, highlight, zoom and pan; this
  // component only has to say what the nodes and links mean.
  const flowNodes = useMemo<FlowNode[]>(() => scopeAwareGraph.nodes.map((node) => ({
    id: node.id,
    name: String(node.name),
    kind: String(node.kind),
    status: String(node.status || "ok"),
    resource_type: node.kind === "resource"
      ? String(node.resource_type || armTypeFromId(String(node.resource_id || "")))
      : node.kind === "alert" ? ALERT_ICON_TYPES[String(node.family || "")] || ALERT_ICON_TYPES.metric
        : node.kind === "action_group" ? "microsoft.insights/actiongroups" : "",
  })), [scopeAwareGraph.nodes]);
  const flowLinks = useMemo<FlowLink[]>(
    () => scopeAwareGraph.links.map((link) => ({ source: link.source, target: link.target, value: link.value, status: link.status })),
    [scopeAwareGraph.links],
  );
  const pageCount = Math.max(1, Math.ceil(filteredRoutes.length / 100));
  const currentPage = Math.min(page, pageCount);
  const visibleRoutes = filteredRoutes.slice((currentPage - 1) * 100, currentPage * 100);

  return <div className="space-y-3">
    {error && <div className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">{error}</div>}

    {!result && <section className="overflow-hidden rounded-xl border bg-white" aria-busy={busy}>
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
        <div className="mr-auto"><h3 className="font-semibold">Expected notification flow</h3><p className="text-xs text-gray-500">Search in plain text, or click a name, icon, vertical bar, or flow segment to highlight complete paths.</p></div>
        <input aria-label="Search notification flow" disabled placeholder="Search resource, alert, group, receiver…" className="w-72 rounded border bg-gray-50 px-3 py-1.5 text-xs" />
        <div role="group" aria-label="Sankey zoom controls" className="flex items-center overflow-hidden rounded border bg-white text-xs opacity-50"><button type="button" disabled className="h-7 w-7 border-r">−</button><output className="w-12 text-center">100%</output><button type="button" disabled className="h-7 w-7 border-l">+</button><button type="button" disabled className="h-7 border-l px-2">Fit</button></div>
        <button type="button" disabled className="h-7 rounded border bg-white px-2.5 text-xs opacity-50">⛶ Full screen</button>
        <button onClick={() => void run()} disabled={busy} className="h-7 shrink-0 rounded-lg bg-gray-900 px-3 text-xs font-medium text-white disabled:opacity-50">{busy ? "Building routing graph…" : "▶ Simulate all alerts"}</button>
      </div>
      <div className="flex flex-wrap items-end gap-x-2 gap-y-1.5 border-b bg-gray-50/40 px-4 py-2">
        <label className="w-32 flex-none text-xs">Rule family<select value={family} onChange={(event) => { invalidateSimulationForFilterChange(); setFamily(event.target.value as typeof family); setSeverity("all"); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option value="all">All families{inventoryFacets ? ` (${inventoryFacets.total_rules})` : ""}</option>{availableFamilies.map((value) => <option key={value} value={value}>{value} ({inventoryFacets?.families?.[value]})</option>)}</select></label>
        <label className="w-28 flex-none text-xs">Severity<select value={severity} onChange={(event) => { invalidateSimulationForFilterChange(); setSeverity(event.target.value); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option value="all">All severities</option>{availableSeverities.map((value) => <option key={value} value={value}>Sev {value} ({inventoryFacets?.severities?.[value as 0 | 1 | 2 | 3 | 4]})</option>)}</select></label>
        <label className="w-24 flex-none text-xs">Event state<select value={condition} onChange={(event) => { invalidateSimulationForFilterChange(); setCondition(event.target.value as typeof condition); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option>Fired</option><option>Resolved</option></select></label>
        <label className="flex h-7 flex-none items-center gap-1.5 whitespace-nowrap text-xs"><input type="checkbox" checked={includeDisabled} onChange={(event) => { invalidateSimulationForFilterChange(); setIncludeDisabled(event.target.checked); }} /> Include disabled rules</label>
      </div>
      <div className="flex flex-wrap items-center gap-1 border-b px-4 py-2">{["Total", "Mapped", "Unmapped", "Alerted", "No alert", "Healthy", "Flow gaps"].map((label) => <div key={label} className="h-8 min-w-16 rounded-lg border bg-gray-50 px-2 py-px"><div className="h-4 w-6 animate-pulse rounded bg-gray-200" /><div className="mt-0.5 text-[8px] font-medium uppercase tracking-wide text-gray-400">{label}</div></div>)}</div>
      {/* The spinner is keyed on `busy`, NOT on `!result`. It used to render whenever there was
          no result, so a failed simulation left "Building the notification flow… Elapsed 0:01"
          spinning forever underneath the error banner — the screen claimed to be working on
          something it had already given up on. */}
      {busy ? (
        <div className="flex h-[580px] flex-col items-center justify-center gap-3 text-sm text-gray-500"><div className="h-7 w-7 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600" /><div>Building the notification flow…</div><div className="text-xs tabular-nums text-gray-500" role="timer" aria-live="off">Elapsed {elapsedLabel(elapsedSeconds)}</div><div className="text-xs text-gray-400">Loading resources, alert rules, Action Groups, and receivers.</div></div>
      ) : (
        <div className="flex h-[580px] flex-col items-center justify-center gap-2 px-8 text-center text-sm text-gray-500">
          <div className="text-2xl">{error ? "⚠️" : "🔀"}</div>
          <div className="font-medium text-gray-700">
            {error ? "The notification flow could not be built." : "No notification flow has been built yet."}
          </div>
          <div className="max-w-md text-xs text-gray-500">
            {error
              ? "Nothing below is a measurement of this scope — the inventory it needs could not be read. The reason is shown above."
              : "Choose a scope, then run the simulation to trace every alert rule to the people it would page."}
          </div>
          <button onClick={() => void run()} className="mt-1 h-7 rounded-lg bg-gray-900 px-3 text-xs font-medium text-white">
            {error ? "↻ Try again" : "▶ Simulate all alerts"}
          </button>
        </div>
      )}
    </section>}

    {result && <>
      <SankeyExplorer
        nodes={flowNodes}
        links={flowLinks}
        title="Expected notification flow"
        subtitle="Search in plain text, or click a name, icon, vertical bar, or flow segment to highlight complete paths."
        colors={NODE_COLORS}
        iconKinds={ICON_KINDS}
        labelRightKinds={LABEL_RIGHT_KINDS}
        storageKey="azsup.alertsManager.sankeyZoom"
        searchPlaceholder="Search resource, alert, group, receiver…"
        emptyMessage="No resources match the selected graph filters."
        onClearFilters={clearGraphFilters}
        formatValue={(value) => `${value} flow${value === 1 ? "" : "s"}`}
        actions={<button onClick={() => void run()} disabled={busy} className="h-7 shrink-0 rounded-lg bg-gray-900 px-3 text-xs font-medium text-white disabled:opacity-50">{busy ? "Building routing graph…" : "▶ Simulate all alerts"}</button>}
        filterBar={<>
        <div className="flex flex-wrap items-end gap-x-2 gap-y-1.5 border-b bg-gray-50/40 px-4 py-2">
          <label className="w-32 flex-none text-xs">Rule family<select value={family} onChange={(event) => { invalidateSimulationForFilterChange(); setFamily(event.target.value as typeof family); setSeverity("all"); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option value="all">All families{inventoryFacets ? ` (${inventoryFacets.total_rules})` : ""}</option>{availableFamilies.map((value) => <option key={value} value={value}>{value} ({inventoryFacets?.families?.[value]})</option>)}</select></label>
          <label className="w-28 flex-none text-xs">Severity<select value={severity} onChange={(event) => { invalidateSimulationForFilterChange(); setSeverity(event.target.value); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option value="all">All severities</option>{availableSeverities.map((value) => <option key={value} value={value}>Sev {value} ({inventoryFacets?.severities?.[value as 0 | 1 | 2 | 3 | 4]})</option>)}</select></label>
          <label className="w-24 flex-none text-xs">Event state<select value={condition} onChange={(event) => { invalidateSimulationForFilterChange(); setCondition(event.target.value as typeof condition); }} className="mt-0.5 w-full rounded border bg-white px-2 py-1"><option>Fired</option><option>Resolved</option></select></label>
          <label className="flex h-7 flex-none items-center gap-1.5 whitespace-nowrap text-xs"><input type="checkbox" checked={includeDisabled} onChange={(event) => { invalidateSimulationForFilterChange(); setIncludeDisabled(event.target.checked); }} /> Include disabled rules</label>
          <div className="ml-auto flex h-7 items-center gap-1.5"><button onClick={() => download(csv(result), "notification-paths.csv", "text/csv")} className="rounded border bg-white px-2 py-1 text-xs">CSV</button><button onClick={() => download(JSON.stringify(result, null, 2), "notification-paths.json", "application/json")} className="rounded border bg-white px-2 py-1 text-xs">JSON</button></div>
        </div>
        <div className="flex flex-wrap items-center gap-1 border-b px-4 py-2"><Kpi label="Total" value={resourceKpis.total} active={displayMode === "all"} onClick={() => selectKpiDisplay("all")} /><Kpi label="Mapped" value={resourceKpis.mapped} active={scopeKind !== "workload" && displayMode === "workloads"} onClick={() => selectKpiDisplay(scopeKind === "workload" ? "all" : "workloads")} /><Kpi label="Unmapped" value={resourceKpis.unmapped} tone="text-amber-600" active={scopeKind !== "workload" && displayMode === "unmapped"} onClick={() => selectKpiDisplay(scopeKind === "workload" ? "all" : "unmapped")} /><Kpi label="Alerted" value={resourceKpis.alerted} active={displayMode === "alerted"} onClick={() => selectKpiDisplay("alerted")} /><Kpi label="No alert" value={resourceKpis.noAlert} tone="text-red-600" active={displayMode === "no-alert"} onClick={() => selectKpiDisplay("no-alert")} /><Kpi label="Healthy" value={resourceKpis.healthy} tone="text-green-600" active={displayMode === "healthy"} onClick={() => selectKpiDisplay("healthy")} /><Kpi label="Flow gaps" value={resourceKpis.gaps} tone="text-red-600" active={displayMode === "gaps"} onClick={() => selectKpiDisplay("gaps")} /><span title="Resources with no complete, runnable notification path to a receiver." className="cursor-help text-[10px] text-gray-400">ⓘ</span><div className="ml-auto max-w-xl text-right text-[10px] leading-tight text-gray-500">{result.warning}{(resourceModel.partial || catalogWarning || result.completeness?.warnings?.length) && <div className="mt-0.5 text-amber-700">Partial view: {catalogWarning || result.completeness?.warnings?.join(" ") || "the backend did not return a complete enriched resource inventory; counts include readable resources only."}</div>}</div></div>
        <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50/70 px-4 py-2">
          <label className="text-xs">Display<select value={displayMode} onChange={(event) => setDisplayMode(event.target.value as DisplayMode)} className="ml-1 rounded border bg-white px-2 py-1">{(["all", ...(scopeKind === "workload" ? [] : ["workloads", "shared", "unmapped"]), "alerted", "no-alert", "gaps", "healthy"] as DisplayMode[]).map((value) => <option key={value} value={value} disabled={value !== displayMode && displayCounts[value] === 0}>{displayLabel(value)} ({displayCounts[value]})</option>)}</select></label>
          <label className="text-xs">Group by<select value={groupBy} onChange={(event) => setGroupBy(event.target.value)} className="ml-1 rounded border bg-white px-2 py-1">{(scopeKind === "workload" ? [["resource_type", "Resource type"], ["resource_group", "Resource group"], ["alert_state", "Alert state"]] : [["workload", "Workload"], ["resource_type", "Resource type"], ["resource_group", "Resource group"], ["alert_state", "Alert state"]]).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="text-xs">Density<select value={density} onChange={(event) => setDensity(event.target.value as Density)} className="ml-1 rounded border bg-white px-2 py-1"><option value="auto">Auto ({resolvedDensity})</option><option value="detailed">Detailed</option><option value="compact">Compact</option><option value="summary">Summary</option></select></label>
          {scopeKind !== "workload" && workloadOptions.length > 0 && <MultiSelectFilter label="Workloads" values={workloadOptions} selected={selectedWorkloads} onChange={setSelectedWorkloads} />}
          {scopeKind === "management_group" && subscriptionOptions.length > 0 && <MultiSelectFilter label="Subscriptions" values={subscriptionOptions} selected={selectedSubscriptions} onChange={setSelectedSubscriptions} />}
          <label className="text-xs">Resource type<select value={resourceType} onChange={(event) => setResourceType(event.target.value)} className="ml-1 max-w-64 rounded border bg-white px-2 py-1"><option value="all">All ({resourceTypes.length})</option>{resourceTypes.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <span className="ml-auto text-[10px] text-gray-500">Showing {filteredResources.length} of {resourceKpis.total} resources · {resolvedDensity} density</span>
          {(displayMode !== "all" || selectedWorkloads.length > 0 || selectedSubscriptions.length > 0 || resourceType !== "all") && <button type="button" onClick={clearGraphFilters} className="rounded border bg-white px-2 py-1 text-xs text-blue-700">Clear filters</button>}
        </div>
        </>}
      />

      <section className="overflow-hidden rounded-xl border bg-white"><div className="flex flex-wrap items-center gap-2 border-b px-4 py-3"><h3 className="mr-auto font-semibold">Notification routes <span className="text-xs font-normal text-gray-500">({filteredRoutes.length})</span></h3><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="Search resource, rule, group, receiver…" className="w-72 rounded border px-2 py-1.5 text-xs" /><select value={outcome} onChange={(event) => { setOutcome(event.target.value); setPage(1); }} className="rounded border px-2 py-1.5 text-xs"><option value="all">All outcomes</option><option value="deliver">Expected delivery</option><option value="disabled">Disabled</option><option value="unresolved_group">Cross-subscription group not visible</option><option value="missing_group">Missing group</option><option value="no_receiver">No receiver</option></select></div><div className="overflow-auto"><table className="w-full min-w-[1200px] text-left text-xs"><thead className="bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Resource</th><th>Alert</th><th>Action Group</th><th>Receiver</th><th>Schema</th><th>Outcome</th><th>Issues</th></tr></thead><tbody className="divide-y">{visibleRoutes.map((route,index) => <tr key={`${route.rule_id}:${route.action_group_id}:${route.receiver_fingerprint ?? index}`}><td className="max-w-xs px-3 py-2"><div className="truncate" title={route.resource_ids.join("\n")}>{route.resource_ids.map((id) => id.split("/").pop()).join(", ") || "Unscoped"}</div></td><td><div className="font-medium">{route.rule_name}</div><div className="text-[10px] text-gray-400">{route.family} · Sev {route.severity ?? "—"}</div></td><td>{route.action_group_name || "—"}</td><td><div className="capitalize">{route.receiver_type || "—"}</div><div className="text-[10px] text-gray-400">{route.receiver_destination || route.receiver_masked}</div></td><td>{route.payload_schema || "—"}</td><td><span className={`rounded px-2 py-0.5 ${route.would_run ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{route.outcome.replaceAll("_"," ")}</span></td><td className="text-red-600">{route.issues.join(" · ") || "—"}</td></tr>)}</tbody></table></div>{filteredRoutes.length > 100 && <div className="flex items-center justify-between border-t px-3 py-2 text-xs"><span>Showing {(currentPage-1)*100+1}–{Math.min(currentPage*100,filteredRoutes.length)} of {filteredRoutes.length}</span><div className="flex gap-2"><button disabled={currentPage===1} onClick={() => setPage(currentPage-1)} className="rounded border px-2 py-1 disabled:opacity-40">Previous</button><span>Page {currentPage} of {pageCount}</span><button disabled={currentPage===pageCount} onClick={() => setPage(currentPage+1)} className="rounded border px-2 py-1 disabled:opacity-40">Next</button></div></div>}</section>

      <section className="rounded-xl border bg-white"><div className="border-b px-4 py-3"><h3 className="font-semibold">Routing diagnostics</h3><p className="text-xs text-gray-500">Prioritized delivery risks found across the complete scope.</p></div>{result.diagnostics.length ? <div className="divide-y">{result.diagnostics.map((item,index) => <div key={`${item.code}:${item.rule_id}:${index}`} className="flex items-start gap-3 p-3 text-xs"><span className={`rounded px-2 py-0.5 font-medium ${item.severity === "critical" || item.severity === "high" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}>{item.severity}</span><div><div className="font-medium text-gray-800">{item.message}</div><div className="text-gray-400">{item.rule_name || item.receiver || item.action_group_id}</div></div></div>)}</div> : <div className="p-8 text-center text-sm text-green-700">No routing diagnostics found.</div>}</section>
      {!filteredRoutes.length && <div className="rounded-xl border bg-white p-8 text-center text-sm text-gray-500"><div>{query || outcome !== "all" ? "No notification routes match the selected filters." : "No notification routes were produced for this simulation."}</div><button type="button" onClick={() => { setQuery(""); setOutcome("all"); setPage(1); }} className="mt-2 rounded border px-3 py-1 text-xs text-blue-700">Clear filters</button></div>}
    </>}
  </div>;
}
