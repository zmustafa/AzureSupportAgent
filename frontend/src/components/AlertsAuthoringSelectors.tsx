import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type AlertsAuthoringOptions, type ResolvedAlertScope, type WorkloadNode } from "../api";
import { AzureIcon, friendlyLocation, friendlyResourceType } from "./AzureIcon";

const control = "mt-1 w-full rounded border px-2 py-1.5 disabled:bg-gray-100";

export function useAlertsAuthoringOptions(connectionId: string, subscriptionId = "", resourceGroup = "") {
  return useQuery({
    queryKey: ["alerts-authoring-options", connectionId, subscriptionId, resourceGroup],
    queryFn: () => api.alertsAuthoringOptions({ connection_id: connectionId, subscription_id: subscriptionId, resource_group: resourceGroup }),
    staleTime: 5 * 60 * 1000,
  });
}

function fallbackScope(resourceId: string): ResolvedAlertScope {
  const parts = resourceId.split("/").filter(Boolean);
  const lower = parts.map((part) => part.toLowerCase());
  const subscriptionIndex = lower.indexOf("subscriptions");
  const groupIndex = lower.indexOf("resourcegroups");
  const providerIndex = lower.indexOf("providers");
  const subscriptionId = subscriptionIndex >= 0 ? parts[subscriptionIndex + 1] || "" : "";
  const resourceGroup = groupIndex >= 0 ? parts[groupIndex + 1] || "" : "";
  const kind = providerIndex >= 0 ? "resource" : groupIndex >= 0 ? "resource_group" : "subscription";
  const resourceType = providerIndex >= 0 ? [parts[providerIndex + 1], parts[providerIndex + 2]].filter(Boolean).join("/") : "";
  return {
    kind, id: resourceId, name: parts.at(-1) || resourceId,
    subscription_id: subscriptionId, subscription_name: subscriptionId,
    resource_group: resourceGroup, resource_type: resourceType, location: "",
  };
}

