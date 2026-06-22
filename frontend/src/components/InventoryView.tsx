import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { keepPreviousData, useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type InventoryCost,
  type InventoryFilter,
  type InventoryResource,
  type InventoryResponse,
  type InventoryOptimization,
} from "../api";
import { AzureIcon, friendlyLocation, friendlyResourceType } from "./AzureIcon";
import { INVENTORY_NAV, type InventoryTab } from "./navConfig";
import { LocationMode, LocationFilterToolbar } from "./InventoryLocationMap";
import { DnsDebugModal } from "./DnsDebugModal";
import { ConnectionScopePicker } from "./ConnectionScopePicker";

const FLAG_META: Record<string, { label: string; tone: string }> = {
  untagged: { label: "Untagged", tone: "bg-gray-100 text-gray-600" },
  unattached_disk: { label: "Unattached disk", tone: "bg-amber-100 text-amber-700" },
  orphaned_nic: { label: "Orphaned NIC", tone: "bg-amber-100 text-amber-700" },
  idle_public_ip: { label: "Idle public IP", tone: "bg-amber-100 text-amber-700" },
};
const CLEANUP_FLAGS = ["unattached_disk", "orphaned_nic", "idle_public_ip"];

// Inventory/cost data older than this (6 hours) is flagged stale (bold red).
const STALE_SECONDS = 6 * 3600;

