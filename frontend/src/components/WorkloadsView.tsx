import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type Workload, type WorkloadNode, type WorkloadNodeKind, type WorkloadProfile } from "../api";
import { formatError } from "../utils/format";
import { ResourcePicker } from "./ResourcePicker";
import { AutopilotModal, TypeChips, TYPE_LABELS, CRIT_OPTIONS } from "./AutopilotModal";
import { AzureIcon, friendlyResourceType } from "./AzureIcon";
import { WorkloadCard } from "./workloads/WorkloadCard";
import { FleetCockpit, matchesFleetFilter } from "./workloads/FleetCockpit";
import { WorkloadTable, WorkloadBoard } from "./workloads/WorkloadTableBoard";
import { ConstellationMap } from "./workloads/ConstellationMap";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

function countByKind(nodes: WorkloadNode[]): string {
  const c: Record<string, number> = {};
  for (const n of nodes) c[n.kind] = (c[n.kind] ?? 0) + 1;
  const order: WorkloadNodeKind[] = ["mg", "subscription", "resource_group", "resource"];
  const labels: Record<WorkloadNodeKind, string> = {
    mg: "MG",
    subscription: "sub",
    resource_group: "RG",
    resource: "resource",
  };
  return order
    .filter((k) => c[k])
    .map((k) => `${c[k]} ${labels[k]}${c[k] > 1 ? "s" : ""}`)
    .join(" · ");
}

// --- Resource tree (subscription → resource group → resource) -------------
/** Parse the subscription GUID and resource-group name out of an ARM resource id. */
function armScope(id: string): { sub: string; rg: string } {
  const sub = /\/subscriptions\/([^/]+)/i.exec(id)?.[1] ?? "";
  const rg = /\/resourcegroups\/([^/]+)/i.exec(id)?.[1] ?? "";
  return { sub, rg };
}

type TreeRG = { rg: string; scope?: WorkloadNode; resources: WorkloadNode[] };
type TreeSub = { sub: string; scope?: WorkloadNode; rgs: TreeRG[] };
type ResourceTree = { mgs: WorkloadNode[]; subs: TreeSub[] };

/** Group flat workload nodes into a subscription → resource-group → resource tree. */
function buildResourceTree(nodes: WorkloadNode[]): ResourceTree {
  const mgs: WorkloadNode[] = [];
  const subMap = new Map<string, TreeSub>();
  const ensureSub = (sub: string): TreeSub => {
    let s = subMap.get(sub);
    if (!s) {
      s = { sub, rgs: [] };
      subMap.set(sub, s);
    }
    return s;
  };
  const ensureRg = (s: TreeSub, rg: string): TreeRG => {
    let g = s.rgs.find((x) => x.rg.toLowerCase() === rg.toLowerCase());
    if (!g) {
      g = { rg, resources: [] };
      s.rgs.push(g);
    }
    return g;
  };

  for (const n of nodes) {
    if (n.kind === "mg") {
      mgs.push(n);
      continue;
    }
    const parsed = armScope(n.id);
    const sub = n.subscription_id || parsed.sub || "(unknown subscription)";
    if (n.kind === "subscription") {
      ensureSub(sub).scope = n;
      continue;
    }
    const rg = n.resource_group || parsed.rg || "(unknown group)";
    const s = ensureSub(sub);
    if (n.kind === "resource_group") {
      ensureRg(s, rg).scope = n;
    } else {
      ensureRg(s, rg).resources.push(n);
    }
  }

  const subs = [...subMap.values()].sort((a, b) => a.sub.localeCompare(b.sub));
  for (const s of subs) {
    s.rgs.sort((a, b) => a.rg.localeCompare(b.rg));
    for (const g of s.rgs) g.resources.sort((a, b) => a.name.localeCompare(b.name));
  }
  return { mgs, subs };
}