export function useResolvedAlertScopes(connectionId: string, resourceIds: string[]) {
  const key = resourceIds.join("\n");
  const optionsQuery = useAlertsAuthoringOptions(connectionId);
  const query = useQuery({
    queryKey: ["alerts-authoring-resolved-scopes", connectionId, key],
    queryFn: () => api.resolveAlertsAuthoringScopes(connectionId, resourceIds),
    enabled: resourceIds.length > 0,
    staleTime: 5 * 60 * 1000,
  });
  const subscriptionNames = new Map(
    (optionsQuery.data?.subscriptions || []).map((item) => [item.id.toLowerCase(), item.name]),
  );
  const resources = (query.data?.resources || resourceIds.map(fallbackScope)).map((item) => {
    const normalizedId = item.id.replace(/^\/subscriptions\//i, "").replace(/\/$/, "").toLowerCase();
    const directSubscriptionName = !normalizedId.includes("/") ? subscriptionNames.get(normalizedId) || "" : "";
    const subscriptionId = item.subscription_id || (directSubscriptionName ? normalizedId : "");
    const catalogName = subscriptionNames.get(subscriptionId.toLowerCase()) || directSubscriptionName;
    const subscriptionName = catalogName || item.subscription_name || item.subscription_id;
    const isSubscription = item.kind === "subscription" || !!directSubscriptionName;
    const unresolvedScopeName = isSubscription
      && (item.name === item.id || item.name === item.subscription_id);
    return {
      ...item,
      kind: isSubscription ? "subscription" as const : item.kind,
      name: unresolvedScopeName && subscriptionName ? subscriptionName : item.name,
      subscription_id: subscriptionId,
      subscription_name: subscriptionName,
      resource_type: isSubscription ? "" : item.resource_type,
    };
  });
  return {
    ...query,
    isFetching: query.isFetching || optionsQuery.isFetching,
    resources,
  };
}

export function resolvedScopesToWorkloadNodes(resources: ResolvedAlertScope[]): WorkloadNode[] {
  return resources.map((item) => ({
    kind: item.kind, id: item.id, name: item.name,
    subscription_id: item.subscription_id || null, subscription_name: item.subscription_name || null,
    resource_group: item.resource_group || null,
    resource_type: item.resource_type || null, location: item.location || null,
  }));
}

export function SelectedScopesTable({ resources, loading = false, onRemove }: { resources: ResolvedAlertScope[]; loading?: boolean; onRemove: (id: string) => void }) {
  if (!resources.length) return <div className="rounded border border-dashed p-5 text-center text-xs text-gray-400">No scopes selected. Use Select targets to add Azure resources.</div>;
  return <div className="overflow-auto rounded border"><table className="w-full min-w-[760px] text-left text-xs"><thead className="bg-gray-50 text-gray-500"><tr><th className="px-3 py-2">Resource</th><th className="px-3 py-2">Type</th><th className="px-3 py-2">Subscription</th><th className="px-3 py-2">Resource group</th><th className="px-3 py-2">Location</th><th className="px-3 py-2" /></tr></thead><tbody className="divide-y">{resources.map((item) => <tr key={item.id} className="hover:bg-gray-50"><td className="max-w-xs px-3 py-2"><div className="flex items-center gap-2"><AzureIcon kind={item.kind} type={item.resource_type} className="h-4 w-4 shrink-0" /><span className="truncate font-medium text-gray-800" title={item.name}>{item.name}</span>{loading && <span className="h-3 w-3 animate-spin rounded-full border border-gray-300 border-t-brand" />}</div></td><td className="whitespace-nowrap px-3 py-2 text-gray-600">{item.kind === "resource" ? friendlyResourceType(item.resource_type) : item.kind === "resource_group" ? "Resource group" : "Subscription"}</td><td className="whitespace-nowrap px-3 py-2 text-gray-600">{item.subscription_name || item.subscription_id || "—"}</td><td className="whitespace-nowrap px-3 py-2 text-gray-600">{item.resource_group || "—"}</td><td className="whitespace-nowrap px-3 py-2 text-gray-600">{item.location ? friendlyLocation(item.location) : "—"}</td><td className="px-3 py-2 text-right"><button onClick={() => onRemove(item.id)} className="text-red-600 hover:underline">Remove</button></td></tr>)}</tbody></table></div>;
}

export function AzurePlacementFields({ connectionId, subscriptionId, resourceGroup, location, disabled = false, required = false, globalLocation = false, showLocation = true, onChange }: {
  connectionId: string;
  subscriptionId: string;
  resourceGroup: string;
  location: string;
  disabled?: boolean;
  required?: boolean;
  globalLocation?: boolean;
  showLocation?: boolean;
  onChange: (patch: { subscription_id?: string; resource_group?: string; location?: string }) => void;
}) {
  const optionsQ = useAlertsAuthoringOptions(connectionId, subscriptionId, resourceGroup);
  const options = optionsQ.data;
  const groups = options?.resource_groups || [];
  const locations = useMemo(() => {
    const values = new Set<string>();
    if (globalLocation) values.add("Global");
    if (location) values.add(location);
    for (const group of groups) if (group.location) values.add(group.location);
    for (const resource of options?.resources || []) if (resource.location) values.add(resource.location);
    return [...values].sort((a, b) => a.localeCompare(b));
  }, [globalLocation, groups, location, options?.resources]);
  return <>
    <label>Subscription{required && <span className="text-red-600"> *</span>}<select aria-label="Subscription" aria-required={required} required={required} disabled={disabled || optionsQ.isLoading} value={subscriptionId} onChange={(event) => onChange({ subscription_id: event.target.value, resource_group: "", ...(globalLocation ? {} : { location: "" }) })} className={control}><option value="">{optionsQ.isLoading ? "Loading subscriptions…" : "Select subscription…"}</option>{subscriptionId && !options?.subscriptions.some((item) => item.id === subscriptionId) && <option value={subscriptionId}>{subscriptionId}</option>}{(options?.subscriptions || []).map((item) => <option key={item.id} value={item.id}>{item.name} ({item.id.slice(0, 8)}…)</option>)}</select></label>
    <label>Resource group{required && <span className="text-red-600"> *</span>}<select aria-label="Resource group" aria-required={required} required={required} disabled={disabled || !subscriptionId || optionsQ.isFetching} value={resourceGroup} onChange={(event) => { const selected = groups.find((item) => item.name === event.target.value); onChange({ resource_group: event.target.value, ...(globalLocation ? {} : selected?.location ? { location: selected.location } : {}) }); }} className={control}><option value="">{!subscriptionId ? "Select subscription first" : optionsQ.isFetching ? "Loading resource groups…" : "Select resource group…"}</option>{resourceGroup && !groups.some((item) => item.name === resourceGroup) && <option value={resourceGroup}>{resourceGroup}</option>}{groups.map((item) => <option key={item.id} value={item.name}>{item.name}{item.location ? ` · ${item.location}` : ""}</option>)}</select></label>
    {showLocation && <label>{globalLocation ? "Processing region" : "Location"}{required && <span className="text-red-600"> *</span>}<select aria-label={globalLocation ? "Processing region" : "Location"} aria-required={required} required={required} disabled={disabled || (!globalLocation && !resourceGroup)} value={location} onChange={(event) => onChange({ location: event.target.value })} className={control}><option value="">Select location…</option>{locations.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>}
  </>;
}

export function AzureResourceDropdown({ label, connectionId, subscriptionId, resourceGroup, types, kinds = [], value, valueMode = "id", disabled = false, onChange }: {
  label: string;
  connectionId: string;
  subscriptionId: string;
  resourceGroup: string;
  types: string[];
  kinds?: string[];
  value: string;
  valueMode?: "id" | "name" | "leaf_name" | "workspace_id";
  disabled?: boolean;
  onChange: (value: string, resource?: AlertsAuthoringOptions["resources"][number]) => void;
}) {
  const optionsQ = useAlertsAuthoringOptions(connectionId, subscriptionId, resourceGroup);
  const wanted = new Set(types.map((item) => item.toLowerCase()));
  const wantedKinds = kinds.map((item) => item.toLowerCase());
  const resources = (optionsQ.data?.resources || []).filter((item) => wanted.has(item.type) && (!wantedKinds.length || wantedKinds.some((kind) => item.kind.includes(kind))));
  const optionValue = (item: AlertsAuthoringOptions["resources"][number]) => valueMode === "name" ? item.name : valueMode === "leaf_name" ? item.name.split("/").at(-1) || item.name : valueMode === "workspace_id" ? item.workspace_id : item.id;
  return <label>{label}<select aria-label={label} disabled={disabled || !subscriptionId || !resourceGroup || optionsQ.isFetching} value={value} onChange={(event) => onChange(event.target.value, resources.find((item) => optionValue(item) === event.target.value))} className={control}><option value="">{!subscriptionId ? "Select subscription first" : !resourceGroup ? "Select resource group first" : optionsQ.isFetching ? `Loading ${label.toLowerCase()}…` : `Select ${label.toLowerCase()}…`}</option>{value && !resources.some((item) => optionValue(item) === value) && <option value={value}>{value}</option>}{resources.map((item) => <option key={item.id} value={optionValue(item)}>{item.name}{item.location ? ` · ${item.location}` : ""}</option>)}</select></label>;
}