/** Friendly "how long ago" label for a cached payload's age in seconds. */
function ageLabel(seconds?: number): string {
  if (seconds === undefined) return "just now";
  if (seconds < 45) return "just now";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h ago`;
}

/** Azure Inventory: one filterable grid of every resource across workloads (or the whole
 * tenant), with natural-language search and a per-resource detail drawer. Read-only. The
 * active sub-tab is driven by the /inventory/:tab URL so a browser refresh restores it. */
export function InventoryPanel({ tab = "grid" }: { tab?: InventoryTab }) {
  const qc = useQueryClient();
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections, retry: false });
  const connections = connQ.data?.connections ?? [];
  const [connectionId, setConnectionId] = useState("");
  const effectiveConn = connectionId || connections.find((c) => c.is_default)?.id || "";

  const invQ = useQuery({
    queryKey: ["inventory", effectiveConn],
    queryFn: () => api.inventory(effectiveConn || null, false),
    enabled: !connQ.isLoading,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const inv = invQ.data;

  // The force re-collect (a slow Azure scan) runs as a React Query MUTATION rather than a
  // local useState flag, so its in-flight status survives navigating away and back: the
  // mutation lives in the MutationCache (not the component), and useIsMutating below reads
  // that cache. The cache write happens INSIDE the mutationFn so a result still lands even
  // if the user is on another screen when the scan finishes.
  const refreshMutation = useMutation({
    mutationKey: ["inventory-refresh", effectiveConn],
    mutationFn: async (conn: string) => {
      const fresh = await api.inventory(conn || null, true);
      qc.setQueryData(["inventory", conn], fresh);
      return fresh;
    },
    onError: () => {
      // Clear any half-applied state so a return visit reads the cached snapshot cleanly.
      void qc.invalidateQueries({ queryKey: ["inventory", effectiveConn] });
    },
  });
  // Count of in-flight refreshes for THIS connection — survives unmount/remount.
  const isRefreshing = useIsMutating({ mutationKey: ["inventory-refresh", effectiveConn] }) > 0;

  function refresh() {
    if (isRefreshing) return; // re-entrancy guard (the button is also disabled while busy)
    refreshMutation.mutate(effectiveConn);
  }

  const busy = isRefreshing || invQ.isFetching;

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <Header
        inv={inv}
        connectionId={effectiveConn}
        onConnection={setConnectionId}
        refreshing={busy}
        onRefresh={refresh}
      />
      <div className="min-h-0 flex-1 overflow-hidden">
        {invQ.isError ? (
          <div className="m-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {(invQ.error as Error)?.message || "Failed to load inventory."}
          </div>
        ) : inv?.never_loaded ? (
          <div className="mx-auto max-w-2xl px-6 py-16 text-center">
            <div className="text-3xl">🗂️</div>
            <h2 className="mt-2 text-base font-semibold text-gray-900">Inventory not loaded yet</h2>
            <p className="mt-1 text-sm text-gray-500">
              Scanning every subscription with Azure Resource Graph and attributing each resource to its
              workload takes a moment, so it doesn&apos;t run automatically. Press Refresh to collect the
              inventory — it&apos;s then cached until you refresh again.
            </p>
            <button
              onClick={refresh}
              disabled={busy}
              className="mt-4 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
            >
              {busy ? "Refreshing…" : "↻ Refresh inventory"}
            </button>
          </div>
        ) : !inv && invQ.isLoading ? (
          <div className="flex h-full items-center justify-center text-sm text-gray-400">Loading inventory…</div>
        ) : inv ? (
          <InventoryBody key={effectiveConn} inv={inv} connectionId={effectiveConn} refreshing={busy} tab={tab} />
        ) : null}
      </div>
    </div>
  );
}

// =========================================================================== Header
function Header({
  inv,
  connectionId,
  onConnection,
  refreshing,
  onRefresh,
}: {
  inv?: InventoryResponse;
  connectionId: string;
  onConnection: (id: string) => void;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const s = inv?.summary;
  return (
    <div className="border-b bg-white px-5 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-2xl">🗂️</span>
          <div>
            <h1 className="text-lg font-bold text-gray-800">Inventory</h1>
            <p className="text-xs text-gray-500">Every Azure resource across your workloads</p>
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <ConnectionScopePicker value={connectionId} onChange={onConnection} />
          <button
            onClick={onRefresh}
            disabled={refreshing}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            ↻ Refresh
          </button>
          {refreshing && <span className="animate-pulse text-xs font-medium text-brand">Refreshing…</span>}
          {inv?.fetched_at && !refreshing && (() => {
            const stale = (inv.age_seconds ?? 0) > STALE_SECONDS;
            return (
              <span
                className={`text-[11px] ${stale ? "font-bold text-red-600" : "text-gray-400"}`}
                title={`Collected ${new Date(inv.fetched_at).toLocaleString()}${stale ? " — over 6 hours ago; refresh recommended" : ""}`}
              >
                Updated {ageLabel(inv.age_seconds)}
              </span>
            );
          })()}
        </div>
      </div>
      {s && !inv?.never_loaded && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-500">
          <Stat label="resources" value={s.total_resources} />
          <Stat label="types" value={s.type_count} />
          <Stat label="subscriptions" value={s.subscription_count} />
          <Stat label="resource groups" value={s.resource_group_count} />
          <Stat label="locations" value={s.location_count} />
          <Stat label="workloads" value={s.workload_count} />
          {s.unassigned_count > 0 && (
            <span className="text-gray-400">
              · <b className="text-gray-600">{s.unassigned_count}</b> not in any workload
            </span>
          )}
          {s.truncated_subscriptions.length > 0 && (
            <span className="text-amber-600" title={s.truncated_subscriptions.join(", ")}>
              ⚠ some subscriptions truncated at 1000
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <span>
      <b className="text-gray-700">{value.toLocaleString()}</b> {label}
    </span>
  );
}

// =========================================================================== Body
type ScopeMode = "workloads" | "tenant";
// Facet dimensions used by the cascading filter (each can exclude itself when tallying).
type Dim = "type" | "loc" | "sub" | "rg" | "wl";
type GroupKey = "none" | "type" | "resource_group" | "subscription_id" | "location" | "workload";
type SortKey = "name" | "type" | "resource_group" | "location" | "subscription" | "cost";
type ColKey = "type" | "resource_group" | "location" | "subscription" | "sku" | "tags" | "flags" | "cost" | "workloads";
const DEFAULT_COLS: ColKey[] = ["type", "resource_group", "location", "subscription", "workloads"];
const ALL_COLS: { key: ColKey; label: string }[] = [
  { key: "type", label: "Type" },
  { key: "resource_group", label: "Resource group" },
  { key: "location", label: "Location" },
  { key: "subscription", label: "Subscription" },
  { key: "sku", label: "SKU" },
  { key: "tags", label: "Tags" },
  { key: "flags", label: "Health" },
  { key: "cost", label: "Cost (30d)" },
  { key: "workloads", label: "Workloads" },
];

// Build sorted facet rows from a tally; always keep currently-selected keys visible (count 0
// if they fell out) so the user can still deselect them.
function valueRows(map: Map<string, number>, selected: Set<string>): { key: string; count: number }[] {
  const out: { key: string; count: number }[] = [];
  const seen = new Set<string>();
  for (const [k, c] of map) { out.push({ key: k, count: c }); seen.add(k); }
  for (const k of selected) if (!seen.has(k) && k !== "__unassigned__") out.push({ key: k, count: 0 });
  out.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
  return out;
}

function InventoryBody({ inv, connectionId, refreshing, tab }: { inv: InventoryResponse; connectionId: string; refreshing: boolean; tab: InventoryTab }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const view = tab;
  const setView = (v: InventoryTab) => navigate(v === "grid" ? "/inventory" : `/inventory/${v}`);
  const [scope, setScope] = useState<ScopeMode>("tenant");
  const [typeSel, setTypeSel] = useState<Set<string>>(new Set());
  const [locSel, setLocSel] = useState<Set<string>>(new Set());
  const [subSel, setSubSel] = useState<Set<string>>(new Set());
  const [rgSel, setRgSel] = useState<Set<string>>(new Set());
  const [wlSel, setWlSel] = useState<Set<string>>(new Set());
  const [flagSel, setFlagSel] = useState<Set<string>>(new Set());
  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [text, setText] = useState("");
  const [skuContains, setSkuContains] = useState<string[]>([]);
  const [kqlIds, setKqlIds] = useState<Set<string> | null>(null);
  const [kqlText, setKqlText] = useState("");
  const [selected, setSelected] = useState<InventoryResource | null>(null);
  // Grid display controls (Theme 1 + 6)
  const [groupBy, setGroupBy] = useState<GroupKey>("none");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [density, setDensity] = useState<"comfortable" | "compact">("comfortable");
  const [cols, setCols] = useState<Set<ColKey>>(new Set(DEFAULT_COLS));
  const [picked, setPicked] = useState<Set<string>>(new Set()); // bulk selection (resource ids)

  // Cost overlay (Theme 4). Auto-restores the SERVER-cached cost on mount (cached_only never
  // runs the slow Azure query); "Load cost" / "Refresh cost" run the full/forced query. Shared
  // by the grid's Cost (30d) column and the Cost tab (which rolls it up over the filtered set).
  const costQ = useQuery({
    queryKey: ["inventoryCost", connectionId],
    queryFn: () => api.inventoryCost(connectionId || null, false, true),
    retry: false,
    staleTime: Infinity,
  });
  const cost = costQ.data;
  const [costLoading, setCostLoading] = useState(false);
  async function loadCost(force: boolean) {
    setCostLoading(true);
    try {
      const data = await api.inventoryCost(connectionId || null, force, false);
      qc.setQueryData(["inventoryCost", connectionId], data);
    } finally {
      setCostLoading(false);
    }
  }

  const subName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const sb of inv.facets.subscriptions) m[sb.key] = sb.name;
    return m;
  }, [inv.facets.subscriptions]);

  function clearAll() {
    setTypeSel(new Set());
    setLocSel(new Set());
    setSubSel(new Set());
    setRgSel(new Set());
    setWlSel(new Set());
    setFlagSel(new Set());
    setTagKey("");
    setTagValue("");
    setText("");
    setSkuContains([]);
    setKqlIds(null);
    setKqlText("");
  }

  // Apply an AI-parsed structured filter to the live filter controls.
  function applyFilter(f: InventoryFilter) {
    setTypeSel(new Set((f.types ?? []).map((t) => t.toLowerCase())));
    setLocSel(new Set((f.locations ?? []).map((t) => t.toLowerCase())));
    setSubSel(new Set(f.subscriptions ?? []));
    setRgSel(new Set(f.resource_groups ?? []));
    // Map workload names → ids.
    const byName: Record<string, string> = {};
    for (const w of inv.facets.workloads) byName[w.name.toLowerCase()] = w.id;
    setWlSel(new Set((f.workloads ?? []).map((n) => byName[n.toLowerCase()]).filter(Boolean) as string[]));
    setTagKey(f.tag_key ?? "");
    setTagValue(f.tag_value ?? "");
    setText(f.text ?? "");
    setSkuContains(f.sku_contains ?? []);
    setKqlIds(null);
    setKqlText("");
  }

  // A single predicate, optionally excluding ONE facet dimension. The grid uses the full
  // predicate; each facet's options are tallied with its own dimension excluded so picking a
  // workload narrows type/location/subscription/RG, picking a region narrows them again, and
  // the Workload list is never collapsed by its own selection.
  const matches = useCallback(
    (r: InventoryResource, except?: Dim) => {
      if (scope === "workloads" && r.workloads.length === 0 && !wlSel.has("__unassigned__")) return false;
      if (except !== "type" && typeSel.size && !typeSel.has(r.type)) return false;
      if (except !== "loc" && locSel.size && !locSel.has(r.location)) return false;
      if (except !== "sub" && subSel.size && !subSel.has(r.subscription_id)) return false;
      if (except !== "rg" && rgSel.size && !rgSel.has(r.resource_group)) return false;
      if (except !== "wl" && wlSel.size) {
        const unassignedHit = wlSel.has("__unassigned__") && r.workloads.length === 0;
        const wlHit = r.workloads.some((w) => wlSel.has(w.id));
        if (!unassignedHit && !wlHit) return false;
      }
      if (tagKey) {
        if (!(tagKey in r.tags)) return false;
        if (tagValue && r.tags[tagKey] !== tagValue) return false;
      }
      if (flagSel.size) {
        if (flagSel.has("__cleanup__")) {
          if (!r.flags.some((f) => CLEANUP_FLAGS.includes(f))) return false;
        } else if (!r.flags.some((f) => flagSel.has(f))) {
          return false;
        }
      }
      const t = text.trim().toLowerCase();
      if (t && !r.name.toLowerCase().includes(t) && !r.id.toLowerCase().includes(t)) return false;
      if (skuContains.length) {
        const hay = `${r.sku} ${r.size} ${r.tier}`.toLowerCase();
        if (!skuContains.some((s) => hay.includes(s.toLowerCase()))) return false;
      }
      if (kqlIds && !kqlIds.has(r.id.toLowerCase())) return false;
      return true;
    },
    [scope, typeSel, locSel, subSel, rgSel, wlSel, flagSel, tagKey, tagValue, text, skuContains, kqlIds],
  );

  const filtered = useMemo(() => inv.resources.filter((r) => matches(r)), [inv.resources, matches]);

  // For the Location map: every filter EXCEPT location, so ALL regions stay plotted (and
  // the selected ones are highlighted) while selecting a region narrows the rest of the UI.
  const mapResources = useMemo(() => inv.resources.filter((r) => matches(r, "loc")), [inv.resources, matches]);

  // Cascading facet options: tally each dimension over resources passing every OTHER active
  // filter (its own selection excluded).
  const dyn = useMemo(() => {
    const types = new Map<string, number>();
    const locs = new Map<string, number>();
    const subs = new Map<string, number>();
    const rgs = new Map<string, number>();
    const wls = new Map<string, number>();
    let unassigned = 0;
    for (const r of inv.resources) {
      if (matches(r, "type")) types.set(r.type, (types.get(r.type) || 0) + 1);
      if (matches(r, "loc") && r.location) locs.set(r.location, (locs.get(r.location) || 0) + 1);
      if (matches(r, "sub")) subs.set(r.subscription_id, (subs.get(r.subscription_id) || 0) + 1);
      if (matches(r, "rg") && r.resource_group) rgs.set(r.resource_group, (rgs.get(r.resource_group) || 0) + 1);
      if (matches(r, "wl")) {
        if (r.workloads.length === 0) unassigned += 1;
        else for (const w of r.workloads) wls.set(w.id, (wls.get(w.id) || 0) + 1);
      }
    }
    return { types, locs, subs, rgs, wls, unassigned };
  }, [inv.resources, matches]);

  const typeRows = useMemo(() => valueRows(dyn.types, typeSel), [dyn.types, typeSel]);
  const locRows = useMemo(() => valueRows(dyn.locs, locSel), [dyn.locs, locSel]);
  const subRows = useMemo(() => valueRows(dyn.subs, subSel), [dyn.subs, subSel]);
  const rgRows = useMemo(() => valueRows(dyn.rgs, rgSel), [dyn.rgs, rgSel]);
  // Workloads stay fully visible (greyed at 0) so selecting one never hides the others.
  const wlRows = useMemo(
    () => inv.facets.workloads.map((w) => ({ id: w.id, name: w.name, count: dyn.wls.get(w.id) || 0 })),
    [inv.facets.workloads, dyn.wls],
  );

  const costByRes = useMemo(() => cost?.by_resource ?? {}, [cost]);
  const cleanupCount = useMemo(
    () => inv.resources.filter((r) => r.flags.some((f) => CLEANUP_FLAGS.includes(f))).length,
    [inv.resources],
  );

  // Sort the filtered rows for the grid.
  const sorted = useMemo(() => {
    const arr = [...filtered];
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      let av: string | number = "";
      let bv: string | number = "";
      if (sortKey === "cost") {
        av = costByRes[a.id.toLowerCase()] ?? -1;
        bv = costByRes[b.id.toLowerCase()] ?? -1;
      } else if (sortKey === "subscription") {
        av = subName[a.subscription_id] || a.subscription_id;
        bv = subName[b.subscription_id] || b.subscription_id;
      } else {
        av = (a as unknown as Record<string, string>)[sortKey] || "";
        bv = (b as unknown as Record<string, string>)[sortKey] || "";
      }
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    return arr;
  }, [filtered, sortKey, sortDir, costByRes, subName]);

  const hasFilters =
    typeSel.size || locSel.size || subSel.size || rgSel.size || wlSel.size || flagSel.size || tagKey || text || skuContains.length || kqlIds;

  // Bundle of filter state + setters shared by the chips bar on both the Grid and Cost tabs,
  // so any active filter can be removed one-by-one from either view.
  const wlNameMap = useMemo(() => Object.fromEntries(inv.facets.workloads.map((w) => [w.id, w.name])), [inv.facets.workloads]);
  const chipProps: FilterChipsProps = {
    kqlText, setKqlIds, setKqlText,
    flagSel, setFlagSel,
    typeSel, setTypeSel, locSel, setLocSel,
    subSel, subName, setSubSel,
    rgSel, setRgSel,
    wlSel, setWlSel, wlName: wlNameMap,
    skuContains, setSkuContains,
    tagKey, tagValue, clearTag: () => { setTagKey(""); setTagValue(""); },
  };

  function toggle(set: Set<string>, key: string, setter: (s: Set<string>) => void) {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setter(next);
  }

  return (
    <div className="flex h-full min-h-0">
      {/* Filters sidebar */}
      <aside className="hidden w-60 shrink-0 overflow-y-auto border-r bg-white px-3 py-3 lg:block">
        <div className="mb-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Scope</div>
          <div className="flex rounded-lg border p-0.5 text-xs">
            <button
              onClick={() => setScope("workloads")}
              className={`flex-1 rounded-md px-2 py-1 ${scope === "workloads" ? "bg-brand text-white" : "text-gray-600"}`}
            >
              🧩 Workloads
            </button>
            <button
              onClick={() => setScope("tenant")}
              className={`flex-1 rounded-md px-2 py-1 ${scope === "tenant" ? "bg-brand text-white" : "text-gray-600"}`}
            >
              🌐 Tenant
            </button>
          </div>
        </div>

        {/* Health / cleanup smart filters (Theme 2) */}
        <FacetGroup title="Health">
          {cleanupCount > 0 && (
            <FacetRow
              label="🧹 Cleanup candidates"
              count={cleanupCount}
              active={flagSel.has("__cleanup__")}
              onClick={() => toggle(flagSel, "__cleanup__", setFlagSel)}
            />
          )}
          {Object.entries(inv.summary.flag_counts || {}).map(([f, c]) => (
            <FacetRow
              key={f}
              label={FLAG_META[f]?.label || f}
              count={c}
              active={flagSel.has(f)}
              onClick={() => toggle(flagSel, f, setFlagSel)}
            />
          ))}
        </FacetGroup>

        <FacetGroup title="Workload">
          {wlRows.map((w) => (
            <FacetRow
              key={w.id}
              label={`🧩 ${w.name}`}
              count={w.count}
              active={wlSel.has(w.id)}
              dimmed={w.count === 0 && !wlSel.has(w.id)}
              onClick={() => toggle(wlSel, w.id, setWlSel)}
            />
          ))}
          {inv.facets.unassigned_count > 0 && (
            <FacetRow
              label="— Not in any workload"
              count={dyn.unassigned}
              active={wlSel.has("__unassigned__")}
              dimmed={dyn.unassigned === 0 && !wlSel.has("__unassigned__")}
              onClick={() => toggle(wlSel, "__unassigned__", setWlSel)}
            />
          )}
        </FacetGroup>

        <FacetGroup title="Resource type" max={12}>
          {typeRows.map((tp) => (
            <FacetRow
              key={tp.key}
              label={friendlyResourceType(tp.key)}
              count={tp.count}
              active={typeSel.has(tp.key)}
              onClick={() => toggle(typeSel, tp.key, setTypeSel)}
              icon={<AzureIcon kind="resource" type={tp.key} className="h-3.5 w-3.5" />}
            />
          ))}
        </FacetGroup>

        <FacetGroup title="Location" max={10}>
          {locRows.map((l) => (
            <FacetRow
              key={l.key}
              label={friendlyLocation(l.key)}
              count={l.count}
              active={locSel.has(l.key)}
              onClick={() => toggle(locSel, l.key, setLocSel)}
            />
          ))}
        </FacetGroup>

        <FacetGroup title="Subscription" max={8}>
          {subRows.map((sb) => (
            <FacetRow
              key={sb.key}
              label={subName[sb.key] || sb.key}
              count={sb.count}
              active={subSel.has(sb.key)}
              onClick={() => toggle(subSel, sb.key, setSubSel)}
            />
          ))}
        </FacetGroup>

        <FacetGroup title="Resource group" max={10}>
          {rgRows.map((g) => (
            <FacetRow
              key={g.key}
              label={g.key}
              count={g.count}
              active={rgSel.has(g.key)}
              onClick={() => toggle(rgSel, g.key, setRgSel)}
            />
          ))}
        </FacetGroup>
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* View switcher */}
        <div className="flex items-center gap-1 border-b bg-white px-4 pt-2">
          {INVENTORY_NAV.map(({ id: v, label }) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`rounded-t-lg px-3 py-1.5 text-sm font-medium ${view === v ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"}`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Location filter toolbar — sits just below the tabs; drives the real inventory
            filters so the left facet menu + grid + every tab narrow to the selection. */}
        {view === "location" && (
          <LocationFilterToolbar
            resources={inv.resources}
            selectedLocations={locSel}
            onToggleLocation={(loc) => toggle(locSel, loc, setLocSel)}
            onClearLocations={() => setLocSel(new Set())}
            workloadName={wlNameMap}
            selectedWorkloads={wlSel}
            onToggleWorkload={(id) => toggle(wlSel, id, setWlSel)}
          />
        )}

        {view === "overview" ? (
          <OverviewMode inv={inv} connectionId={connectionId} />
        ) : view === "location" ? (
          <LocationMode
            resources={mapResources}
            selectedLocations={locSel}
            onToggleLocation={(loc) => toggle(locSel, loc, setLocSel)}
            onClear={() => setLocSel(new Set())}
            resourceGroups={rgRows}
            selectedRGs={rgSel}
            onToggleRG={(rg) => toggle(rgSel, rg, setRgSel)}
            types={typeRows}
            selectedTypes={typeSel}
            onToggleType={(t) => toggle(typeSel, t, setTypeSel)}
            subscriptions={subRows}
            selectedSubs={subSel}
            onToggleSub={(s) => toggle(subSel, s, setSubSel)}
            subName={subName}
          />
        ) : view === "cost" ? (
          <CostMode
            cost={cost}
            resources={filtered}
            subName={subName}
            hasFilters={!!hasFilters}
            chipProps={chipProps}
            onClearFilters={clearAll}
            onLoadCost={loadCost}
            loading={costLoading}
          />
        ) : view === "changes" ? (
          <ChangesMode connectionId={connectionId} subName={subName} />
        ) : view === "optimization" ? (
          <OptimizationMode connectionId={connectionId} onLoadCost={loadCost} costLoading={costLoading} />
        ) : (
          <>
            <div className="border-b bg-white px-4 py-2.5">
              <NlSearchBar
                connectionId={connectionId}
                facets={inv.facets}
                onFilter={applyFilter}
                onKql={(ids, q) => {
                  clearAll();
                  setKqlIds(new Set(ids.map((i) => i.toLowerCase())));
                  setKqlText(q);
                }}
              />
              {/* toolbar: text search + view controls + active filters */}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <input
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="Filter by name…"
                  className="w-44 rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm"
                />
                <span className="text-xs text-gray-500">
                  <b className="text-gray-700">{filtered.length.toLocaleString()}</b> of {inv.resources.length.toLocaleString()}
                </span>
                {hasFilters ? (
                  <button onClick={clearAll} className="rounded-md border px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-50">
                    Clear filters
                  </button>
                ) : null}

                <div className="ml-auto flex items-center gap-1.5">
                  <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupKey)} title="Group by" className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600">
                    <option value="none">No grouping</option>
                    <option value="type">Group: Type</option>
                    <option value="resource_group">Group: Resource group</option>
                    <option value="subscription_id">Group: Subscription</option>
                    <option value="location">Group: Location</option>
                    <option value="workload">Group: Workload</option>
                  </select>
                  <ColumnChooser cols={cols} setCols={setCols} />
                  <button onClick={() => setDensity((d) => (d === "compact" ? "comfortable" : "compact"))} title="Toggle density" className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">
                    {density === "compact" ? "↕ Compact" : "↕ Cozy"}
                  </button>
                  {!cost?.available && (
                    <button onClick={() => loadCost(false)} disabled={costLoading} title="Load last-30-days cost" className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                      {costLoading ? "💲 Loading…" : "💲 Load cost"}
                    </button>
                  )}
                  <button onClick={() => exportCsv(sorted, subName, costByRes)} title="Export current view to CSV" className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">
                    ⬇ Export
                  </button>
                </div>
              </div>

              {(kqlText || hasFilters) && (
                <div className="mt-2">
                  <FilterChipsBar p={chipProps} hasFilters={!!hasFilters} />
                </div>
              )}
            </div>

            {picked.size > 0 && (
              <BulkBar
                count={picked.size}
                onClear={() => setPicked(new Set())}
                onExport={() => exportCsv(sorted.filter((r) => picked.has(r.id)), subName, costByRes)}
              />
            )}

            <Grid
              resources={sorted}
              subName={subName}
              cols={cols}
              density={density}
              groupBy={groupBy}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={(k) => { if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc")); else { setSortKey(k); setSortDir("asc"); } }}
              costByRes={costByRes}
              costCurrency={cost?.currency || "USD"}
              picked={picked}
              onPick={(id) => { const n = new Set(picked); if (n.has(id)) n.delete(id); else n.add(id); setPicked(n); }}
              onPickAll={(ids, on) => { const n = new Set(picked); if (on) ids.forEach((i) => n.add(i)); else ids.forEach((i) => n.delete(i)); setPicked(n); }}
              selected={selected}
              onSelect={setSelected}
              refreshing={refreshing}
            />
          </>
        )}
      </div>

      {selected && (
        <DetailDrawer resource={selected} subName={subName} connectionId={connectionId} cost={costByRes[selected.id.toLowerCase()]} costCurrency={cost?.currency || "USD"} costLoaded={!!cost?.available} onLoadCost={() => loadCost(false)} costLoading={costLoading} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

// =========================================================================== NL search
function NlSearchBar({
  connectionId,
  facets,
  onFilter,
  onKql,
}: {
  connectionId: string;
  facets: InventoryResponse["facets"];
  onFilter: (f: InventoryFilter) => void;
  onKql: (ids: string[], kql: string) => void;
}) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  // Abort any in-flight NL search when the component unmounts (e.g. tab/connection switch).
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  async function run() {
    if (!q.trim() || busy) return;
    setBusy(true);
    setNote("");
    // Bound the AI call so a hung backend can't leave the button spinning forever.
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const timeout = window.setTimeout(() => ctrl.abort(), 45_000);
    try {
      const res = await api.inventoryNlSearch({
        query: q,
        connection_id: connectionId,
        types: facets.types.map((t) => t.key),
        locations: facets.locations.map((l) => l.key),
        workloads: facets.workloads.map((w) => w.name),
        subscriptions: facets.subscriptions.map((s) => `${s.key}:${s.name}`),
      }, ctrl.signal);
      setNote(res.explanation || "");
      if (res.mode === "kql" && res.matched_ids) onKql(res.matched_ids, res.kql || "");
      else if (res.filter) onFilter(res.filter);
    } catch (e) {
      // A timeout/abort surfaces as an AbortError — show a friendly, actionable message.
      const aborted = (e as Error)?.name === "AbortError" || ctrl.signal.aborted;
      setNote(aborted ? "Search timed out — try a simpler query or the filters above." : ((e as Error)?.message || "Search failed."));
    } finally {
      window.clearTimeout(timeout);
      abortRef.current = null;
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-gray-400">✨</span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder='Ask in plain English — e.g. "all virtual machines in eastus with D-series SKU"'
            className="w-full rounded-lg border border-gray-200 py-2 pl-9 pr-3 text-sm focus:border-brand focus:outline-none"
          />
        </div>
        <button
          onClick={run}
          disabled={busy || !q.trim()}
          className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
        >
          {busy ? "Searching…" : "Search"}
        </button>
      </div>
      {note && <div className="mt-1 text-[11px] text-gray-500">💡 {note}</div>}
    </div>
  );
}

// =========================================================================== Active chips
function Chip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
      {label}
      <button onClick={onRemove} className="text-brand/60 hover:text-brand">✕</button>
    </span>
  );
}

function ActiveChips(props: {
  typeSel: Set<string>; setTypeSel: (s: Set<string>) => void;
  locSel: Set<string>; setLocSel: (s: Set<string>) => void;
  subSel: Set<string>; subName: Record<string, string>; setSubSel: (s: Set<string>) => void;
  rgSel: Set<string>; setRgSel: (s: Set<string>) => void;
  wlSel: Set<string>; setWlSel: (s: Set<string>) => void; wlName: Record<string, string>;
  skuContains: string[]; setSkuContains: (s: string[]) => void;
  tagKey: string; tagValue: string; clearTag: () => void;
}) {
  const rm = (set: Set<string>, key: string, setter: (s: Set<string>) => void) => () => {
    const n = new Set(set); n.delete(key); setter(n);
  };
  return (
    <>
      {[...props.typeSel].map((t) => <Chip key={`t${t}`} label={friendlyResourceType(t)} onRemove={rm(props.typeSel, t, props.setTypeSel)} />)}
      {[...props.locSel].map((l) => <Chip key={`l${l}`} label={friendlyLocation(l)} onRemove={rm(props.locSel, l, props.setLocSel)} />)}
      {[...props.subSel].map((s) => <Chip key={`s${s}`} label={props.subName[s] || s} onRemove={rm(props.subSel, s, props.setSubSel)} />)}
      {[...props.rgSel].map((g) => <Chip key={`g${g}`} label={g} onRemove={rm(props.rgSel, g, props.setRgSel)} />)}
      {[...props.wlSel].map((w) => <Chip key={`w${w}`} label={w === "__unassigned__" ? "Unassigned" : `🧩 ${props.wlName[w] || w}`} onRemove={rm(props.wlSel, w, props.setWlSel)} />)}
      {props.skuContains.map((s, i) => <Chip key={`k${i}`} label={`SKU ~ ${s}`} onRemove={() => props.setSkuContains(props.skuContains.filter((_, j) => j !== i))} />)}
      {props.tagKey && <Chip label={`tag ${props.tagKey}${props.tagValue ? `=${props.tagValue}` : ""}`} onRemove={props.clearTag} />}
    </>
  );
}

// Active-filter chips row (KQL + cleanup/flags + facets), each individually removable. Shared
// by the Grid and Cost tabs so filters can be removed one-by-one from either view.
type FilterChipsProps = {
  kqlText: string; setKqlIds: (s: Set<string> | null) => void; setKqlText: (s: string) => void;
  flagSel: Set<string>; setFlagSel: (s: Set<string>) => void;
  typeSel: Set<string>; setTypeSel: (s: Set<string>) => void;
  locSel: Set<string>; setLocSel: (s: Set<string>) => void;
  subSel: Set<string>; subName: Record<string, string>; setSubSel: (s: Set<string>) => void;
  rgSel: Set<string>; setRgSel: (s: Set<string>) => void;
  wlSel: Set<string>; setWlSel: (s: Set<string>) => void; wlName: Record<string, string>;
  skuContains: string[]; setSkuContains: (s: string[]) => void;
  tagKey: string; tagValue: string; clearTag: () => void;
};
function FilterChipsBar({ p, hasFilters, onClearAll }: { p: FilterChipsProps; hasFilters: boolean; onClearAll?: () => void }) {
  const toggleFlag = (key: string) => { const n = new Set(p.flagSel); if (n.has(key)) n.delete(key); else n.add(key); p.setFlagSel(n); };
  return (
    <div className="flex flex-wrap items-center gap-2">
      {p.kqlText && (
        <span className="flex items-center gap-1 rounded-md bg-violet-50 px-2 py-0.5 text-[11px] text-violet-700">
          🔧 KQL
          <code className="max-w-[28rem] truncate font-mono">{p.kqlText}</code>
          <button onClick={() => { p.setKqlIds(null); p.setKqlText(""); }} className="text-violet-400 hover:text-violet-600">✕</button>
        </span>
      )}
      {p.flagSel.has("__cleanup__") && <Chip label="🧹 Cleanup candidates" onRemove={() => toggleFlag("__cleanup__")} />}
      {[...p.flagSel].filter((f) => f !== "__cleanup__").map((f) => <Chip key={f} label={FLAG_META[f]?.label || f} onRemove={() => toggleFlag(f)} />)}
      <ActiveChips
        typeSel={p.typeSel} setTypeSel={p.setTypeSel}
        locSel={p.locSel} setLocSel={p.setLocSel}
        subSel={p.subSel} subName={p.subName} setSubSel={p.setSubSel}
        rgSel={p.rgSel} setRgSel={p.setRgSel}
        wlSel={p.wlSel} setWlSel={p.setWlSel} wlName={p.wlName}
        skuContains={p.skuContains} setSkuContains={p.setSkuContains}
        tagKey={p.tagKey} tagValue={p.tagValue} clearTag={p.clearTag}
      />
      {hasFilters && onClearAll && (
        <button onClick={onClearAll} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear filters</button>
      )}
    </div>
  );
}

// =========================================================================== Grid
function moneyFmt(v: number | undefined, cur: string): string {
  if (v === undefined) return "—";
  return `${cur === "USD" ? "$" : ""}${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}${cur !== "USD" ? " " + cur : ""}`;
}

function Grid({
  resources,
  subName,
  cols,
  density,
  groupBy,
  sortKey,
  sortDir,
  onSort,
  costByRes,
  costCurrency,
  picked,
  onPick,
  onPickAll,
  selected,
  onSelect,
  refreshing,
}: {
  resources: InventoryResource[];
  subName: Record<string, string>;
  cols: Set<ColKey>;
  density: "comfortable" | "compact";
  groupBy: GroupKey;
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (k: SortKey) => void;
  costByRes: Record<string, number>;
  costCurrency: string;
  picked: Set<string>;
  onPick: (id: string) => void;
  onPickAll: (ids: string[], on: boolean) => void;
  selected: InventoryResource | null;
  onSelect: (r: InventoryResource) => void;
  refreshing: boolean;
}) {
  const [limit, setLimit] = useState(300);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  useEffect(() => { setLimit(300); }, [resources, groupBy]);

  const groupLabel = (r: InventoryResource): { key: string; label: string } => {
    if (groupBy === "type") return { key: r.type, label: friendlyResourceType(r.type) };
    if (groupBy === "resource_group") return { key: r.resource_group, label: r.resource_group || "(none)" };
    if (groupBy === "subscription_id") return { key: r.subscription_id, label: subName[r.subscription_id] || r.subscription_id };
    if (groupBy === "location") return { key: r.location, label: r.location ? friendlyLocation(r.location) : "(none)" };
    if (groupBy === "workload") return { key: r.workloads[0]?.id || "__none__", label: r.workloads[0]?.name ? `🧩 ${r.workloads[0].name}` : "Not in any workload" };
    return { key: "", label: "" };
  };

  const pad = density === "compact" ? "py-1" : "py-2";
  const pageIds = resources.slice(0, limit).map((r) => r.id);
  const allPickedOnPage = pageIds.length > 0 && pageIds.every((id) => picked.has(id));

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 400 && limit < resources.length) {
      setLimit((n) => Math.min(n + 300, resources.length));
    }
  }

  if (resources.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
        {refreshing ? "Loading…" : "No resources match your filters."}
      </div>
    );
  }

  const colCount = 2 + cols.size; // checkbox + name + visible cols

  function SortTh({ k, label }: { k: SortKey; label: string }) {
    return (
      <th className={`px-2 ${pad} cursor-pointer select-none text-left font-semibold hover:text-gray-700`} onClick={() => onSort(k)}>
        {label}{sortKey === k ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
      </th>
    );
  }

  function Row({ r }: { r: InventoryResource }) {
    const c = costByRes[r.id.toLowerCase()];
    return (
      <tr onClick={() => onSelect(r)} className={`group cursor-pointer ${selected?.id === r.id ? "bg-brand/5" : picked.has(r.id) ? "bg-brand/[0.03]" : "hover:bg-gray-50"}`}>
        <td className={`px-2 ${pad} w-8`} onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={picked.has(r.id)} onChange={() => onPick(r.id)} className="h-3.5 w-3.5 rounded border-gray-300" />
        </td>
        <td className={`px-2 ${pad}`}>
          <div className="flex items-center gap-2">
            <AzureIcon kind="resource" type={r.type} className="h-4 w-4" />
            <span className="truncate font-medium text-gray-800" title={r.name}>{r.name}</span>
            <a href={`https://portal.azure.com/#@/resource${r.id}`} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} title="Open in Azure Portal" className="shrink-0 text-gray-300 opacity-0 transition hover:text-brand group-hover:opacity-100">↗</a>
          </div>
        </td>
        {cols.has("type") && <td className={`px-2 ${pad} text-gray-600`}>{friendlyResourceType(r.type)}</td>}
        {cols.has("resource_group") && <td className={`px-2 ${pad} text-[12px] text-gray-500`}>{r.resource_group || "—"}</td>}
        {cols.has("location") && <td className={`px-2 ${pad} text-[12px] text-gray-500`}>{r.location ? friendlyLocation(r.location) : "—"}</td>}
        {cols.has("subscription") && <td className={`px-2 ${pad} text-[12px] text-gray-500`} title={r.subscription_id}>{subName[r.subscription_id] || r.subscription_id.slice(0, 8)}</td>}
        {cols.has("sku") && <td className={`px-2 ${pad} text-[12px] text-gray-500`}>{r.size || r.sku || r.tier || "—"}</td>}
        {cols.has("tags") && <td className={`px-2 ${pad} text-[12px] text-gray-500`}>{r.tag_count || "—"}</td>}
        {cols.has("flags") && (
          <td className={`px-2 ${pad}`}>
            <div className="flex flex-wrap gap-1">
              {r.flags.filter((f) => f !== "untagged").map((f) => (
                <span key={f} className={`rounded px-1.5 py-0.5 text-[9px] ${FLAG_META[f]?.tone || "bg-gray-100 text-gray-600"}`}>{FLAG_META[f]?.label || f}</span>
              ))}
              {r.flags.length === 0 && <span className="text-[11px] text-green-500">✓</span>}
            </div>
          </td>
        )}
        {cols.has("cost") && <td className={`px-2 ${pad} text-right text-[12px] ${c ? "font-medium text-gray-700" : "text-gray-400"}`}>{moneyFmt(c, costCurrency)}</td>}
        {cols.has("workloads") && (
          <td className={`px-2 ${pad}`}>
            <div className="flex flex-wrap gap-1">
              {r.workloads.length === 0 ? <span className="text-[11px] text-gray-300">—</span> :
                r.workloads.slice(0, 2).map((w) => <span key={w.id} className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-600">🧩 {w.name}</span>)}
              {r.workloads.length > 2 && <span className="text-[10px] text-gray-400">+{r.workloads.length - 2}</span>}
            </div>
          </td>
        )}
      </tr>
    );
  }

  // Build grouped or flat rows for the windowed slice.
  const rows: React.ReactNode[] = [];
  if (groupBy === "none") {
    for (const r of resources.slice(0, limit)) rows.push(<Row key={r.id} r={r} />);
  } else {
    const groups = new Map<string, { label: string; items: InventoryResource[] }>();
    for (const r of resources) {
      const { key, label } = groupLabel(r);
      if (!groups.has(key)) groups.set(key, { label, items: [] });
      groups.get(key)!.items.push(r);
    }
    let rendered = 0;
    for (const [key, g] of [...groups.entries()].sort((a, b) => b[1].items.length - a[1].items.length)) {
      const isCollapsed = collapsed.has(key);
      rows.push(
        <tr key={`g-${key}`} className="bg-gray-50/80">
          <td colSpan={colCount} className={`px-3 ${pad} cursor-pointer text-[12px] font-semibold text-gray-600`} onClick={() => { const n = new Set(collapsed); if (n.has(key)) n.delete(key); else n.add(key); setCollapsed(n); }}>
            {isCollapsed ? "▸" : "▾"} {g.label} <span className="ml-1 rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500">{g.items.length}</span>
          </td>
        </tr>,
      );
      if (!isCollapsed) {
        for (const r of g.items) {
          if (rendered >= limit) break;
          rows.push(<Row key={r.id} r={r} />);
          rendered++;
        }
      }
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto" onScroll={onScroll}>
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 z-10 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-500 shadow-sm">
          <tr>
            <th className={`px-2 ${pad} w-8`}>
              <input type="checkbox" checked={allPickedOnPage} onChange={(e) => onPickAll(pageIds, e.target.checked)} className="h-3.5 w-3.5 rounded border-gray-300" />
            </th>
            <SortTh k="name" label="Name" />
            {cols.has("type") && <SortTh k="type" label="Type" />}
            {cols.has("resource_group") && <SortTh k="resource_group" label="Resource group" />}
            {cols.has("location") && <SortTh k="location" label="Location" />}
            {cols.has("subscription") && <SortTh k="subscription" label="Subscription" />}
            {cols.has("sku") && <th className={`px-2 ${pad} text-left font-semibold`}>SKU</th>}
            {cols.has("tags") && <th className={`px-2 ${pad} text-left font-semibold`}>Tags</th>}
            {cols.has("flags") && <th className={`px-2 ${pad} text-left font-semibold`}>Health</th>}
            {cols.has("cost") && <SortTh k="cost" label="Cost (30d)" />}
            {cols.has("workloads") && <th className={`px-2 ${pad} text-left font-semibold`}>Workloads</th>}
          </tr>
        </thead>
        <tbody className="divide-y">{rows}</tbody>
      </table>
      {limit < resources.length && (
        <div className="px-4 py-2 text-center text-[11px] text-gray-400">Showing {limit.toLocaleString()} of {resources.length.toLocaleString()} — scroll for more.</div>
      )}
    </div>
  );
}

// =========================================================================== CSV export
function exportCsv(resources: InventoryResource[], subName: Record<string, string>, costByRes: Record<string, number>) {
  const headers = ["name", "type", "kind", "resource_group", "location", "subscription", "subscription_id", "sku", "tag_count", "flags", "cost_30d", "workloads", "id"];
  const esc = (v: string) => `"${(v ?? "").replace(/"/g, '""')}"`;
  const lines = [headers.join(",")];
  for (const r of resources) {
    lines.push([
      esc(r.name), esc(friendlyResourceType(r.type)), esc(r.kind), esc(r.resource_group),
      esc(r.location ? friendlyLocation(r.location) : ""), esc(subName[r.subscription_id] || ""), esc(r.subscription_id),
      esc(r.size || r.sku || r.tier), String(r.tag_count), esc(r.flags.join("; ")),
      String(costByRes[r.id.toLowerCase()] ?? ""), esc(r.workloads.map((w) => w.name).join("; ")), esc(r.id),
    ].join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `inventory-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// =========================================================================== Column chooser / bulk bar
function ColumnChooser({ cols, setCols }: { cols: Set<ColKey>; setCols: (s: Set<ColKey>) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button onClick={() => setOpen((v) => !v)} className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">⚙ Columns</button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-30 mt-1 w-44 rounded-lg border bg-white p-2 shadow-lg">
            {ALL_COLS.map((c) => (
              <label key={c.key} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-[12px] text-gray-700 hover:bg-gray-50">
                <input type="checkbox" checked={cols.has(c.key)} onChange={() => { const n = new Set(cols); if (n.has(c.key)) n.delete(c.key); else n.add(c.key); setCols(n); }} className="h-3.5 w-3.5 rounded border-gray-300" />
                {c.label}
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function BulkBar({ count, onClear, onExport }: { count: number; onClear: () => void; onExport: () => void }) {
  return (
    <div className="flex items-center gap-3 border-b bg-brand/5 px-4 py-1.5 text-xs">
      <span className="font-medium text-brand">{count} selected</span>
      <button onClick={onExport} className="rounded-md border border-brand/30 bg-white px-2 py-0.5 text-[11px] text-brand hover:bg-brand/10">⬇ Export selection</button>
      <button onClick={onClear} className="text-[11px] text-gray-500 hover:text-gray-700">Clear</button>
    </div>
  );
}

// =========================================================================== Detail drawer
type DrawerTab = "overview" | "governance" | "findings" | "cost";

function DetailDrawer({
  resource,
  subName,
  connectionId,
  cost,
  costCurrency,
  costLoaded,
  onLoadCost,
  costLoading,
  onClose,
}: {
  resource: InventoryResource;
  subName: Record<string, string>;
  connectionId: string;
  cost?: number;
  costCurrency: string;
  costLoaded: boolean;
  onLoadCost: () => void;
  costLoading: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<DrawerTab>("overview");
  const [explain, setExplain] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [dnsDebug, setDnsDebug] = useState(false);
  const r = resource;

  // Reset transient state when switching resources.
  const idRef = useRef(r.id);
  useEffect(() => {
    if (idRef.current !== r.id) {
      idRef.current = r.id;
      setExplain("");
      setCopied(false);
      setTab("overview");
    }
  }, [r.id]);

  async function doExplain() {
    setBusy(true);
    try {
      const res = await api.inventoryExplain(r);
      setExplain(res.explanation || "No explanation produced.");
    } catch (e) {
      setExplain((e as Error)?.message || "Explain failed.");
    } finally {
      setBusy(false);
    }
  }

  function investigate() {
    // Hand the resource to a new chat investigation (read-only, copy-only).
    const prompt = `Investigate the Azure resource "${r.name}" (${friendlyResourceType(r.type)}). ARM id: ${r.id}. Summarize its purpose, configuration, health, recent changes, and any security/cost/reliability concerns. Read-only.`;
    try { sessionStorage.setItem("azsup.chat.prefill", prompt); } catch { /* ignore */ }
    window.location.href = "/chat";
  }

  const tags = Object.entries(r.tags || {});
  const portalUrl = `https://portal.azure.com/#@/resource${r.id}`;
  // Private Endpoint or a PaaS type that commonly has a PE → offer "Debug resolution".
  const t = (r.type || "").toLowerCase();
  const peEligible =
    t.includes("privateendpoint") ||
    /microsoft\.(storage|sql|dbforpostgresql|dbformysql|keyvault|documentdb|containerregistry|servicebus|web|cache)\b/.test(t);

  return (
    <div className="absolute inset-0 z-30 flex justify-end bg-black/20" onClick={onClose}>
      <div className="flex h-full w-full max-w-md flex-col overflow-hidden bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-3 border-b px-4 py-3">
          <AzureIcon kind="resource" type={r.type} className="mt-0.5 h-6 w-6" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-base font-semibold text-gray-800" title={r.name}>{r.name}</div>
            <div className="text-xs text-gray-500">{friendlyResourceType(r.type)}</div>
          </div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600">✕</button>
        </div>

        {/* Tabs */}
        <div className="flex items-center gap-1 border-b px-3 pt-1.5 text-[12px]">
          {([["overview", "Overview"], ["governance", "Governance"], ["findings", "Findings"], ["cost", "Cost"]] as const).map(([t, label]) => (
            <button key={t} onClick={() => setTab(t)} className={`rounded-t-md px-2.5 py-1.5 font-medium ${tab === t ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"}`}>{label}</button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-sm">
          {tab === "overview" && (
            <div className="space-y-4">
              <div className="rounded-lg border bg-gradient-to-br from-brand/5 to-violet-50 p-3">
                {explain ? <p className="text-[13px] leading-relaxed text-gray-700">{explain}</p> : (
                  <div className="flex items-center gap-3">
                    <button onClick={doExplain} disabled={busy} className="text-sm font-medium text-brand hover:underline disabled:opacity-50">{busy ? "✨ Thinking…" : "✨ Explain this resource"}</button>
                    <button onClick={investigate} className="text-sm font-medium text-violet-600 hover:underline">🔍 Investigate</button>
                  </div>
                )}
              </div>

              <Field label="Resource ID">
                <div className="flex items-start gap-1">
                  <code className="block flex-1 break-all rounded bg-gray-50 px-2 py-1 text-[11px] text-gray-600">{r.id}</code>
                  <button onClick={() => { navigator.clipboard?.writeText(r.id); setCopied(true); }} className="shrink-0 rounded border px-1.5 py-1 text-[11px] text-gray-500 hover:bg-gray-50">{copied ? "✓" : "Copy"}</button>
                </div>
              </Field>

              {r.flags.length > 0 && (
                <Field label="Health">
                  <div className="flex flex-wrap gap-1.5">
                    {r.flags.map((f) => <span key={f} className={`rounded px-2 py-0.5 text-[11px] ${FLAG_META[f]?.tone || "bg-gray-100 text-gray-600"}`}>{FLAG_META[f]?.label || f}</span>)}
                  </div>
                </Field>
              )}

              <div className="grid grid-cols-2 gap-3">
                {r.kind && <Field label="Kind"><span className="text-gray-700">{r.kind}</span></Field>}
                {(r.sku || r.size || r.tier) && <Field label="SKU / Size"><span className="text-gray-700">{[r.size, r.sku, r.tier].filter(Boolean).join(" · ") || "—"}</span></Field>}
                <Field label="Location"><span className="text-gray-700">{r.location ? friendlyLocation(r.location) : "—"}</span></Field>
                <Field label="Resource group"><span className="text-gray-700">{r.resource_group || "—"}</span></Field>
                <Field label="Subscription"><span className="text-gray-700">{subName[r.subscription_id] || r.subscription_id}</span></Field>
                {cost !== undefined && <Field label="Cost (30d)"><span className="font-medium text-gray-700">{moneyFmt(cost, costCurrency)}</span></Field>}
                {r.managed_by && <Field label="Managed by"><span className="break-all text-[11px] text-gray-500">{r.managed_by}</span></Field>}
              </div>

              <Field label="Workloads">
                {r.workloads.length === 0 ? <span className="text-[12px] text-gray-400">Not part of any workload.</span> : (
                  <div className="flex flex-wrap gap-1.5">
                    {r.workloads.map((w) => <Link key={w.id} to="/workloads" className="rounded-md bg-indigo-50 px-2 py-0.5 text-[12px] text-indigo-700 hover:bg-indigo-100">🧩 {w.name}</Link>)}
                  </div>
                )}
              </Field>

              <Field label={`Tags${tags.length ? ` (${tags.length})` : ""}`}>
                {tags.length === 0 ? <span className="text-[12px] text-gray-400">No tags.</span> : (
                  <div className="space-y-0.5">
                    {tags.map(([k, v]) => <div key={k} className="flex gap-2 text-[12px]"><span className="font-medium text-gray-600">{k}</span><span className="truncate text-gray-500" title={v}>{v}</span></div>)}
                  </div>
                )}
              </Field>

              <a href={portalUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[12px] font-medium text-gray-600 hover:bg-gray-50">↗ Open in Azure Portal</a>
              {peEligible && (
                <button onClick={() => setDnsDebug(true)} className="ml-2 inline-flex items-center gap-1 rounded-lg border border-sky-300 bg-sky-50 px-3 py-1.5 text-[12px] font-medium text-sky-700 hover:bg-sky-100">🧭 Debug resolution</button>
              )}
            </div>
          )}

          {tab === "governance" && <GovernanceTab resourceId={r.id} connectionId={connectionId} />}
          {tab === "findings" && <FindingsTab resourceId={r.id} />}
          {tab === "cost" && <CostTab cost={cost} costCurrency={costCurrency} loaded={costLoaded} onLoad={onLoadCost} loading={costLoading} />}
        </div>
      </div>
      {dnsDebug && (
        <DnsDebugModal architectureId="" preset={{ fqdn: r.name }} onClose={() => setDnsDebug(false)} />
      )}
    </div>
  );
}

function GovernanceTab({ resourceId, connectionId }: { resourceId: string; connectionId: string }) {
  const q = useQuery({
    queryKey: ["invGovernance", resourceId, connectionId],
    queryFn: () => api.inventoryGovernance(resourceId, connectionId || null),
    retry: false,
  });
  if (q.isLoading) return <div className="text-[12px] text-gray-400">Resolving effective policy…</div>;
  if (q.isError) return <div className="text-[12px] text-red-500">{(q.error as Error)?.message || "Failed to resolve policy."}</div>;
  const eff = q.data?.effective;
  if (!eff || eff.count === 0) return <div className="text-[12px] text-gray-400">No Azure Policy assignments govern this resource's scope.</div>;
  return (
    <div className="space-y-2">
      <div className="text-[11px] text-gray-500"><b className="text-gray-700">{eff.count}</b> effective policy assignment{eff.count === 1 ? "" : "s"} at this scope.</div>
      {eff.effective.map((p) => (
        <div key={p.id} className="rounded-lg border p-2.5">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-800">{p.display_name}</span>
            {p.effect && <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{p.effect}</span>}
          </div>
          <div className="mt-0.5 text-[11px] text-gray-500">{p.is_inherited ? `Inherited from ${p.inherited_from}` : "Assigned at this scope"}</div>
        </div>
      ))}
      <Link to="/policy" className="inline-block text-[11px] text-brand hover:underline">Open Azure Policy →</Link>
    </div>
  );
}

function FindingsTab({ resourceId }: { resourceId: string }) {
  const q = useQuery({ queryKey: ["invFindings", resourceId], queryFn: () => api.inventoryFindings(resourceId), retry: false });
  if (q.isLoading) return <div className="text-[12px] text-gray-400">Loading assessment findings…</div>;
  if (q.isError) return <div className="text-[12px] text-red-500">{(q.error as Error)?.message || "Failed to load findings."}</div>;
  const findings = q.data?.findings ?? [];
  if (findings.length === 0) return <div className="text-[12px] text-gray-400">No open Well-Architected findings touch this resource. 🎉</div>;
  const sevTone: Record<string, string> = { critical: "bg-red-100 text-red-700", error: "bg-orange-100 text-orange-700", warning: "bg-amber-100 text-amber-700", info: "bg-gray-100 text-gray-600" };
  return (
    <div className="space-y-2">
      {findings.map((f) => (
        <div key={`${f.run_id}-${f.check_id}`} className="rounded-lg border border-amber-200 bg-amber-50/40 p-2.5">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-800">{f.title}</span>
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${sevTone[f.severity] || sevTone.info}`}>{f.severity}</span>
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-gray-500">
            <span className="rounded bg-gray-100 px-1.5 py-0.5">{f.pillar}</span>
            <span>🧩 {f.workload_name}</span>
          </div>
          {f.ai_rationale && <div className="mt-1 text-[11px] text-gray-600">{f.ai_rationale}</div>}
        </div>
      ))}
      <Link to="/assessments" className="inline-block text-[11px] text-brand hover:underline">Open Assessments →</Link>
    </div>
  );
}

function CostTab({ cost, costCurrency, loaded, onLoad, loading }: { cost?: number; costCurrency: string; loaded: boolean; onLoad: () => void; loading: boolean }) {
  if (!loaded) {
    return (
      <div className="space-y-3 text-center">
        <p className="text-[12px] text-gray-500">Fetch last-30-days Azure cost for this resource (requires Cost Management Reader on the subscription).</p>
        <button onClick={onLoad} disabled={loading} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
          {loading ? "💲 Loading cost…" : "💲 Load cost"}
        </button>
      </div>
    );
  }
  if (cost === undefined) {
    return <div className="text-[12px] text-gray-400">No cost recorded for this resource in the last 30 days (it may be free, newly created, or not yet billed).</div>;
  }
  return (
    <div className="space-y-3">
      <div className="rounded-lg border bg-gradient-to-br from-emerald-50 to-white p-4 text-center">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Last 30 days cost</div>
        <div className="mt-1 text-2xl font-bold text-gray-800">{moneyFmt(cost, costCurrency)}</div>
      </div>
      <p className="text-[11px] text-gray-500">Actual cost over the trailing 30 days for this resource, from Azure Cost Management. Costs lag by a few hours.</p>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">{label}</div>
      {children}
    </div>
  );
}

// =========================================================================== Facets UI
function FacetGroup({ title, children, max = 999 }: { title: string; children: React.ReactNode; max?: number }) {
  const [expanded, setExpanded] = useState(false);
  const items = Array.isArray(children) ? children : [children];
  const shown = expanded ? items : items.slice(0, max);
  if (items.length === 0) return null;
  return (
    <div className="mb-3">
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">{title}</div>
      <div className="space-y-0.5">{shown}</div>
      {items.length > max && (
        <button onClick={() => setExpanded((v) => !v)} className="mt-1 text-[11px] text-brand hover:underline">
          {expanded ? "Show less" : `+${items.length - max} more`}
        </button>
      )}
    </div>
  );
}

function FacetRow({
  label,
  count,
  active,
  onClick,
  icon,
  dimmed,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  dimmed?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-[12px] ${active ? "bg-brand/10 text-brand" : dimmed ? "text-gray-300 hover:bg-gray-50" : "text-gray-600 hover:bg-gray-100"}`}
    >
      {icon}
      <span className="min-w-0 flex-1 truncate" title={label}>{label}</span>
      <span className={`shrink-0 text-[10px] ${active ? "text-brand" : "text-gray-400"}`}>{count}</span>
    </button>
  );
}

// =========================================================================== Overview mode (Theme 1 + 5)
const DONUT_COLORS = ["#0078D4", "#76BC2D", "#FFB900", "#E3008C", "#8661C5", "#00B7C3", "#FF8C00", "#B4009E", "#498205", "#C239B3"];

function Donut({ data, total }: { data: { label: string; value: number }[]; total: number }) {
  const top = data.slice(0, 10);
  let acc = 0;
  const r = 52, c = 2 * Math.PI * r;
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 140 140" className="h-32 w-32 shrink-0">
        <circle cx="70" cy="70" r={r} fill="none" stroke="#F3F4F6" strokeWidth="16" />
        {top.map((d, i) => {
          const frac = total ? d.value / total : 0;
          const dash = frac * c;
          const seg = (
            <circle key={d.label} cx="70" cy="70" r={r} fill="none" stroke={DONUT_COLORS[i % DONUT_COLORS.length]} strokeWidth="16"
              strokeDasharray={`${dash} ${c - dash}`} strokeDashoffset={-acc * c} transform="rotate(-90 70 70)" />
          );
          acc += frac;
          return seg;
        })}
        <text x="70" y="66" textAnchor="middle" className="fill-gray-800 text-lg font-bold">{total.toLocaleString()}</text>
        <text x="70" y="82" textAnchor="middle" className="fill-gray-400 text-[9px]">total</text>
      </svg>
      <div className="min-w-0 flex-1 space-y-0.5">
        {top.map((d, i) => (
          <div key={d.label} className="flex items-center gap-1.5 text-[11px]">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }} />
            <span className="min-w-0 flex-1 truncate text-gray-600" title={d.label}>{d.label}</span>
            <span className="shrink-0 font-medium text-gray-700">{d.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function BarList({ data, total }: { data: { label: string; value: number }[]; total: number }) {
  const max = Math.max(1, ...data.map((d) => d.value));
  return (
    <div className="space-y-1.5">
      {data.slice(0, 8).map((d) => (
        <div key={d.label} className="text-[11px]">
          <div className="flex items-center justify-between gap-2">
            <span className="min-w-0 truncate text-gray-600" title={d.label}>{d.label}</span>
            <span className="shrink-0 font-medium text-gray-700">{d.value}{total ? ` · ${Math.round(d.value / total * 100)}%` : ""}</span>
          </div>
          <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-brand" style={{ width: `${d.value / max * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function OverviewCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-sm font-semibold text-gray-700">{title}</h3>
      {children}
    </div>
  );
}

function OverviewMode({ inv, connectionId }: {
  inv: InventoryResponse;
  connectionId: string;
}) {
  const s = inv.summary;
  const insQ = useQuery({ queryKey: ["invInsights", connectionId], queryFn: () => api.inventoryInsights(connectionId || null), retry: false });
  const sevTone: Record<string, string> = { critical: "border-red-200 bg-red-50", warning: "border-amber-200 bg-amber-50", info: "border-gray-200 bg-gray-50" };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
        {/* AI insights */}
        <div className="rounded-xl border bg-gradient-to-br from-brand/5 to-violet-50 p-4 shadow-sm">
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-gray-700">✨ Estate insights{insQ.data?.source === "local" && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] font-normal text-gray-500">heuristic</span>}</h3>
          {insQ.isLoading ? <div className="text-[12px] text-gray-400">Analyzing your estate…</div> : (
            <>
              {insQ.data?.headline && <p className="mb-2 text-[13px] text-gray-700">{insQ.data.headline}</p>}
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {(insQ.data?.insights ?? []).map((ins, i) => (
                  <div key={i} className={`rounded-lg border p-2.5 ${sevTone[ins.severity] || sevTone.info}`}>
                    <div className="text-[12px] font-semibold text-gray-800">{ins.title}</div>
                    <div className="mt-0.5 text-[11px] text-gray-600">{ins.detail}</div>
                    {ins.action && <div className="mt-1 text-[11px] text-brand">→ {ins.action}</div>}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        {/* KPI tiles */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Kpi label="Resources" value={s.total_resources.toLocaleString()} />
          <Kpi label="Resource types" value={String(s.type_count)} />
          <Kpi label="Tag coverage" value={`${s.tag_coverage_pct}%`} tone={s.tag_coverage_pct < 50 ? "warn" : "ok"} />
          <Kpi label="Unassigned" value={String(s.unassigned_count)} tone={s.unassigned_count ? "warn" : "ok"} />
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <OverviewCard title="By resource type">
            <Donut data={inv.facets.types.map((t) => ({ label: friendlyResourceType(t.key), value: t.count }))} total={s.total_resources} />
          </OverviewCard>
          <OverviewCard title="By location">
            <Donut data={inv.facets.locations.map((l) => ({ label: friendlyLocation(l.key), value: l.count }))} total={s.total_resources} />
          </OverviewCard>
          <OverviewCard title="Top resource groups">
            <BarList data={inv.facets.resource_groups.map((g) => ({ label: g.key, value: g.count }))} total={s.total_resources} />
          </OverviewCard>
          <OverviewCard title="By subscription">
            <BarList data={inv.facets.subscriptions.map((sb) => ({ label: sb.name, value: sb.count }))} total={s.total_resources} />
          </OverviewCard>
          <OverviewCard title="Resources by workload">
            <BarList data={inv.facets.workloads.map((w) => ({ label: w.name, value: w.count })).concat(s.unassigned_count ? [{ label: "Unassigned", value: s.unassigned_count }] : [])} total={s.total_resources} />
          </OverviewCard>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" }) {
  return (
    <div className="rounded-xl border bg-white p-3 text-center shadow-sm">
      <div className={`text-2xl font-bold ${tone === "warn" ? "text-amber-600" : "text-gray-800"}`}>{value}</div>
      <div className="text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

// =========================================================================== Cost mode (Theme 4 — FinOps)
// Horizontal split-bar list used for the cost rollups (workload / type / region / top resources).
function CostSplitBars({ rows, currency, colorAt }: {
  rows: { label: string; value: number; pct?: number; sub?: string; href?: string }[];
  currency: string;
  colorAt?: (i: number) => string;
}) {
  const max = Math.max(0.01, ...rows.map((r) => r.value));
  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <div key={r.label + i} className="text-[12px]">
          <div className="flex items-center justify-between gap-2">
            <span className="flex min-w-0 items-center gap-1 truncate text-gray-700" title={r.label}>
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: colorAt ? colorAt(i) : "#10b981" }} />
              {r.href ? (
                <a href={r.href} target="_blank" rel="noopener noreferrer" className="truncate hover:text-brand hover:underline" title={r.label}>{r.label}</a>
              ) : (
                <span className="truncate">{r.label}</span>
              )}
              {r.sub && <span className="shrink-0 text-[10px] text-gray-400">· {r.sub}</span>}
            </span>
            <span className="shrink-0 font-semibold text-gray-800">
              {moneyFmt(r.value, currency)}
              {r.pct !== undefined && <span className="ml-1 font-normal text-gray-400">{r.pct}%</span>}
            </span>
          </div>
          <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full" style={{ width: `${Math.min(100, (r.value / max) * 100)}%`, background: colorAt ? colorAt(i) : "#10b981" }} />
          </div>
        </div>
      ))}
      {rows.length === 0 && <div className="text-[12px] text-gray-400">No cost attributed.</div>}
    </div>
  );
}

// Money donut: same SVG ring as Overview, but labelled with currency amounts.
function CostDonut({ data, total, currency }: { data: { label: string; value: number }[]; total: number; currency: string }) {
  const top = data.slice(0, 10);
  let acc = 0;
  const r = 52, c = 2 * Math.PI * r;
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 140 140" className="h-32 w-32 shrink-0">
        <circle cx="70" cy="70" r={r} fill="none" stroke="#F3F4F6" strokeWidth="16" />
        {top.map((d, i) => {
          const frac = total ? d.value / total : 0;
          const dash = frac * c;
          const seg = (
            <circle key={d.label} cx="70" cy="70" r={r} fill="none" stroke={DONUT_COLORS[i % DONUT_COLORS.length]} strokeWidth="16"
              strokeDasharray={`${dash} ${c - dash}`} strokeDashoffset={-acc * c} transform="rotate(-90 70 70)" />
          );
          acc += frac;
          return seg;
        })}
        <text x="70" y="74" textAnchor="middle" className="fill-gray-700 text-[10px] font-semibold">{moneyFmt(total, currency)}</text>
      </svg>
      <div className="min-w-0 flex-1 space-y-0.5">
        {top.map((d, i) => (
          <div key={d.label} className="flex items-center gap-1.5 text-[11px]">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }} />
            <span className="min-w-0 flex-1 truncate text-gray-600" title={d.label}>{d.label}</span>
            <span className="shrink-0 font-medium text-gray-700">{moneyFmt(d.value, currency)}</span>
          </div>
        ))}
        {top.length === 0 && <div className="text-[11px] text-gray-400">No data.</div>}
      </div>
    </div>
  );
}

// Roll a per-resource cost map up over a (possibly filtered) resource list — by workload,
// type, region, subscription, resource group, plus the most expensive resources. Mirrors the
// backend build_rollup, but runs client-side so the Cost tab reflects the left-hand facet
// filters live. Multi-workload resources split their cost evenly across their workloads.
type CostRollup = ReturnType<typeof buildCostRollup>;
function buildCostRollup(cost: InventoryCost, resources: InventoryResource[], hasFilters: boolean) {
  const byRes = cost.by_resource || {};
  const byWl = new Map<string, { name: string; cost: number; count: number }>();
  const byType = new Map<string, number>();
  const byLoc = new Map<string, number>();
  const bySub = new Map<string, number>();
  const byRg = new Map<string, number>();
  const top: { id: string; name: string; type: string; cost: number; workloads: string[] }[] = [];
  let attributed = 0;
  let unassigned = 0;

  for (const res of resources) {
    const amount = byRes[res.id.toLowerCase()];
    if (!amount) continue;
    attributed += amount;
    byType.set(res.type, (byType.get(res.type) || 0) + amount);
    if (res.location) byLoc.set(res.location, (byLoc.get(res.location) || 0) + amount);
    bySub.set(res.subscription_id, (bySub.get(res.subscription_id) || 0) + amount);
    if (res.resource_group) byRg.set(res.resource_group, (byRg.get(res.resource_group) || 0) + amount);
    if (res.workloads.length) {
      const share = amount / res.workloads.length;
      for (const w of res.workloads) {
        const cur = byWl.get(w.id) || { name: w.name, cost: 0, count: 0 };
        cur.cost += share;
        cur.count += 1;
        byWl.set(w.id, cur);
      }
    } else {
      unassigned += amount;
    }
    top.push({ id: res.id, name: res.name, type: res.type, cost: amount, workloads: res.workloads.map((w) => w.name) });
  }

  const total = Math.round(attributed * 100) / 100;
  const pct = (v: number) => (total ? Math.round((v / total) * 1000) / 10 : 0);
  const rank = (m: Map<string, number>) =>
    [...m.entries()].sort((a, b) => b[1] - a[1]).map(([key, c]) => ({ key, cost: Math.round(c * 100) / 100, pct: pct(c) }));

  return {
    available: cost.available,
    not_loaded: cost.not_loaded,
    currency: cost.currency || "USD",
    period: cost.period || "",
    fetched_at: cost.fetched_at || "",
    cached: cost.cached,
    total,
    unassigned_cost: Math.round(unassigned * 100) / 100,
    // Only meaningful for the whole estate; with filters the "missing" cost is just filtered-out.
    unattributed_total: hasFilters ? 0 : Math.round((cost.total - attributed) * 100) / 100,
    by_workload: [...byWl.entries()]
      .map(([id, v]) => ({ id, name: v.name, cost: Math.round(v.cost * 100) / 100, pct: pct(v.cost), resource_count: v.count }))
      .sort((a, b) => b.cost - a.cost),
    by_type: rank(byType),
    by_location: rank(byLoc),
    by_subscription: rank(bySub),
    by_resource_group: rank(byRg),
    top_resources: top.sort((a, b) => b.cost - a.cost).slice(0, 20),
    errors: cost.errors || [],
  };
}

function OptimizationMode({ connectionId, onLoadCost, costLoading }: {
  connectionId: string;
  onLoadCost: (force: boolean) => void;
  costLoading: boolean;
}) {
  const q = useQuery({
    queryKey: ["inventoryOptimization", connectionId],
    queryFn: () => api.inventoryOptimization(connectionId || null),
  });
  const data: InventoryOptimization | undefined = q.data;
  const cur = data?.currency || "USD";

  if (q.isLoading) {
    return <div className="p-8 text-center text-sm text-gray-400">Analyzing inventory…</div>;
  }
  if (data && !data.available) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-3xl rounded-xl border bg-amber-50 p-6 text-center">
          <div className="text-sm font-medium text-amber-800">Inventory not loaded yet</div>
          <p className="mt-1 text-xs text-amber-700">
            Open the <b>Grid</b> tab once to collect resources, then return here to see
            orphaned and idle resources you can clean up.
          </p>
        </div>
      </div>
    );
  }

  const items = data?.items ?? [];
  const categories = data?.categories ?? [];
  const total = data?.total_monthly_cost ?? 0;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
        {/* Header: estimated monthly savings */}
        <div className="flex flex-wrap items-center gap-4 rounded-xl border bg-gradient-to-br from-emerald-50 to-white p-4 shadow-sm">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-emerald-700">Potential monthly savings</div>
            <div className="text-2xl font-bold text-emerald-800">
              {data?.cost_available ? moneyFmt(total, cur) : "—"}
            </div>
            <div className="text-[11px] text-gray-500">
              {items.length} orphaned / idle {items.length === 1 ? "resource" : "resources"}
              {data?.cost_available && data?.cost_period ? ` · cost over ${data.cost_period}` : ""}
            </div>
          </div>
          {!data?.cost_available && (
            <button
              onClick={() => onLoadCost(false)}
              disabled={costLoading}
              className="ml-auto rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-emerald-700 disabled:opacity-50"
            >
              {costLoading ? "Loading cost…" : "Load cost to estimate savings"}
            </button>
          )}
        </div>

        {items.length === 0 ? (
          <div className="rounded-xl border bg-white p-8 text-center">
            <div className="text-sm font-medium text-gray-700">No orphaned or idle resources found 🎉</div>
            <p className="mt-1 text-xs text-gray-500">
              No unattached disks, idle public IPs, or orphaned NICs in the current inventory.
            </p>
          </div>
        ) : (
          <>
            {/* Category summary cards */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {categories.map((c) => (
                <div key={c.flag} className="rounded-xl border bg-white p-3 shadow-sm">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-800">{c.label}</span>
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">{c.count}</span>
                  </div>
                  {data?.cost_available && (
                    <div className="mt-1 text-lg font-bold text-emerald-700">{moneyFmt(c.monthly_cost, cur)}<span className="ml-1 text-[11px] font-normal text-gray-400">/mo</span></div>
                  )}
                  <p className="mt-1 text-[11px] leading-snug text-gray-500">{c.remediation}</p>
                </div>
              ))}
            </div>

            {/* Detail table */}
            <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-3 py-2">Resource</th>
                    <th className="px-3 py-2">Issue</th>
                    <th className="px-3 py-2">Resource group</th>
                    <th className="px-3 py-2">Location</th>
                    <th className="px-3 py-2 text-right">Est. monthly</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.id} className="border-t hover:bg-gray-50">
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <AzureIcon kind="resource" type={it.type} className="h-4 w-4" />
                          <div className="min-w-0">
                            <div className="truncate font-medium text-gray-800" title={it.name}>{it.name}</div>
                            <div className="truncate text-[11px] text-gray-400" title={it.type}>{friendlyResourceType(it.type)}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${FLAG_META[it.category]?.tone || "bg-amber-100 text-amber-700"}`} title={it.reason}>
                          {it.category_label}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-[12px] text-gray-600">{it.resource_group || "—"}</td>
                      <td className="px-3 py-2 text-[12px] text-gray-600">{friendlyLocation(it.location) || "—"}</td>
                      <td className="px-3 py-2 text-right font-medium tabular-nums text-gray-700">
                        {data?.cost_available ? moneyFmt(it.monthly_cost, cur) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] text-gray-400">
              Costs are the trailing-30-day actual spend per resource (≈ monthly) from Azure Cost
              Management — an estimate of what you’d save by cleaning each one up. Always confirm a
              resource is truly unused before deleting it.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function CostMode({ cost, resources, subName, hasFilters, chipProps, onClearFilters, onLoadCost, loading }: {
  cost?: InventoryCost;
  resources: InventoryResource[];
  subName: Record<string, string>;
  hasFilters: boolean;
  chipProps: FilterChipsProps;
  onClearFilters: () => void;
  onLoadCost: (force: boolean) => void;
  loading: boolean;
}) {
  // Roll the (server-cached) per-resource cost up over the CURRENTLY FILTERED resources, so the
  // left-hand facet selections (workloads, types, regions, …) drive these charts live.
  const r: CostRollup | undefined = useMemo(
    () => (cost && cost.available ? buildCostRollup(cost, resources, hasFilters) : undefined),
    [cost, resources, hasFilters],
  );

  const cur = r?.currency || cost?.currency || "USD";
  const busy = loading;
  // "Nothing cached yet" — distinct from "loaded but Cost Management unavailable".
  const notLoaded = !cost || cost.not_loaded;
  const unavailable = !!cost && !cost.not_loaded && !cost.available;
  const matchedCount = r?.top_resources ? resources.filter((x) => (cost?.by_resource || {})[x.id.toLowerCase()]).length : 0;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
        {/* Header: total + refresh */}
        <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-gradient-to-br from-emerald-50 to-white p-4 shadow-sm">
          <span className="text-2xl">💰</span>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-700">Cost by workload</h2>
            <p className="text-[11px] text-gray-500">
              Last-30-days Azure spend, attributed to your workloads. Resources shared by multiple workloads have their cost split evenly.
            </p>
          </div>
          <div className="ml-auto flex items-center gap-3">
            {r && (
              <div className="text-right">
                <div className="text-xl font-bold text-gray-800">{moneyFmt(r.total, cur)}</div>
                <div className="text-[10px] text-gray-400">
                  {r.period} · last 30 days{hasFilters ? ` · filtered (${matchedCount} of ${resources.length})` : ""}
                  {r.fetched_at && (() => {
                    const ageSec = (Date.now() - new Date(r.fetched_at).getTime()) / 1000;
                    const stale = ageSec > STALE_SECONDS;
                    return (
                      <>
                        {" · "}
                        <span
                          className={stale ? "font-bold text-red-600" : ""}
                          title={stale ? "Cost data is over 6 hours old; refresh recommended" : undefined}
                        >
                          updated {new Date(r.fetched_at).toLocaleString()}
                        </span>
                      </>
                    );
                  })()}
                  {r.cached ? " (cached)" : ""}
                </div>
              </div>
            )}
            <button
              onClick={() => onLoadCost(true)}
              disabled={busy}
              title="Re-run the Azure Cost Management query and refresh cached cost"
              className="rounded-lg border border-emerald-200 bg-white px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-50 disabled:opacity-50"
            >
              {loading ? "↻ Refreshing cost…" : "↻ Refresh cost"}
            </button>
          </div>
        </div>

        {/* Active filters — removable one-by-one, same as the Grid tab. */}
        {(chipProps.kqlText || hasFilters) && (
          <div className="rounded-xl border bg-white px-4 py-2.5 shadow-sm">
            <FilterChipsBar p={chipProps} hasFilters={hasFilters} onClearAll={onClearFilters} />
          </div>
        )}

        {notLoaded && !busy ? (
          <div className="rounded-xl border bg-white p-10 text-center shadow-sm">
            <div className="mb-1 text-3xl">💲</div>
            <div className="text-sm font-medium text-gray-700">No cost loaded yet</div>
            <p className="mx-auto mt-1 max-w-md text-[12px] text-gray-500">
              Load last-30-days cost from Azure Cost Management. The result is cached permanently on the server and reused everywhere until you click <b>Refresh cost</b>.
            </p>
            <button onClick={() => onLoadCost(false)} className="mt-3 rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700">
              Load cost
            </button>
          </div>
        ) : busy && notLoaded ? (
          <div className="flex h-40 items-center justify-center text-sm text-gray-400">Querying Azure Cost Management…</div>
        ) : unavailable ? (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-6 text-center text-sm text-amber-700">
            Cost Management data isn't available for this connection{cost?.errors[0] ? ` (${cost.errors[0]})` : ""}.
            <div className="mt-1 text-[11px] text-amber-600">The connection needs the <b>Cost Management Reader</b> role on the subscriptions.</div>
          </div>
        ) : r && r.total === 0 ? (
          <div className="rounded-xl border bg-white p-10 text-center text-sm text-gray-500 shadow-sm">
            No cost for the current filter selection.
          </div>
        ) : r ? (
          <>
            {/* Cost by workload — headline */}
            <OverviewCard title="Cost by workload">
              <CostSplitBars
                currency={cur}
                colorAt={(i) => DONUT_COLORS[i % DONUT_COLORS.length]}
                rows={r.by_workload.map((w) => ({ label: w.name, value: w.cost, pct: w.pct, sub: `${w.resource_count} res` }))
                  .concat(r.unassigned_cost > 0 ? [{ label: "Unassigned", value: r.unassigned_cost, pct: r.total ? Math.round((r.unassigned_cost / r.total) * 1000) / 10 : 0, sub: "no workload" }] : [])}
              />
            </OverviewCard>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <OverviewCard title="Cost by resource type">
                <CostDonut currency={cur} total={r.total} data={r.by_type.map((b) => ({ label: friendlyResourceType(b.key), value: b.cost }))} />
              </OverviewCard>
              <OverviewCard title="Cost by region">
                <CostDonut currency={cur} total={r.total} data={r.by_location.map((b) => ({ label: friendlyLocation(b.key), value: b.cost }))} />
              </OverviewCard>
              <OverviewCard title="Cost by subscription">
                <CostSplitBars currency={cur} rows={r.by_subscription.map((b) => ({ label: subName[b.key] || b.key, value: b.cost, pct: b.pct }))} />
              </OverviewCard>
              <OverviewCard title="Cost by resource group">
                <CostSplitBars currency={cur} rows={r.by_resource_group.slice(0, 10).map((b) => ({ label: b.key, value: b.cost, pct: b.pct }))} />
              </OverviewCard>
            </div>

            {/* Top resources */}
            <OverviewCard title="Most expensive resources">
              <CostSplitBars
                currency={cur}
                rows={r.top_resources.map((t) => ({
                  label: t.name || t.id,
                  value: t.cost,
                  pct: r.total ? Math.round((t.cost / r.total) * 1000) / 10 : 0,
                  sub: [friendlyResourceType(t.type), t.workloads[0] || "unassigned"].filter(Boolean).join(" · "),
                  href: `https://portal.azure.com/#@/resource${t.id}`,
                }))}
              />
            </OverviewCard>

            {r.errors.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
                ⚠ {r.errors.length} subscription(s) couldn't be queried (throttled or no access) — totals may be partial. Click <b>Refresh cost</b> to retry.
              </div>
            )}
            {r.unattributed_total > 0.5 && (
              <div className="text-center text-[11px] text-gray-400">
                {moneyFmt(r.unattributed_total, cur)} of spend couldn't be matched to an inventoried resource (deleted or out-of-scope resources) and is excluded from the breakdowns above.
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}

// =========================================================================== Changes mode (Theme 3)
function ChangesMode({ connectionId, subName }: { connectionId: string; subName: Record<string, string> }) {
  const qc = useQueryClient();
  const snapsQ = useQuery({ queryKey: ["invSnapshots", connectionId], queryFn: () => api.inventorySnapshots(connectionId || null), retry: false });
  const driftQ = useQuery({ queryKey: ["invDrift", connectionId], queryFn: () => api.inventoryDrift(connectionId || null), retry: false });
  const [taking, setTaking] = useState(false);
  const snaps = snapsQ.data?.snapshots ?? [];
  const drift = driftQ.data?.drift;

  async function takeSnapshot() {
    setTaking(true);
    try {
      await api.inventoryTakeSnapshot(connectionId || null);
      qc.invalidateQueries({ queryKey: ["invSnapshots"] });
      qc.invalidateQueries({ queryKey: ["invDrift"] });
    } finally {
      setTaking(false);
    }
  }
  async function del(id: string) {
    await api.inventoryDeleteSnapshot(id).catch(() => null);
    qc.invalidateQueries({ queryKey: ["invSnapshots"] });
    qc.invalidateQueries({ queryKey: ["invDrift"] });
  }

  const label = (it: { name?: string; type?: string }) => `${it.name || "(resource)"}${it.type ? ` · ${friendlyResourceType(it.type)}` : ""}`;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-gray-700">Inventory drift</h3>
            <p className="text-[11px] text-gray-500">Track what's been added, removed, or changed between snapshots.</p>
          </div>
          <button onClick={takeSnapshot} disabled={taking} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">{taking ? "Capturing…" : "📸 Take snapshot"}</button>
        </div>

        {driftQ.data?.reason ? (
          <div className="rounded-lg border border-dashed bg-gray-50/60 p-6 text-center text-sm text-gray-400">{driftQ.data.reason}</div>
        ) : drift ? (
          <>
            <div className="grid grid-cols-3 gap-3">
              <Kpi label="Added" value={`+${drift.counts.added}`} tone={drift.counts.added ? "ok" : undefined} />
              <Kpi label="Removed" value={`−${drift.counts.removed}`} tone={drift.counts.removed ? "warn" : undefined} />
              <Kpi label="Changed" value={String(drift.counts.changed)} tone={drift.counts.changed ? "warn" : undefined} />
            </div>
            <p className="text-[11px] text-gray-400">Since snapshot {new Date(drift.baseline_at).toLocaleString()}.</p>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              <DriftList title="➕ Added" tone="text-green-700" items={drift.added.map((i) => label(i))} />
              <DriftList title="➖ Removed" tone="text-red-700" items={drift.removed.map((i) => label(i))} />
              <DriftList title="✏️ Changed" tone="text-amber-700" items={drift.changed.map((i) => `${i.name} · ${Object.keys(i.changes || {}).join(", ")}`)} />
            </div>
          </>
        ) : (
          <div className="text-[12px] text-gray-400">Computing drift…</div>
        )}

        <div>
          <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-gray-500">Snapshots ({snaps.length})</h4>
          {snaps.length === 0 ? <div className="text-[12px] text-gray-400">No snapshots yet.</div> : (
            <div className="space-y-1">
              {snaps.map((sn) => (
                <div key={sn.id} className="flex items-center gap-2 rounded-lg border p-2 text-sm">
                  <span className="flex-1 text-gray-700">{new Date(sn.created_at).toLocaleString()}</span>
                  <span className="text-[11px] text-gray-500">{sn.total_resources.toLocaleString()} resources · {sn.tag_coverage_pct}% tagged</span>
                  <button onClick={() => del(sn.id)} title="Delete snapshot" className="rounded p-1 text-gray-300 hover:bg-red-50 hover:text-red-500">🗑</button>
                </div>
              ))}
            </div>
          )}
        </div>
        {/* subName referenced to keep prop meaningful for future per-row sub display */}
        <span className="hidden">{Object.keys(subName).length}</span>
      </div>
    </div>
  );
}

function DriftList({ title, tone, items }: { title: string; tone: string; items: string[] }) {
  return (
    <div className="rounded-xl border bg-white p-3 shadow-sm">
      <div className={`mb-2 text-[12px] font-semibold ${tone}`}>{title} ({items.length})</div>
      {items.length === 0 ? <div className="text-[11px] text-gray-400">None.</div> : (
        <div className="max-h-64 space-y-0.5 overflow-y-auto">
          {items.map((it, i) => <div key={i} className="truncate text-[11px] text-gray-600" title={it}>{it}</div>)}
        </div>
      )}
    </div>
  );
}
