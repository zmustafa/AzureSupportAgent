import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, streamPrefetch, type TreeNode, type WorkloadNode, type WorkloadNodeKind } from "../api";
import { formatError } from "../utils/format";
import { AzureIcon, friendlyLocation, friendlyResourceType } from "./AzureIcon";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";

const KIND_LABEL: Record<WorkloadNodeKind, string> = {
  mg: "Management group",
  subscription: "Subscription",
  resource_group: "Resource group",
  resource: "Resource",
};

function cacheAgeMs(now: number, cachedAt: number): number {
  return Math.max(0, now - cachedAt);
}

// Cache is considered stale (shown in bold red) once it's older than 1 minute.
function cacheStale(now: number, cachedAt: number): boolean {
  return cacheAgeMs(now, cachedAt) > 60_000;
}

function relAge(now: number, cachedAt: number): string {
  const s = Math.floor(cacheAgeMs(now, cachedAt) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

interface RowState {
  node: TreeNode;
  depth: number;
  parentId: string | null;
}

/**
 * Azure-portal-style resource/scope picker. Browse an expandable MG ▸ Sub ▸ RG ▸
 * Resource tree (lazy-loaded), filter by subscription/type/location, search, and
 * hand-pick nodes at any level. A checked parent means "all descendants"; deselecting a
 * child of a checked parent records it as an exclusion. Reusable: returns the chosen
 * nodes via onApply.
 */
export function ResourcePicker({
  connectionId,
  initialNodes,
  onApply,
  onCancel,
}: {
  connectionId: string;
  initialNodes: WorkloadNode[];
  onApply: (nodes: WorkloadNode[]) => void;
  onCancel: () => void;
}) {
  const [groupBy, setGroupBy] = useState<"subscription" | "mg">("subscription");
  const [tab, setTab] = useState<"browse" | "search">("browse");
  // Selected nodes keyed by ARM id.
  const [selected, setSelected] = useState<Map<string, WorkloadNode>>(
    () => new Map(initialNodes.map((n) => [n.id, n])),
  );
  // Exclusions: childId -> parentId (a deselected descendant of a checked parent).
  const [excluded, setExcluded] = useState<Map<string, string>>(new Map());

  // Lazy tree: expanded node ids + their loaded children.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [children, setChildren] = useState<Map<string, TreeNode[]>>(new Map());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [topNodes, setTopNodes] = useState<TreeNode[]>([]);
  const [topLoading, setTopLoading] = useState(false);
  const [error, setError] = useState("");

  // Cache freshness: the OLDEST cached_at (epoch ms) across all loaded data, plus a
  // ticking "now" so the displayed age stays live. A manual Refresh re-pulls from Azure.
  const [oldestCachedAt, setOldestCachedAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());
  const [refreshing, setRefreshing] = useState(false);

  // Live discovery status (top-level load + prefetch), shown instead of a static spinner.
  const [discoveryStatus, setDiscoveryStatus] = useState<string>("");
  const [prefetching, setPrefetching] = useState(false);
  const [prefetchCounts, setPrefetchCounts] = useState<{ subs: number; rgs: number; resources: number } | null>(null);
  const prefetchAbort = useRef<AbortController | null>(null);

  // Search state.
  const [query, setQuery] = useState("");
  const [subFilter, setSubFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [locFilter, setLocFilter] = useState<string[]>([]);
  const [searchRows, setSearchRows] = useState<TreeNode[]>([]);
  const [searching, setSearching] = useState(false);

  // Record a cached_at ISO timestamp, keeping the OLDEST one seen (worst-case freshness).
  function noteCachedAt(iso?: string) {
    if (!iso) return;
    const ms = Date.parse(iso);
    if (Number.isNaN(ms)) return;
    setOldestCachedAt((prev) => (prev == null ? ms : Math.min(prev, ms)));
  }

  // Tick the age display every 5s while the picker is open.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);

  // Abort any in-flight prefetch when the picker unmounts.
  useEffect(() => {
    return () => prefetchAbort.current?.abort();
  }, []);

  const facetsQ = useQuery({
    queryKey: ["workloadFacets", connectionId, subFilter],
    queryFn: async () => {
      const r = await api.workloadFacets({ connection_id: connectionId, subscription_id: subFilter });
      noteCachedAt(r.cached_at);
      return r;
    },
    enabled: !!connectionId,
  });
  // Subscriptions for the filter dropdown (top-level, group_by=subscription).
  const subsQ = useQuery({
    queryKey: ["workloadSubs", connectionId],
    queryFn: async () => {
      const r = await api.workloadTree({ connection_id: connectionId, group_by: "subscription" });
      noteCachedAt(r.cached_at);
      return r;
    },
    enabled: !!connectionId,
  });

  // Load the top level when group-by changes.
  useEffect(() => {
    if (!connectionId) return;
    let cancelled = false;
    setTopLoading(true);
    setError("");
    setDiscoveryStatus(groupBy === "mg" ? "Discovering management groups…" : "Discovering subscriptions…");
    api
      .workloadTree({ connection_id: connectionId, group_by: groupBy })
      .then((r) => {
        if (cancelled) return;
        setTopNodes(r.nodes);
        noteCachedAt(r.cached_at);
        const label = groupBy === "mg" ? "management group" : "subscription";
        setDiscoveryStatus(`Found ${r.nodes.length} ${label}${r.nodes.length === 1 ? "" : "s"}.`);
      })
      .catch((e) => !cancelled && setError(formatError(e)))
      .finally(() => !cancelled && setTopLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId, groupBy]);

  // Prefetch: walk subscriptions → RGs → resources, warming the server cache, with live
  // progress. After it completes, expanding any node in the tree is instant.
  async function prefetchAll() {
    if (!connectionId || prefetching) return;
    setPrefetching(true);
    setError("");
    setPrefetchCounts({ subs: 0, rgs: 0, resources: 0 });
    const controller = new AbortController();
    prefetchAbort.current = controller;
    await streamPrefetch(
      { connection_id: connectionId, group_by: groupBy },
      {
        onStatus: (d) => {
          setDiscoveryStatus(d.message);
          setPrefetchCounts({
            subs: d.subscriptions ?? 0,
            rgs: d.resource_groups ?? 0,
            resources: d.resources ?? 0,
          });
        },
        onDone: (d) => {
          setDiscoveryStatus(
            `Prefetched ${d.subscriptions} subscription(s), ${d.resource_groups} resource group(s), ${d.resources} resource(s).`,
          );
          setPrefetchCounts({ subs: d.subscriptions, rgs: d.resource_groups, resources: d.resources });
          setOldestCachedAt(Date.now());
        },
        onError: (m) => setError(m),
      },
      controller.signal,
    );
    setPrefetching(false);
  }

  async function toggleExpand(node: TreeNode) {
    const next = new Set(expanded);
    if (next.has(node.id)) {
      next.delete(node.id);
      setExpanded(next);
      return;
    }
    next.add(node.id);
    setExpanded(next);
    if (!children.has(node.id)) {
      setLoading((l) => new Set(l).add(node.id));
      try {
        const r = await api.workloadTree({
          connection_id: connectionId,
          kind: node.kind,
          node_id: node.id,
        });
        setChildren((c) => new Map(c).set(node.id, r.nodes));
        noteCachedAt(r.cached_at);
      } catch (e) {
        setError(formatError(e));
      } finally {
        setLoading((l) => {
          const n = new Set(l);
          n.delete(node.id);
          return n;
        });
      }
    }
  }

  // Build an id -> node index from everything currently loaded (kinds are stable).
  function nodeIndex(): Map<string, TreeNode> {
    const idx = new Map<string, TreeNode>();
    for (const n of topNodes) idx.set(n.id, n);
    for (const arr of children.values()) for (const n of arr) idx.set(n.id, n);
    return idx;
  }

  // Refresh: invalidate the server cache for this connection, then re-pull the top level
  // and every currently-expanded node from Azure (force-refresh). Keeps the tree open.
  async function refreshAll() {
    if (!connectionId || refreshing) return;
    setRefreshing(true);
    setError("");
    try {
      await api.invalidateWorkloadCache(connectionId);
      const idx = nodeIndex();
      const top = await api.workloadTree({
        connection_id: connectionId,
        group_by: groupBy,
        refresh: true,
      });
      setTopNodes(top.nodes);
      let oldest = Date.parse(top.cached_at);

      const newChildren = new Map<string, TreeNode[]>();
      for (const id of expanded) {
        const node = idx.get(id);
        if (!node || !node.has_children) continue;
        try {
          const r = await api.workloadTree({
            connection_id: connectionId,
            kind: node.kind,
            node_id: id,
            refresh: true,
          });
          newChildren.set(id, r.nodes);
          const ms = Date.parse(r.cached_at);
          if (!Number.isNaN(ms)) oldest = Math.min(oldest, ms);
        } catch {
          /* a node that no longer exists just drops out */
        }
      }
      setChildren(newChildren);
      // Keep only expansions that still resolved to a children list after the refresh.
      setExpanded((e) => new Set([...e].filter((id) => newChildren.has(id))));
      setOldestCachedAt(Number.isNaN(oldest) ? Date.now() : oldest);
      // Facets + subscription dropdown were also invalidated server-side; refetch them.
      void facetsQ.refetch();
      void subsQ.refetch();
    } catch (e) {
      setError(formatError(e));
    } finally {
      setRefreshing(false);
    }
  }

  function nodeFrom(t: TreeNode): WorkloadNode {
    return {
      kind: t.kind,
      id: t.id,
      name: t.name,
      subscription_id: t.subscription_id ?? null,
      resource_group: t.resource_group ?? null,
      resource_type: t.resource_type ?? null,
      location: t.location ?? null,
      excludes: [],
    };
  }

  // Is this node covered by a selected ancestor? (id prefix match on ARM path)
  function coveringAncestor(node: TreeNode): WorkloadNode | null {
    for (const sel of selected.values()) {
      if (sel.id === node.id) continue;
      if (node.id.toLowerCase().startsWith(sel.id.toLowerCase() + "/")) return sel;
      // subscription parent of an RG/resource
      if (sel.kind === "subscription" && node.subscription_id === sel.id) return sel;
    }
    return null;
  }

  function isChecked(node: TreeNode): boolean | "indeterminate" {
    if (selected.has(node.id)) return true;
    const anc = coveringAncestor(node);
    if (anc) return excluded.get(node.id) === anc.id ? false : true;
    return false;
  }

  function toggleSelect(node: TreeNode) {
    const anc = coveringAncestor(node);
    if (selected.has(node.id)) {
      // Uncheck an explicitly selected node.
      setSelected((m) => {
        const n = new Map(m);
        n.delete(node.id);
        return n;
      });
      return;
    }
    if (anc) {
      // Covered by an ancestor: toggle an exclusion.
      setExcluded((m) => {
        const n = new Map(m);
        if (n.get(node.id) === anc.id) n.delete(node.id);
        else n.set(node.id, anc.id);
        return n;
      });
      return;
    }
    // Plain select.
    setSelected((m) => new Map(m).set(node.id, nodeFrom(node)));
  }

  function removeSelected(id: string) {
    setSelected((m) => {
      const n = new Map(m);
      n.delete(id);
      return n;
    });
    // Drop any exclusions that pointed at this parent.
    setExcluded((m) => {
      const n = new Map(m);
      for (const [child, parent] of n) if (parent === id) n.delete(child);
      return n;
    });
  }

  async function runSearch() {
    if (!connectionId) return;
    setSearching(true);
    setError("");
    try {
      const r = await api.workloadSearch({
        connection_id: connectionId,
        query: query.trim(),
        subscription_id: subFilter,
        types: typeFilter,
        locations: locFilter,
        top: 200,
      });
      if (r.error) setError(r.error);
      setSearchRows(r.rows);
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSearching(false);
    }
  }

  // Compose the final node list with exclusions folded into their parents.
  function applyNow() {
    const byId = new Map(selected);
    // Attach exclusions to their parent nodes.
    const exByParent = new Map<string, string[]>();
    for (const [child, parent] of excluded) {
      if (!byId.has(parent)) continue;
      exByParent.set(parent, [...(exByParent.get(parent) ?? []), child]);
    }
    const out: WorkloadNode[] = [];
    for (const [id, node] of byId) {
      out.push({ ...node, excludes: exByParent.get(id) ?? [] });
    }
    onApply(out);
  }

  const selectedList = useMemo(() => Array.from(selected.values()), [selected]);
  const subscriptions = subsQ.data?.nodes ?? [];
  // Resolve a subscription id → its friendly name (for the selected-tree headers).
  const subNameOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of subscriptions) m.set(s.id.toLowerCase(), s.name);
    for (const n of topNodes) if (n.kind === "subscription") m.set(n.id.toLowerCase(), n.name);
    return (id: string) => m.get((id || "").toLowerCase()) ?? id;
  }, [subscriptions, topNodes]);

  // Build flattened browse rows from the lazy tree.
  const rows: RowState[] = [];
  function pushRows(nodes: TreeNode[], depth: number, parentId: string | null) {
    for (const n of nodes) {
      rows.push({ node: n, depth, parentId });
      if (expanded.has(n.id)) {
        const kids = children.get(n.id);
        if (kids) pushRows(kids, depth + 1, n.id);
      }
    }
  }
  pushRows(topNodes, 0, null);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <div
        className="flex h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">Select resources</h2>
          <button onClick={onCancel} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        {/* Tabs + group-by */}
        <div className="flex items-center justify-between border-b px-6 py-2">
          <div className="flex gap-3 text-sm">
            <button
              onClick={() => setTab("browse")}
              className={tab === "browse" ? "font-semibold text-brand" : "text-gray-500 hover:text-gray-700"}
            >
              Browse
            </button>
            <button
              onClick={() => setTab("search")}
              className={tab === "search" ? "font-semibold text-brand" : "text-gray-500 hover:text-gray-700"}
            >
              Search
            </button>
          </div>
          {tab === "browse" && (
            <div className="flex items-center gap-2 text-xs text-gray-500">
              <span>Group by:</span>
              <div className="inline-flex overflow-hidden rounded-md border">
                {(["subscription", "mg"] as const).map((g) => (
                  <button
                    key={g}
                    onClick={() => {
                      if (groupBy === g) return;
                      setGroupBy(g);
                      setExpanded(new Set());
                      setChildren(new Map());
                    }}
                    className={`px-2.5 py-1 text-xs transition ${
                      groupBy === g
                        ? "bg-brand text-white"
                        : "bg-white text-gray-600 hover:bg-gray-50"
                    }`}
                  >
                    {g === "subscription" ? "Subscription" : "Management group"}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Filters (shared) */}
        <div className="flex flex-wrap items-center gap-2 border-b px-6 py-2">
          <select
            className="rounded-lg border px-2 py-1.5 text-xs"
            value={subFilter}
            onChange={(e) => setSubFilter(e.target.value)}
            title="Subscription filter"
          >
            <option value="">All subscriptions</option>
            {subscriptions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <MultiSelect
            label="Resource types"
            options={facetsQ.data?.types ?? []}
            selected={typeFilter}
            onChange={setTypeFilter}
            render={friendlyResourceType}
            icon={(t) => <AzureIcon kind="resource" type={t} className="h-3.5 w-3.5" />}
          />
          <MultiSelect
            label="Locations"
            options={facetsQ.data?.locations ?? []}
            selected={locFilter}
            onChange={setLocFilter}
            render={friendlyLocation}
          />
          {tab === "search" && (
            <div className="flex flex-1 items-center gap-2">
              <input
                className={input + " flex-1"}
                placeholder="Search to filter items…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void runSearch()}
              />
              <button
                onClick={() => void runSearch()}
                disabled={searching}
                className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-60"
              >
                {searching ? "Searching…" : "Search"}
              </button>
            </div>
          )}
        </div>

        {error && <div className="border-b bg-red-50 px-6 py-1.5 text-xs text-red-700">{error}</div>}

        {/* Cache freshness + live discovery bar */}
        <div className="flex items-center justify-between gap-2 border-b bg-gray-50/70 px-6 py-1.5 text-[11px]">
          <span className="flex min-w-0 items-center gap-1.5 text-gray-500">
            {prefetching || topLoading ? (
              <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
            ) : (
              <svg className="h-3 w-3 shrink-0 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
                <circle cx="10" cy="10" r="7" />
                <path d="M10 6v4l2.5 2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
            {prefetching || (topLoading && oldestCachedAt == null) ? (
              <span className="truncate text-gray-600">{discoveryStatus || "Discovering from Azure…"}</span>
            ) : oldestCachedAt == null ? (
              <span className="truncate">{discoveryStatus || "Loading from Azure…"}</span>
            ) : (
              <span className="truncate">
                Azure data cached{" "}
                <span className={cacheStale(now, oldestCachedAt) ? "font-bold text-red-600" : "text-gray-600"}>
                  {relAge(now, oldestCachedAt)}
                </span>
                {prefetchCounts && (prefetchCounts.subs > 0 || prefetchCounts.resources > 0) && (
                  <span className="ml-2 text-gray-400">
                    · {prefetchCounts.subs} subs · {prefetchCounts.rgs} RGs · {prefetchCounts.resources} resources
                  </span>
                )}
              </span>
            )}
          </span>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              onClick={() => void prefetchAll()}
              disabled={!connectionId || prefetching || refreshing}
              title="Pre-load the whole tree from Azure now, so expanding is instant"
              className="flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] text-gray-600 transition hover:bg-white disabled:opacity-60"
            >
              <span className={prefetching ? "inline-block animate-spin" : ""}>⤓</span>
              {prefetching ? "Prefetching…" : "Prefetch"}
            </button>
            <button
              onClick={() => void refreshAll()}
              disabled={!connectionId || refreshing || prefetching}
              title="Re-pull the resource tree from Azure (bypasses the cache)"
              className="flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] text-gray-600 transition hover:bg-white disabled:opacity-60"
            >
              <span className={refreshing ? "inline-block animate-spin" : ""}>↻</span>
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {/* Table */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-400">
              <tr>
                <th className="px-6 py-2 font-medium">Resource</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Location</th>
              </tr>
            </thead>
            <tbody>
              {tab === "browse" ? (
                topLoading ? (
                  <tr><td colSpan={3} className="px-6 py-6 text-center text-gray-400">Loading…</td></tr>
                ) : rows.length === 0 ? (
                  <tr><td colSpan={3} className="px-6 py-6 text-center text-gray-400">No items. Pick a connection.</td></tr>
                ) : (
                  rows.map(({ node, depth }) => {
                    // A node we've already expanded that returned no children is a leaf:
                    // hide its chevron so the user can't try to expand it again.
                    const loadedKids = children.get(node.id);
                    const knownEmpty = loadedKids !== undefined && loadedKids.length === 0;
                    return (
                      <BrowseRow
                        key={node.id}
                        node={node}
                        depth={depth}
                        expanded={expanded.has(node.id)}
                        loading={loading.has(node.id)}
                        checkState={isChecked(node)}
                        onToggleExpand={() => void toggleExpand(node)}
                        onToggleSelect={() => toggleSelect(node)}
                        hideChevron={knownEmpty}
                      />
                    );
                  })
                )
              ) : searchRows.length === 0 ? (
                <tr><td colSpan={3} className="px-6 py-6 text-center text-gray-400">{searching ? "Searching…" : "No results."}</td></tr>
              ) : (
                searchRows.map((node) => (
                  <BrowseRow
                    key={node.id}
                    node={node}
                    depth={0}
                    expanded={false}
                    loading={false}
                    checkState={isChecked(node)}
                    onToggleExpand={() => {}}
                    onToggleSelect={() => toggleSelect(node)}
                    hideChevron
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Selected tray */}
        <div className="border-t bg-gray-50 px-6 py-2">
          <details open>
            <summary className="cursor-pointer text-xs font-medium text-gray-600">
              Selected resources{" "}
              <span className="text-gray-400">
                {selectedList.length === 0 ? "No resources selected" : `${selectedList.length} selected`}
                {excluded.size > 0 ? ` · ${excluded.size} excluded` : ""}
              </span>
            </summary>
            <div className="mt-2 max-h-40 overflow-y-auto">
              <SelectedTree nodes={selectedList} onRemove={removeSelected} subName={subNameOf} />
            </div>
          </details>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t px-6 py-3">
          <button
            onClick={() => {
              setSelected(new Map());
              setExcluded(new Map());
            }}
            className="text-xs text-gray-500 hover:text-gray-700"
          >
            Clear all selections
          </button>
          <div className="flex gap-2">
            <button onClick={onCancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
            <button
              onClick={applyNow}
              disabled={selectedList.length === 0}
              className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function BrowseRow({
  node,
  depth,
  expanded,
  loading,
  checkState,
  onToggleExpand,
  onToggleSelect,
  hideChevron,
}: {
  node: TreeNode;
  depth: number;
  expanded: boolean;
  loading: boolean;
  checkState: boolean | "indeterminate";
  onToggleExpand: () => void;
  onToggleSelect: () => void;
  hideChevron?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = checkState === "indeterminate";
  }, [checkState]);
  const hasChevron = !hideChevron && node.has_children;
  return (
    <tr className="border-b hover:bg-gray-50">
      <td className="px-6 py-1.5">
        <div className="flex items-center gap-1.5" style={{ paddingLeft: depth * 18 }}>
          {hasChevron ? (
            <button onClick={onToggleExpand} className="text-gray-400 hover:text-gray-600">
              {loading ? "⋯" : expanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="w-3.5" />
          )}
          <input
            ref={ref}
            type="checkbox"
            checked={checkState === true}
            onChange={onToggleSelect}
            className="h-3.5 w-3.5"
          />
          <AzureIcon kind={node.kind} type={node.resource_type} className="h-4 w-4" />
          <span className="truncate text-gray-800">{node.name || node.id}</span>
        </div>
      </td>
      <td className="px-3 py-1.5 text-xs text-gray-500">
        {node.kind === "resource" ? friendlyResourceType(node.resource_type) : KIND_LABEL[node.kind]}
      </td>
      <td className="px-3 py-1.5 text-xs text-gray-500">{node.location ? friendlyLocation(node.location) : "-"}</td>
    </tr>
  );
}

/** A hierarchical (MG ▸ Sub ▸ RG ▸ Resource) view of the chosen nodes, so the selection
 * reads like the Azure tree instead of one flat line. Parent scope nodes (a whole sub /
 * RG) appear as their own rows; loose resources are grouped under synthesized
 * subscription → resource-group headers from each node's metadata. */
function SelectedTree({
  nodes,
  onRemove,
  subName,
}: {
  nodes: WorkloadNode[];
  onRemove: (id: string) => void;
  subName: (id: string) => string;
}) {
  if (nodes.length === 0) {
    return <div className="px-1 py-2 text-[11px] text-gray-400">Nothing selected yet.</div>;
  }

  function subOf(n: WorkloadNode): string {
    if (n.kind === "subscription") return n.id;
    return (n.subscription_id || "").toLowerCase();
  }
  function subLabel(n: WorkloadNode): string {
    if (n.kind === "subscription") return n.name || n.id;
    return n.subscription_id ? subName(n.subscription_id) : "(unknown subscription)";
  }

  const subs = new Map<
    string,
    {
      label: string;
      subSelf: WorkloadNode | null;
      rgs: Map<string, { rgSelf: WorkloadNode | null; resources: WorkloadNode[] }>;
    }
  >();
  const standaloneMgs: WorkloadNode[] = [];

  for (const n of nodes) {
    if (n.kind === "mg") {
      standaloneMgs.push(n);
      continue;
    }
    const sk = subOf(n) || "(none)";
    if (!subs.has(sk)) subs.set(sk, { label: subLabel(n), subSelf: null, rgs: new Map() });
    const entry = subs.get(sk)!;
    if (n.kind === "subscription") {
      entry.subSelf = n;
      entry.label = n.name || n.id;
      continue;
    }
    const rgKey = (n.kind === "resource_group" ? n.name : n.resource_group || "(none)") || "(none)";
    if (!entry.rgs.has(rgKey)) entry.rgs.set(rgKey, { rgSelf: null, resources: [] });
    const rg = entry.rgs.get(rgKey)!;
    if (n.kind === "resource_group") rg.rgSelf = n;
    else rg.resources.push(n);
  }

  const RemoveBtn = ({ id }: { id: string }) => (
    <button onClick={() => onRemove(id)} className="ml-auto shrink-0 text-gray-300 hover:text-red-500" title="Remove">
      ✕
    </button>
  );

  return (
    <div className="space-y-0.5 text-[11px]">
      {standaloneMgs.map((mg) => (
        <div key={mg.id} className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-white">
          <AzureIcon kind="mg" className="h-3.5 w-3.5" />
          <span className="truncate font-medium text-gray-700" title={mg.id}>{mg.name || mg.id}</span>
          <span className="rounded bg-gray-100 px-1 text-[10px] text-gray-400">management group</span>
          <RemoveBtn id={mg.id} />
        </div>
      ))}
      {[...subs.values()].map((s, i) => (
        <div key={i}>
          <div className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-white">
            <AzureIcon kind="subscription" className="h-3.5 w-3.5" />
            <span className="truncate font-medium text-gray-700" title={s.subSelf?.id}>{s.label}</span>
            {s.subSelf && <span className="rounded bg-brand/10 px-1 text-[10px] text-brand">whole subscription</span>}
            {s.subSelf && <RemoveBtn id={s.subSelf.id} />}
          </div>
          {[...s.rgs.entries()].map(([rgKey, rg]) => (
            <div key={rgKey} className="ml-4">
              <div className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-white">
                <AzureIcon kind="resource_group" className="h-3.5 w-3.5" />
                <span className="truncate text-gray-600" title={rg.rgSelf?.id}>{rgKey}</span>
                {rg.rgSelf && <span className="rounded bg-brand/10 px-1 text-[10px] text-brand">whole RG</span>}
                {rg.rgSelf && <RemoveBtn id={rg.rgSelf.id} />}
              </div>
              {rg.resources.map((r) => (
                <div key={r.id} className="ml-4 flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-white">
                  <AzureIcon kind="resource" type={r.resource_type} className="h-3.5 w-3.5" />
                  <span className="truncate text-gray-700" title={r.id}>{r.name || r.id}</span>
                  {r.resource_type && (
                    <span className="shrink-0 text-[10px] text-gray-400">{friendlyResourceType(r.resource_type)}</span>
                  )}
                  <RemoveBtn id={r.id} />
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
  render,
  icon,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (v: string[]) => void;
  render?: (v: string) => string;
  icon?: (v: string) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);
  // Focus the search box + clear the filter whenever the dropdown opens.
  useEffect(() => {
    if (open) {
      setFilter("");
      setTimeout(() => searchRef.current?.focus(), 0);
    }
  }, [open]);
  // Sort by the friendly display label so the dropdown reads naturally.
  const ordered = [...options].sort((a, b) =>
    (render ? render(a) : a).localeCompare(render ? render(b) : b),
  );
  const q = filter.trim().toLowerCase();
  // Match against BOTH the friendly label and the raw value.
  const shown = q
    ? ordered.filter((o) => (render ? render(o) : o).toLowerCase().includes(q) || o.toLowerCase().includes(q))
    : ordered;
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="rounded-lg border px-2 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
      >
        {label}
        {selected.length > 0 ? ` (${selected.length})` : ""} ▾
      </button>
      {open && (
        <div className="absolute z-50 mt-1 flex max-h-80 w-72 flex-col rounded-lg border bg-white shadow-xl">
          <div className="border-b p-1.5">
            <input
              ref={searchRef}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={`Filter ${label.toLowerCase()}…`}
              className="w-full rounded border px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-brand"
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-1">
            {options.length === 0 && <div className="px-2 py-2 text-[11px] text-gray-400">No options.</div>}
            {options.length > 0 && shown.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-gray-400">No matches for “{filter}”.</div>
            )}
            {shown.map((o) => (
              <label key={o} className="flex items-center gap-2 rounded px-2 py-1 text-xs hover:bg-gray-50">
                <input
                  type="checkbox"
                  checked={selected.includes(o)}
                  onChange={() =>
                    onChange(selected.includes(o) ? selected.filter((x) => x !== o) : [...selected, o])
                  }
                />
                {icon && icon(o)}
                <span className="truncate" title={o}>{render ? render(o) : o}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