function WorkloadResourceTree({
  nodes,
  subName,
  onRemove,
}: {
  nodes: WorkloadNode[];
  subName: (sub: string) => string;
  onRemove: (id: string) => void;
}) {
  const tree = buildResourceTree(nodes);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setCollapsed((c) => {
      const next = new Set(c);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const RemoveBtn = ({ id }: { id: string }) => (
    <button
      onClick={() => onRemove(id)}
      className="ml-auto shrink-0 text-gray-300 hover:text-red-500"
      title="Remove from workload"
    >
      ✕
    </button>
  );

  return (
    <div className="max-h-72 space-y-0.5 overflow-y-auto rounded-lg border p-2 text-sm">
      {tree.mgs.map((m) => (
        <div key={m.id} className="flex items-center gap-2 rounded px-1 py-0.5 hover:bg-gray-50">
          <AzureIcon kind="mg" className="h-3.5 w-3.5" />
          <span className="truncate text-gray-800" title={m.id}>{m.name || m.id}</span>
          <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-500">management group</span>
          <RemoveBtn id={m.id} />
        </div>
      ))}

      {tree.subs.map((s) => {
        const subKey = `sub:${s.sub}`;
        const subOpen = !collapsed.has(subKey);
        return (
          <div key={s.sub}>
            <div className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-gray-50">
              <button onClick={() => toggle(subKey)} className="shrink-0 text-gray-400" aria-label={subOpen ? "Collapse" : "Expand"}>
                <span className={`inline-block transition-transform ${subOpen ? "rotate-90" : ""}`}>▸</span>
              </button>
              <AzureIcon kind="subscription" className="h-3.5 w-3.5" />
              <span className="truncate font-medium text-gray-800" title={s.sub}>{subName(s.sub)}</span>
              {s.scope && <span className="shrink-0 rounded bg-brand/10 px-1.5 py-0.5 text-[9px] text-brand">entire subscription</span>}
              {s.scope && <RemoveBtn id={s.scope.id} />}
            </div>

            {subOpen && (
              <div className="ml-4 border-l pl-2">
                {s.rgs.map((g) => {
                  const rgKey = `rg:${s.sub}/${g.rg}`;
                  const rgOpen = !collapsed.has(rgKey);
                  return (
                    <div key={g.rg}>
                      <div className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-gray-50">
                        <button onClick={() => toggle(rgKey)} className="shrink-0 text-gray-400" aria-label={rgOpen ? "Collapse" : "Expand"}>
                          <span className={`inline-block transition-transform ${rgOpen ? "rotate-90" : ""}`}>▸</span>
                        </button>
                        <AzureIcon kind="resource_group" className="h-3.5 w-3.5" />
                        <span className="truncate text-gray-700" title={g.rg}>{g.rg}</span>
                        {g.scope && <span className="shrink-0 rounded bg-brand/10 px-1.5 py-0.5 text-[9px] text-brand">entire group</span>}
                        {g.resources.length > 0 && <span className="shrink-0 text-[10px] text-gray-400">{g.resources.length}</span>}
                        {g.scope && <RemoveBtn id={g.scope.id} />}
                      </div>

                      {rgOpen && (
                        <div className="ml-4 border-l pl-2">
                          {g.resources.map((r) => (
                            <div key={r.id} className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-gray-50">
                              <AzureIcon kind="resource" type={r.resource_type} className="h-3.5 w-3.5" />
                              <span className="truncate text-gray-800" title={r.id}>{r.name || r.id}</span>
                              {r.resource_type && <span className="shrink-0 text-[10px] text-gray-400">{friendlyResourceType(r.resource_type)}</span>}
                              {r.excludes && r.excludes.length > 0 && <span className="shrink-0 text-[10px] text-amber-600">−{r.excludes.length} excluded</span>}
                              <RemoveBtn id={r.id} />
                            </div>
                          ))}
                          {g.resources.length === 0 && !g.scope && (
                            <div className="px-1 py-0.5 text-[11px] text-gray-400">(no resources)</div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


// Estate coverage: % of the Azure estate organized into workloads + orphaned-resource
// triage. Loads on demand (the estate scan is heavy) per connection — expanding the card
// only reveals a Scan button; the scan runs when the user explicitly presses it.
function EstateCoveragePanel() {
  const [open, setOpen] = useState(false);
  const [connId, setConnId] = useState("");
  // Which connection the user has explicitly asked to scan. The scan never auto-runs on
  // expand; it runs only after the Scan button is pressed (and re-arms when the connection
  // changes so we never silently scan a different tenant).
  const [scanConn, setScanConn] = useState("");
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections });
  const connections = connQ.data?.connections ?? [];
  const effConn = connId || connections.find((c) => c.is_default)?.id || connections[0]?.id || "";
  const armed = !!scanConn && scanConn === effConn;
  const covQ = useQuery({
    queryKey: ["estate-coverage", effConn],
    queryFn: () => api.estateCoverage(effConn),
    enabled: armed && !!effConn,
    staleTime: 5 * 60 * 1000,
  });
  const cov = armed ? covQ.data : undefined;
  const pct = cov?.organized_pct ?? 0;
  const barColor = pct >= 80 ? "bg-green-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500";

  return (
    <div className="rounded-xl border bg-white">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <span className="text-lg">📊</span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-gray-800">Estate coverage</div>
          <div className="text-xs text-gray-500">
            {cov ? `${cov.organized_pct}% organized · ${cov.orphaned} orphaned of ${cov.total}` : "How much of your estate is organized into workloads"}
          </div>
        </div>
        <span className={`text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
      </button>
      {open && (
        <div className="border-t px-4 py-3">
          {connections.length > 1 && (
            <select
              value={effConn}
              onChange={(e) => { setConnId(e.target.value); setScanConn(""); }}
              className="mb-3 rounded border px-2 py-1 text-xs text-gray-600"
            >
              {connections.map((c) => (
                <option key={c.id} value={c.id}>{c.display_name}</option>
              ))}
            </select>
          )}
          {!armed ? (
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-gray-500">
                Scan your Azure estate to see how much is organized into workloads and triage
                orphaned resources. This reads your resource graph and may take a moment.
              </p>
              <button
                onClick={() => setScanConn(effConn)}
                disabled={!effConn}
                className="shrink-0 rounded-lg bg-brand-dark px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-dark/90 disabled:opacity-50"
              >
                Scan estate
              </button>
            </div>
          ) : covQ.isLoading ? (
            <div className="text-sm text-gray-500">Scanning estate…</div>
          ) : covQ.isError ? (
            <div className="space-y-2">
              <div className="text-sm text-red-600">{formatError(covQ.error)}</div>
              <button
                onClick={() => covQ.refetch()}
                className="rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-600 transition hover:bg-gray-50"
              >
                Retry
              </button>
            </div>
          ) : cov ? (
            <div className="space-y-3">
              <div className="flex justify-end">
                <button
                  onClick={() => covQ.refetch()}
                  disabled={covQ.isFetching}
                  className="rounded-lg border px-2.5 py-1 text-[11px] font-medium text-gray-600 transition hover:bg-gray-50 disabled:opacity-50"
                >
                  {covQ.isFetching ? "Rescanning…" : "↻ Rescan"}
                </button>
              </div>
              <div>
                <div className="mb-1 flex items-center justify-between text-xs text-gray-500">
                  <span><b className="text-gray-700">{cov.organized}</b> organized · <b className="text-gray-700">{cov.orphaned}</b> orphaned</span>
                  <span className="font-semibold text-gray-700">{cov.organized_pct}%</span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-gray-100">
                  <div className={`h-full rounded-full ${barColor}`} style={{ width: `${pct}%` }} />
                </div>
                {cov.truncated && <p className="mt-1 text-[10px] text-gray-400">Estate exceeds 5,000 resources — coverage is for the first 5,000.</p>}
              </div>
              {cov.orphan_resource_groups.length > 0 && (
                <div>
                  <div className="mb-1 text-xs font-medium text-gray-600">Orphaned resources by resource group</div>
                  <div className="max-h-44 space-y-1 overflow-auto">
                    {cov.orphan_resource_groups.slice(0, 30).map((g) => (
                      <div key={g.resource_group} className="flex items-center justify-between rounded bg-gray-50 px-2 py-1 text-xs">
                        <span className="truncate text-gray-700">{g.resource_group}</span>
                        <span className="shrink-0 rounded bg-gray-200 px-1.5 text-[10px] tabular-nums text-gray-600">{g.count}</span>
                      </div>
                    ))}
                  </div>
                  <p className="mt-1.5 text-[11px] text-gray-400">
                    Run Autopilot again to fold these into workloads, or create one manually.
                  </p>
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}


// ---- Sort + classification filter config (workload fleet) ------------------------
// Sort options shown in the toolbar dropdown. "" = the backend's natural order (the
// table view keeps its own worst-health-first triage when no explicit sort is chosen).
const SORT_OPTIONS: { key: string; label: string }[] = [
  { key: "", label: "Default order" },
  { key: "name_asc", label: "Name (A → Z)" },
  { key: "name_desc", label: "Name (Z → A)" },
  { key: "created_desc", label: "Newest first" },
  { key: "created_asc", label: "Oldest first" },
  { key: "updated_desc", label: "Recently updated" },
  { key: "resources_desc", label: "Most resources" },
  { key: "resources_asc", label: "Fewest resources" },
  { key: "crit_desc", label: "Most critical" },
  { key: "health_asc", label: "Worst health first" },
];

// Faceted classification filter. Within a facet the selected values are OR'd; across
// facets they're AND'd (e.g. (production OR development) AND critical AND confidential).
const CLASS_FACETS: { facet: "environment" | "criticality" | "data_classification"; label: string; values: string[] }[] = [
  { facet: "environment", label: "Environment", values: ["production", "staging", "development", "test", "dr", "shared"] },
  { facet: "criticality", label: "Criticality", values: ["critical", "high", "medium", "low"] },
  { facet: "data_classification", label: "Data class", values: ["confidential", "internal", "public"] },
];

// Criticality ordering for the "Most critical" sort (higher = more critical).
const CRIT_RANK: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1 };


export function WorkloadsPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  // Preserve the page scroll position across navigation (e.g. open a workload then hit Back).
  const scrollRef = useRef<HTMLDivElement>(null);  const [editing, setEditing] = useState<Partial<Workload> | null>(null);
  const [autopilot, setAutopilot] = useState(false);
  const [refreshing, setRefreshing] = useState<string>("");
  const [msg, setMsg] = useState("");
  const [notice, setNotice] = useState("");
  const [showTrash, setShowTrash] = useState(false);
  const [view, setView] = useState<"cards" | "table" | "board" | "map">(() =>
    (localStorage.getItem("azsup.workloads.view") as "cards" | "table" | "board" | "map") || "cards");
  const [fleetFilter, setFleetFilter] = useState<string>("");  // category/env filter from cockpit
  const [search, setSearch] = useState<string>("");  // free-text filter over name/description/tags/type
  // Sort + faceted classification filter (persisted so the choice sticks across visits).
  const [sortKey, setSortKey] = useState<string>(() => localStorage.getItem("azsup.workloads.sort") || "");
  const setSort = (k: string) => { setSortKey(k); localStorage.setItem("azsup.workloads.sort", k); };
  const [classFilters, setClassFilters] = useState<Set<string>>(() => {
    try { return new Set<string>(JSON.parse(localStorage.getItem("azsup.workloads.classFilters") || "[]")); } catch { return new Set(); }
  });
  const persistClassFilters = (next: Set<string>) => {
    setClassFilters(next);
    localStorage.setItem("azsup.workloads.classFilters", JSON.stringify([...next]));
  };
  const toggleClassFilter = (token: string) => {
    const next = new Set(classFilters);
    if (next.has(token)) next.delete(token); else next.add(token);
    persistClassFilters(next);
  };
  const clearClassFilters = () => persistClassFilters(new Set());
  // Saved fleet views: named {view mode + active filter} the user can recall in one click.
  const [savedViews, setSavedViews] = useState<{ name: string; view: string; filter: string }[]>(() => {
    try { return JSON.parse(localStorage.getItem("azsup.workloads.savedViews") || "[]"); } catch { return []; }
  });
  const persistViews = (v: typeof savedViews) => {
    setSavedViews(v);
    localStorage.setItem("azsup.workloads.savedViews", JSON.stringify(v));
  };
  const saveCurrentView = () => {
    const name = window.prompt("Name this view (layout + current filter):", fleetFilter ? `${view} · ${fleetFilter.replace(":", " ")}` : view);
    if (!name) return;
    persistViews([...savedViews.filter((s) => s.name !== name), { name, view, filter: fleetFilter }]);
  };
  const applyView = (s: { view: string; filter: string }) => {
    setViewMode(s.view as "cards" | "table" | "board" | "map");
    setFleetFilter(s.filter);
  };

  const workloads = wlQ.data?.workloads ?? [];

  // Restore the saved scroll position once the workload list has rendered (so the content is
  // tall enough to scroll). Runs when data first arrives; the stored value is set on scroll.
  const dataReady = wlQ.isSuccess;
  useEffect(() => {
    if (!dataReady) return;
    const el = scrollRef.current;
    if (!el) return;
    const saved = Number(sessionStorage.getItem("azsup.workloads.scrollTop") || 0);
    if (saved > 0) {
      // Wait a frame so card heights/images are laid out before scrolling.
      requestAnimationFrame(() => {
        el.scrollTop = saved;
      });
    }
  }, [dataReady]);

  // Cache-only command-center profiles for the whole fleet — ONE request powers every card,
  // the cockpit strip and the table. Never scans Azure.
  const profilesQ = useQuery({
    queryKey: ["workloadProfiles"],
    queryFn: () => api.workloadProfiles([]),
    enabled: workloads.length > 0 && !showTrash,
    staleTime: 60_000,
  });
  const profileById: Record<string, WorkloadProfile> = {};
  for (const p of profilesQ.data?.profiles ?? []) profileById[p.id] = p;

  // Lightweight (Tier-1, no Azure calls) duplicate-resource check → drives the banner + button.
  const overlapsQ = useQuery({
    queryKey: ["workloadOverlaps", "", false],
    queryFn: () => api.workloadOverlaps("", false),
    enabled: workloads.length > 1 && !showTrash,
    staleTime: 60_000,
  });
  const overlapCount = overlapsQ.data?.summary?.duplicated_resources ?? 0;

  // Free-text search over a workload's name / description / tags / classification fields.
  const matchesSearch = (w: Workload): boolean => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    const hay = [
      w.name,
      w.description,
      w.workload_type,
      w.environment,
      w.criticality,
      w.data_classification,
      ...(w.tags ?? []),
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  };
  // Faceted classification filter: a workload's environment/criticality/data-class value
  // (own field, falling back to its profile) must be one of the selected values in every
  // facet that has any selection. No selection in a facet ⇒ that facet doesn't constrain.
  const classOf = (w: Workload, facet: "environment" | "criticality" | "data_classification"): string => {
    const own = (w[facet] || "").toLowerCase();
    if (own) return own;
    const p = profileById[w.id]?.classification;
    return (p ? (p[facet] || "") : "").toLowerCase();
  };
  const matchesClass = (w: Workload): boolean => {
    if (classFilters.size === 0) return true;
    for (const { facet, values } of CLASS_FACETS) {
      const active = values.filter((v) => classFilters.has(`${facet}:${v}`));
      if (active.length === 0) continue;
      if (!active.includes(classOf(w, facet))) return false;
    }
    return true;
  };

  // Resource count for sorting: prefer the profile's true total, else count resource nodes.
  const resourceCount = (w: Workload): number =>
    profileById[w.id]?.composition.total ?? (w.nodes || []).filter((n) => n.kind === "resource").length;
  const ts = (s?: string): number => (s ? new Date(s).getTime() || 0 : 0);
  // Apply the chosen sort. "" keeps the backend order (table view still triages by health).
  const sortWorkloads = (list: Workload[]): Workload[] => {
    if (!sortKey) return list;
    const arr = [...list];
    arr.sort((a, b) => {
      switch (sortKey) {
        case "name_asc": return a.name.localeCompare(b.name);
        case "name_desc": return b.name.localeCompare(a.name);
        case "created_desc": return ts(b.created_at) - ts(a.created_at);
        case "created_asc": return ts(a.created_at) - ts(b.created_at);
        case "updated_desc": return ts(b.updated_at || b.last_refreshed) - ts(a.updated_at || a.last_refreshed);
        case "resources_desc": return resourceCount(b) - resourceCount(a);
        case "resources_asc": return resourceCount(a) - resourceCount(b);
        case "crit_desc": return (CRIT_RANK[classOf(b, "criticality")] ?? 0) - (CRIT_RANK[classOf(a, "criticality")] ?? 0);
        case "health_asc": return (profileById[a.id]?.health.score ?? -1) - (profileById[b.id]?.health.score ?? -1);
        default: return 0;
      }
    });
    return arr;
  };
  // The visible set: fleet-cockpit filter AND free-text search AND classification facets,
  // then sorted. Used everywhere below.
  const visibleWorkloads = sortWorkloads(
    workloads.filter((w) => matchesFleetFilter(profileById[w.id], fleetFilter) && matchesSearch(w) && matchesClass(w)),
  );

  const setViewMode = (v: "cards" | "table" | "board" | "map") => {
    setView(v);
    localStorage.setItem("azsup.workloads.view", v);
  };

  const trashQ = useQuery({
    queryKey: ["workloadsTrash"],
    queryFn: api.trashedWorkloads,
    enabled: showTrash,
  });
  const trashed = trashQ.data?.workloads ?? [];

  async function remove(id: string) {
    try {
      await api.deleteWorkload(id);
      qc.invalidateQueries({ queryKey: ["workloads"] });
      qc.invalidateQueries({ queryKey: ["workloadsTrash"] });
      setNotice("Moved to Trash. You can restore it from the Trash view.");
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function restore(id: string) {
    try {
      await api.restoreWorkload(id);
      qc.invalidateQueries({ queryKey: ["workloads"] });
      qc.invalidateQueries({ queryKey: ["workloadsTrash"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function purge(id: string) {
    if (!window.confirm("Permanently delete this workload? This cannot be undone.")) return;
    try {
      await api.purgeWorkload(id);
      qc.invalidateQueries({ queryKey: ["workloadsTrash"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function emptyTrash() {
    if (!window.confirm("Permanently delete ALL workloads in the Trash? This cannot be undone.")) return;
    try {
      const r = await api.emptyWorkloadTrash();
      setNotice(`Emptied Trash — permanently deleted ${r.deleted} workload${r.deleted === 1 ? "" : "s"}.`);
      qc.invalidateQueries({ queryKey: ["workloadsTrash"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function refresh(id: string) {
    setRefreshing(id);
    setMsg("");
    setNotice("");
    try {
      const r = await api.refreshWorkload(id);
      const d = r.diff;
      setNotice(
        `Refreshed “${r.workload.name}”: +${d.added_count} added, −${d.removed_count} removed ` +
          `(scanned ${d.scanned_resource_groups} resource group${d.scanned_resource_groups === 1 ? "" : "s"}).`,
      );
      qc.invalidateQueries({ queryKey: ["workloads"] });
    } catch (e) {
      setMsg(formatError(e));
    } finally {
      setRefreshing("");
    }
  }

  // Fleet mission selection.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggleSelected = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  async function launchFleet() {
    if (selected.size === 0) return;
    setMsg("");
    setNotice("");
    try {
      const r = await api.runFleet({ workload_ids: Array.from(selected) });
      setNotice(`🚀 Launched ${r.launched} mission${r.launched === 1 ? "" : "s"}. Open a workload's Mission Control to watch progress.`);
      setSelected(new Set());
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  // Merge 2+ selected workloads into one new workload (originals move to Trash). The merged
  // workload is a normal workload, so architecture, missions and assessments can all be
  // re-run against it. Its name gets a trailing "MERGED" marker.
  async function mergeSelected() {
    if (selected.size < 2) return;
    const names = workloads.filter((w) => selected.has(w.id)).map((w) => w.name);
    const suggested = names.join(" + ");
    const name = window.prompt(
      `Name the merged workload (“ MERGED” is appended automatically):`,
      suggested,
    );
    if (name === null) return;
    setMsg("");
    setNotice("");
    try {
      const r = await api.mergeWorkloads({ workload_ids: Array.from(selected), name: name.trim() });
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["workloads"] });
      qc.invalidateQueries({ queryKey: ["workloadProfiles"] });
      qc.invalidateQueries({ queryKey: ["workloadsTrash"] });
      setNotice(
        `Merged ${names.length} workloads into “${r.workload.name}”. The originals are in Trash. ` +
          `Opening it now — run Refresh, Mission Control, Assess and architecture again from there.`,
      );
      navigate(`/workloads/${r.workload.id}`);
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  return (
    <div
      ref={scrollRef}
      onScroll={(e) => sessionStorage.setItem("azsup.workloads.scrollTop", String(e.currentTarget.scrollTop))}
      className="h-full overflow-y-auto bg-gray-50"
    >
      <div className="space-y-5 p-8">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-800">Azure Workloads</h1>
            <p className="mt-1 text-sm text-gray-500">
              A workload is a hand-picked set of Azure resources — a customer product or
              application. Use it as an optional scope when chatting, so queries are limited
              to just that workload's resources.
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              onClick={() => setAutopilot(true)}
              className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5"
            >
              ✨ Autopilot
            </button>
            <button
              onClick={() => setEditing({ name: "", description: "", connection_id: "", nodes: [], tags: [] })}
              className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
            >
              + New workload
            </button>
            <button
              onClick={() => navigate("/workloads/overlaps")}
              className={`rounded-lg border px-3 py-1.5 text-sm font-medium ${
                overlapCount > 0 ? "border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100" : "border-gray-300 text-gray-600 hover:bg-gray-50"
              }`}
              title="Resources that belong to more than one workload"
            >
              🧩 Overlaps{overlapCount > 0 ? ` (${overlapCount})` : ""}
            </button>
            <button
              onClick={() => setShowTrash((s) => !s)}
              className={`rounded-lg border px-3 py-1.5 text-sm font-medium ${
                showTrash ? "border-gray-400 bg-gray-100 text-gray-800" : "border-gray-300 text-gray-600 hover:bg-gray-50"
              }`}
            >
              🗑 Trash
            </button>
          </div>
        </div>

        {notice && (
          <div className="flex items-center justify-between rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700">
            <span>{notice}</span>
            <button onClick={() => setNotice("")} className="text-green-500 hover:text-green-700">✕</button>
          </div>
        )}
        {msg && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{msg}</div>}
        {!showTrash && overlapCount > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            <span>⚠</span>
            <span>
              <b>{overlapCount}</b> resource{overlapCount === 1 ? " is" : "s are"} in more than one workload
              {(overlapsQ.data?.summary?.total_extra_memberships ?? 0) > 0 && <> — {overlapsQ.data!.summary.total_extra_memberships} membership{overlapsQ.data!.summary.total_extra_memberships === 1 ? "" : "s"} could be removed</>}.
            </span>
            <button onClick={() => navigate("/workloads/overlaps")} className="ml-auto rounded-lg bg-amber-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-700">Review overlaps</button>
          </div>
        )}
        {wlQ.isLoading && <div className="text-sm text-gray-500">Loading…</div>}

        {/* Front door: first-run onboarding. When the estate has no workloads yet, lead
            with Autopilot so new users map their whole estate in one motion. */}
        {!wlQ.isLoading && workloads.length === 0 && !showTrash && (
          <div className="rounded-xl border border-brand/30 bg-gradient-to-br from-brand/5 to-transparent p-6">
            <div className="flex items-start gap-4">
              <div className="text-3xl">✨</div>
              <div className="min-w-0 flex-1">
                <h2 className="text-base font-semibold text-gray-800">Map your Azure estate with Autopilot</h2>
                <p className="mt-1 text-sm text-gray-500">
                  Point Autopilot at a subscription or management group and it discovers your
                  workloads automatically — grouping resources by dependencies, naming, tags and
                  deployment markers, then classifying each by type, environment and criticality.
                  Review, then save. You can optionally assess each one right away.
                </p>
                <button
                  onClick={() => setAutopilot(true)}
                  className="mt-3 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand/90"
                >
                  ✨ Run Autopilot
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Estate coverage: how much of the estate is organized into workloads + orphan triage. */}
        {!wlQ.isLoading && workloads.length > 0 && !showTrash && <EstateCoveragePanel />}

        {/* Fleet cockpit: aggregate health/composition/triage over the cache-only profiles. */}
        {!showTrash && workloads.length > 0 && (profilesQ.data?.profiles?.length ?? 0) > 0 && (
          <FleetCockpit profiles={profilesQ.data!.profiles} onFilter={setFleetFilter} activeFilter={fleetFilter} />
        )}

        {selected.size > 0 && (
          <div className="flex items-center gap-3 rounded-lg border border-brand/30 bg-brand/5 px-3 py-2 text-sm">
            <span className="font-medium text-brand">{selected.size} selected</span>
            <button onClick={launchFleet} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-dark">
              🚀 Launch missions
            </button>
            {selected.size >= 2 && (
              <button onClick={mergeSelected} className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-xs font-semibold text-brand hover:bg-brand/5" title="Merge the selected workloads into one new workload (originals move to Trash)">
                ⛙ Merge {selected.size} → 1
              </button>
            )}
            <button onClick={() => setSelected(new Set())} className="text-xs text-gray-500 hover:text-gray-700">Clear</button>
          </div>
        )}

        {/* View toolbar: layout modes + active filter pill. */}
        {!showTrash && workloads.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
              {(["cards", "table", "board", "map"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setViewMode(v)}
                  className={`rounded-md px-2.5 py-1 capitalize ${view === v ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"}`}
                >
                  {v === "cards" ? "\u25a6 Cards" : v === "table" ? "\u25a4 Table" : v === "board" ? "\u25a5 Board" : "\u2735 Map"}
                </button>
              ))}
            </div>
            {(() => {
              const visible = visibleWorkloads;
              const allSelected = visible.length > 0 && visible.every((w) => selected.has(w.id));
              return (
                <button
                  onClick={() =>
                    setSelected((s) => {
                      const n = new Set(s);
                      if (allSelected) visible.forEach((w) => n.delete(w.id));
                      else visible.forEach((w) => n.add(w.id));
                      return n;
                    })
                  }
                  className="rounded-lg border px-2.5 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50"
                  title={allSelected ? "Deselect all shown workloads" : "Select all shown workloads"}
                >
                  {allSelected ? "☑ Deselect all" : "☐ Select all"}
                </button>
              );
            })()}
            {/* Free-text search — filters the workloads shown across every view. */}
            <div className="relative">
              <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-xs text-gray-400">⌕</span>
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search workloads…"
                className="w-52 rounded-lg border py-1 pl-6 pr-6 text-xs"
              />
              {search && (
                <button onClick={() => setSearch("")} title="Clear" className="absolute right-1.5 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-600">✕</button>
              )}
            </div>
            {/* Sort — affects cards, table, board and map. */}
            <label className="inline-flex items-center gap-1 text-xs text-gray-500">
              <span className="text-gray-400">↕</span>
              <select
                value={sortKey}
                onChange={(e) => setSort(e.target.value)}
                className="rounded-lg border bg-white py-1 pl-1.5 pr-5 text-xs text-gray-600"
                title="Sort workloads"
              >
                {SORT_OPTIONS.map((o) => (
                  <option key={o.key} value={o.key}>{o.label}</option>
                ))}
              </select>
            </label>
            {fleetFilter && (
              <button onClick={() => setFleetFilter("")} className="inline-flex items-center gap-1 rounded-full bg-brand/10 px-2.5 py-1 text-xs font-medium text-brand">
                Filter: {fleetFilter.replace(":", " · ")} ✕
              </button>
            )}
            {/* Saved views: recall a named layout+filter; star the current one. */}
            {savedViews.map((s) => (
              <span key={s.name} className="inline-flex items-center rounded-full bg-gray-100 text-xs">
                <button onClick={() => applyView(s)} className="py-1 pl-2.5 pr-1 font-medium text-gray-600 hover:text-brand" title="Apply saved view">★ {s.name}</button>
                <button onClick={() => persistViews(savedViews.filter((x) => x.name !== s.name))} className="py-1 pr-2 pl-0.5 text-gray-400 hover:text-red-500" title="Remove">✕</button>
              </span>
            ))}
            <button onClick={saveCurrentView} className="rounded-full border border-dashed px-2.5 py-1 text-xs text-gray-500 hover:border-brand hover:text-brand" title="Save the current layout + filter as a view">+ Save view</button>
            <span className="ml-auto text-xs text-gray-400">
              {visibleWorkloads.length} of {workloads.length}
              {profilesQ.isFetching && " · refreshing profiles…"}
            </span>
          </div>
        )}

        {/* Classification filters: production / critical / confidential / development / … */}
        {!showTrash && workloads.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {CLASS_FACETS.map(({ facet, label, values }) => (
              <div key={facet} className="flex flex-wrap items-center gap-1">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">{label}</span>
                {values.map((v) => {
                  const token = `${facet}:${v}`;
                  const on = classFilters.has(token);
                  return (
                    <button
                      key={token}
                      onClick={() => toggleClassFilter(token)}
                      className={`rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize transition ${
                        on ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-500 hover:bg-gray-50"
                      }`}
                    >
                      {v}
                    </button>
                  );
                })}
              </div>
            ))}
            {classFilters.size > 0 && (
              <button onClick={clearClassFilters} className="text-[11px] font-medium text-gray-400 hover:text-brand">
                Clear filters ✕
              </button>
            )}
          </div>
        )}

        {!showTrash && view === "cards" && (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visibleWorkloads.map((w) => (
              <WorkloadCard
                key={w.id}
                w={w}
                profile={profileById[w.id]}
                selected={selected.has(w.id)}
                onToggleSelect={() => toggleSelected(w.id)}
                onOpen={() => navigate(`/workloads/${w.id}`)}
                onRefresh={() => void refresh(w.id)}
                onEdit={() => setEditing(w)}
                onDelete={() => void remove(w.id)}
                onMission={() => navigate(`/mission-control/${w.id}`)}
                onAssess={() => { sessionStorage.setItem("azsup.assessWorkload", w.id); navigate("/assessments"); }}
                refreshing={refreshing === w.id}
              />
            ))}
          </div>
        )}

        {!showTrash && view === "table" && (
          <WorkloadTable
            workloads={visibleWorkloads}
            profileById={profileById}
            onOpen={(id) => navigate(`/workloads/${id}`)}
            respectOrder={sortKey !== ""}
          />
        )}

        {!showTrash && view === "board" && (
          <WorkloadBoard
            workloads={visibleWorkloads}
            profileById={profileById}
            onOpen={(id) => navigate(`/workloads/${id}`)}
          />
        )}

        {!showTrash && view === "map" && (
          <ConstellationMap
            workloads={visibleWorkloads}
            profileById={profileById}
            onOpen={(id) => navigate(`/workloads/${id}`)}
          />
        )}

        {!showTrash && workloads.length > 0 && visibleWorkloads.length === 0 && (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
            No workloads match {search.trim() ? <>“<span className="font-medium">{search}</span>”</> : "the current filter"}.{" "}
            <button onClick={() => { setSearch(""); setFleetFilter(""); clearClassFilters(); }} className="font-medium text-brand hover:underline">Clear filters</button>
          </div>
        )}

        {workloads.length === 0 && !wlQ.isLoading && (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
            No workloads yet. Use <span className="font-medium text-brand">✨ Autopilot</span> to
            auto-discover them, or create one manually.
          </div>
        )}

        {showTrash && (
          <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="flex items-center gap-2 text-sm font-semibold text-gray-800">
                  🗑 Trash
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                    {trashed.length}
                  </span>
                </h2>
                <p className="mt-0.5 text-xs text-gray-500">
                  Deleted workloads are kept here until you restore or permanently delete them.
                </p>
              </div>
              {trashed.length > 0 && (
                <button
                  onClick={() => void emptyTrash()}
                  className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50"
                >
                  Empty trash
                </button>
              )}
            </div>
            {trashQ.isLoading ? (
              <div className="text-sm text-gray-500">Loading…</div>
            ) : trashed.length === 0 ? (
              <div className="rounded-lg border border-dashed p-6 text-center text-sm text-gray-400">
                Trash is empty.
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {trashed.map((w) => (
                  <div key={w.id} className="rounded-xl border border-gray-200 bg-gray-50 p-4">
                    <div className="min-w-0">
                      <span className="truncate font-semibold text-gray-700">{w.name}</span>
                      <p className="mt-0.5 line-clamp-2 text-xs text-gray-500">{w.description}</p>
                    </div>
                    <div className="mt-2 text-[11px] text-gray-500">
                      {w.summary && (w.summary.types?.length ?? 0) > 0 ? (
                        <TypeChips types={w.summary.types} max={6} />
                      ) : (
                        countByKind(w.nodes) || "No resources"
                      )}
                    </div>
                    {w.deleted_at && (
                      <div className="mt-1.5 text-[10px] text-gray-400">
                        Deleted {new Date(w.deleted_at).toLocaleString()}
                      </div>
                    )}
                    <div className="mt-3 flex items-center gap-2">
                      <button
                        onClick={() => void restore(w.id)}
                        className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10"
                      >
                        ↩ Restore
                      </button>
                      <button
                        onClick={() => void purge(w.id)}
                        className="ml-auto rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50"
                      >
                        Delete forever
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {editing && (
        <WorkloadForm
          value={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["workloads"] });
          }}
        />
      )}
      {autopilot && (
        <AutopilotModal
          onClose={() => setAutopilot(false)}
          onSaved={() => {
            setAutopilot(false);
            qc.invalidateQueries({ queryKey: ["workloads"] });
            setNotice("Saved discovered workloads.");
          }}
        />
      )}
    </div>
  );
}

export function WorkloadForm({
  value,
  onClose,
  onSaved,
}: {
  value: Partial<Workload>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Partial<Workload>>(value);
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(false);
  const set = (patch: Partial<Workload>) => setForm((f) => ({ ...f, ...patch }));
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections });
  const connections = connQ.data?.connections ?? [];

  // Subscription display names (GUID → name) for the resource tree, from the cached
  // top-level subscription list for the selected connection.
  const subsQ = useQuery({
    queryKey: ["workloadSubs", form.connection_id],
    queryFn: () => api.workloadTree({ connection_id: form.connection_id!, group_by: "subscription" }),
    enabled: !!form.connection_id,
  });
  const subNameMap = new Map<string, string>();
  for (const sn of subsQ.data?.nodes ?? []) {
    const guid = sn.subscription_id || armScope(sn.id).sub || sn.id;
    if (guid) subNameMap.set(guid.toLowerCase(), sn.name || guid);
  }
  const subName = (sub: string) => subNameMap.get(sub.toLowerCase()) || sub;

  // Default-select the connection when exactly one exists (and none is chosen yet). With
  // multiple connections the user must choose explicitly.
  useEffect(() => {
    if (!form.id && !form.connection_id && connections.length === 1) {
      set({ connection_id: connections[0].id });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connections.length]);

  const nodes = form.nodes ?? [];

  async function save() {
    if (!form.name?.trim()) {
      setError("Give the workload a name.");
      return;
    }
    if (!form.connection_id) {
      setError("Pick an Azure connection.");
      return;
    }
    try {
      await api.upsertWorkload(form);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">
            {form.id ? "Edit workload" : "New workload"}
          </h2>
          <button onClick={onClose} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-5">
          <div>
            <label className={label}>Name</label>
            <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} placeholder="e.g. Contoso Checkout" />
          </div>
          <div>
            <label className={label}>Description</label>
            <input className={input} value={form.description ?? ""} onChange={(e) => set({ description: e.target.value })} />
          </div>
          <div>
            <label className={label}>Azure connection (tenant)</label>
            <select className={input} value={form.connection_id ?? ""} onChange={(e) => set({ connection_id: e.target.value })}>
              <option value="">Select a connection…</option>
              {connections.map((c) => (
                <option key={c.id} value={c.id}>{c.display_name}</option>
              ))}
            </select>
          </div>

          {/* Classification — the type/environment/criticality/data-class pills shown on cards. */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={label}>Type</label>
              <select className={input} value={form.workload_type ?? ""} onChange={(e) => set({ workload_type: e.target.value })}>
                <option value="">—</option>
                {Object.entries(TYPE_LABELS).map(([v, lbl]) => (
                  <option key={v} value={v}>{lbl}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={label}>Environment</label>
              <select className={input} value={form.environment ?? ""} onChange={(e) => set({ environment: e.target.value })}>
                {["", "production", "staging", "development", "test", "dr", "shared", "unknown"].map((v) => (
                  <option key={v} value={v}>{v ? v[0].toUpperCase() + v.slice(1) : "—"}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={label}>Criticality</label>
              <select className={input} value={form.criticality ?? ""} onChange={(e) => set({ criticality: e.target.value })}>
                {CRIT_OPTIONS.map((v) => (
                  <option key={v} value={v}>{v ? v[0].toUpperCase() + v.slice(1) : "—"}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={label}>Data classification</label>
              <select className={input} value={form.data_classification ?? ""} onChange={(e) => set({ data_classification: e.target.value })}>
                {["", "confidential", "internal", "public", "unknown"].map((v) => (
                  <option key={v} value={v}>{v ? v[0].toUpperCase() + v.slice(1) : "—"}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Tags — comma-separated free-text labels. */}
          <div>
            <label className={label}>Tags <span className="font-normal text-gray-400">(comma-separated)</span></label>
            <input
              className={input}
              value={(form.tags ?? []).join(", ")}
              onChange={(e) => set({ tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })}
              placeholder="e.g. gis, billing, pci"
            />
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <label className={label + " mb-0"}>Resources ({nodes.length})</label>
              <button
                onClick={() => {
                  if (!form.connection_id) {
                    setError("Pick an Azure connection first.");
                    return;
                  }
                  setError("");
                  setPicking(true);
                }}
                className="rounded-lg border border-brand/40 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/5"
              >
                Select resources
              </button>
            </div>
            {nodes.length === 0 ? (
              <p className="rounded-lg border border-dashed p-4 text-center text-[11px] text-gray-400">
                No resources selected. Click “Select resources” to pick from your Azure tree.
              </p>
            ) : (
              <WorkloadResourceTree
                nodes={nodes}
                subName={subName}
                onRemove={(id) => set({ nodes: nodes.filter((x) => x.id !== id) })}
              />
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
          {error && <div className="mr-auto text-xs text-red-600">{error}</div>}
          <button onClick={onClose} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
        </div>
      </div>

      {picking && (
        <ResourcePicker
          connectionId={form.connection_id ?? ""}
          initialNodes={nodes}
          onApply={(picked) => {
            set({ nodes: picked });
            setPicking(false);
          }}
          onCancel={() => setPicking(false)}
        />
      )}
    </div>
  );
}
