import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  api,
  streamRbacRefresh,
  type RbacOverview,
  type RbacRow,
  type RbacScopeFreshness,
  type RbacScopeNode,
  type RbacProgress,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { RBAC_NAV, type RbacTab } from "./navConfig";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { AzureIcon } from "./AzureIcon";

// Active connection/tenant scope for the whole RBAC review. "" => default connection.
// Shared via context so every tab + the refresh stream re-scope together without prop drilling.
const RbacConnectionContext = createContext<string>("");
const useRbacConnectionId = () => useContext(RbacConnectionContext) || null;

// ---- helpers --------------------------------------------------------------------
function agoText(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const STATUS_CLS: Record<string, string> = {
  Succeeded: "bg-green-100 text-green-700",
  SucceededWithWarnings: "bg-amber-100 text-amber-700",
  PartiallyCollected: "bg-amber-100 text-amber-700",
  Skipped: "bg-gray-100 text-gray-600",
  Unauthorized: "bg-orange-100 text-orange-700",
  Throttled: "bg-orange-100 text-orange-700",
  Failed: "bg-red-100 text-red-700",
};

function StatusPill({ status }: { status: string }) {
  const cls = STATUS_CLS[status] ?? "bg-sky-100 text-sky-700";
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>{status || "—"}</span>;
}

function StaleBadge({ stale, age }: { stale?: boolean; age: number | null }) {
  if (age == null) return <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">never</span>;
  const cls = stale ? "bg-amber-100 text-amber-700" : "bg-green-100 text-green-700";
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>updated {agoText(age)}</span>;
}

function PrivBadge({ row }: { row: RbacRow }) {
  if (row.roleIsPrivileged) return <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700">privileged</span>;
  if (row.roleHasDataActions) return <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">data</span>;
  return null;
}

const PATH_LABEL: Record<string, string> = {
  Direct: "Direct",
  GroupTransitive: "via group",
  Owner: "owner",
};

// Friendly Scope-column rendering: classify by the row's scopeType and prefix the name —
// "Subscription: <name>", "RG: <name>", "MG: <name>" — with the matching Azure scope icon.
function scopeCell(r: RbacRow): { icon: "mg" | "subscription" | "resource_group" | "resource" | "tenant" | null; label: string } {
  const str = (k: string) => (typeof r[k] === "string" ? (r[k] as string).trim() : "");
  switch (str("scopeType")) {
    case "subscription":
      return { icon: "subscription", label: `Subscription: ${str("subscriptionName") || str("subscriptionId") || "—"}` };
    case "resourceGroup":
      return { icon: "resource_group", label: `RG: ${str("resourceGroup") || "—"}` };
    case "managementGroup":
      return { icon: "mg", label: `MG: ${str("managementGroupName") || str("scopeDisplayName") || str("managementGroupId") || "—"}` };
    case "resource":
      return { icon: "resource", label: str("resourceName") || str("scopeDisplayName") || str("scope") || "Resource" };
    case "tenantRoot":
      return { icon: "tenant", label: str("scopeDisplayName") || "Tenant Root Group" };
    case "directory":
      return { icon: null, label: "Directory" };
    default:
      return { icon: null, label: str("scopeDisplayName") || str("subscriptionName") || str("scope") || "directory" };
  }
}

function KpiTile({ label, value, tone }: { label: string; value: number; tone?: "red" | "amber" | "sky" }) {
  const toneCls = tone === "red" ? "text-red-600" : tone === "amber" ? "text-amber-600" : tone === "sky" ? "text-sky-600" : "text-gray-900";
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`text-xl font-semibold ${toneCls}`}>{value.toLocaleString()}</div>
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
    </div>
  );
}

// ---- per-scope refresh hook -----------------------------------------------------
function useRbacRefresh() {
  const qc = useQueryClient();
  const connectionId = useRbacConnectionId();
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [log, setLog] = useState<RbacProgress[]>([]);
  const [activeLabel, setActiveLabel] = useState<string>("");
  const abortRef = useRef<AbortController | null>(null);

  const invalidate = () => {
    for (const k of ["overview", "scopes", "access", "pivots", "roles", "diagnostics", "runs"]) {
      qc.invalidateQueries({ queryKey: ["rbac", k] });
    }
  };

  async function run(params: { scope?: string; mode: string; display_name?: string }, key: string, label: string) {
    if (refreshing.has(key)) return;
    setRefreshing((s) => new Set(s).add(key));
    setActiveLabel(label);
    setLog([]);
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    await streamRbacRefresh(
      { ...params, connection_id: connectionId ?? undefined },
      {
        onProgress: (d) => setLog((l) => [...l, d]),
        onDone: () => invalidate(),
        onError: (msg) => setLog((l) => [...l, { seq: l.length, ts: "", level: "error", message: msg }]),
      },
      ctrl.signal,
    );
    invalidate();
    setRefreshing((s) => {
      const n = new Set(s);
      n.delete(key);
      return n;
    });
  }

  return {
    refreshing,
    log,
    activeLabel,
    refreshScope: (scope: string, label: string) => run({ scope, mode: "scope", display_name: label }, scope, label),
    refreshDirectory: () => run({ mode: "directory" }, "directory", "Directory"),
    refreshAll: () => run({ mode: "all" }, "__all__", "All scopes"),
    isBusy: refreshing.size > 0,
  };
}

// ---- scope / workload filter rail -----------------------------------------------
type AccessFilter = {
  type: "scope" | "workload";
  label: string;
  scope_id?: string;
  subscription_ids?: string;
  workload_id?: string;
};

function ScopeTreeRow({
  node,
  depth,
  selectedId,
  expanded,
  toggle,
  onPick,
}: {
  node: RbacScopeNode;
  depth: number;
  selectedId: string;
  expanded: Set<string>;
  toggle: (id: string) => void;
  onPick: (node: RbacScopeNode) => void;
}) {
  const hasKids = node.children.length > 0;
  const isOpen = expanded.has(node.id);
  const selected = selectedId === node.id;
  const azKind = node.type === "managementGroup" ? "mg" : node.type === "subscription" ? "subscription" : "tenant";
  return (
    <div>
      <div
        className={`flex items-center gap-1 rounded px-1 py-1 text-sm ${selected ? "bg-brand/10 font-medium text-brand" : "text-gray-700 hover:bg-gray-100"}`}
        style={{ paddingLeft: depth * 12 + 4 }}
      >
        {hasKids ? (
          <button onClick={() => toggle(node.id)} className="w-4 shrink-0 text-gray-400" title={isOpen ? "Collapse" : "Expand"}>
            {isOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <button onClick={() => onPick(node)} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
          <AzureIcon kind={azKind} className="h-4 w-4" />
          <span className="truncate" title={node.name}>{node.name}</span>
          {node.inferred && node.type === "managementGroup" && (
            <span className="shrink-0 text-[10px] text-gray-400" title="Subscription nesting inferred (single management group)">~</span>
          )}
          <span className="ml-auto shrink-0 rounded bg-gray-100 px-1 text-[10px] tabular-nums text-gray-500">{node.count}</span>
        </button>
      </div>
      {hasKids && isOpen && node.children.map((c) => (
        <ScopeTreeRow key={c.id} node={c} depth={depth + 1} selectedId={selectedId} expanded={expanded} toggle={toggle} onPick={onPick} />
      ))}
    </div>
  );
}

function FilterRail({ filter, onChange }: { filter: AccessFilter | null; onChange: (f: AccessFilter | null) => void }) {
  const [mode, setMode] = useState<"scope" | "workload">("scope");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const connectionId = useRbacConnectionId();
  const treeQ = useQuery({ queryKey: ["rbac", "scope-tree", connectionId ?? ""], queryFn: () => api.rbacScopeTree(connectionId) });
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const root = treeQ.data?.root;
  const workloads = wlQ.data?.workloads ?? [];

  // Expand the root + management-group nodes once the tree loads so the hierarchy is visible.
  useEffect(() => {
    if (root) {
      setExpanded((s) => (s.size ? s : new Set([root.id, ...root.children.filter((c) => c.children.length).map((c) => c.id)])));
    }
  }, [root]);

  const toggle = (id: string) =>
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const selectedScopeId = filter?.type === "scope" ? (filter.scope_id ?? "") : "__none__";

  function pickScope(node: RbacScopeNode) {
    if (node.type === "root") {
      onChange(null);
      return;
    }
    onChange({
      type: "scope",
      label: node.name,
      scope_id: node.id,
      subscription_ids: node.subscriptionIds.join(","),
    });
  }

  return (
    <div className="flex w-64 shrink-0 flex-col border-r bg-gray-50">
      <div className="flex gap-1 border-b bg-white p-2">
        <button
          onClick={() => setMode("scope")}
          className={`flex-1 rounded px-2 py-1 text-xs font-medium ${mode === "scope" ? "bg-brand text-white" : "text-gray-600 hover:bg-gray-100"}`}
        >
          Azure scope
        </button>
        <button
          onClick={() => setMode("workload")}
          className={`flex-1 rounded px-2 py-1 text-xs font-medium ${mode === "workload" ? "bg-brand text-white" : "text-gray-600 hover:bg-gray-100"}`}
        >
          Workloads
        </button>
      </div>
      {filter && (
        <div className="flex items-center gap-1 border-b bg-amber-50 px-2 py-1 text-[11px] text-amber-800">
          <span className="truncate">
            Filtered: <b>{filter.label}</b>
          </span>
          <button onClick={() => onChange(null)} className="ml-auto shrink-0 rounded px-1 text-amber-700 hover:bg-amber-100">
            clear ✕
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-1">
        {mode === "scope" ? (
          treeQ.isLoading ? (
            <div className="p-3 text-xs text-gray-400">Loading…</div>
          ) : !root || root.children.length === 0 ? (
            <div className="p-3 text-xs text-gray-400">No scopes scanned yet. Run an access scan or seed demo data.</div>
          ) : (
            <ScopeTreeRow node={root} depth={0} selectedId={selectedScopeId} expanded={expanded} toggle={toggle} onPick={pickScope} />
          )
        ) : wlQ.isLoading ? (
          <div className="p-3 text-xs text-gray-400">Loading…</div>
        ) : workloads.length === 0 ? (
          <div className="p-3 text-xs text-gray-400">No workloads defined. Create one under Azure Workloads.</div>
        ) : (
          <div className="space-y-0.5">
            <button
              onClick={() => onChange(null)}
              className={`block w-full rounded px-2 py-1 text-left text-sm ${!filter ? "bg-brand/10 font-medium text-brand" : "text-gray-700 hover:bg-gray-100"}`}
            >
              🌐 All workloads
            </button>
            {workloads.map((w) => {
              const sel = filter?.type === "workload" && filter.workload_id === w.id;
              return (
                <button
                  key={w.id}
                  onClick={() => onChange({ type: "workload", label: w.name, workload_id: w.id })}
                  className={`block w-full truncate rounded px-2 py-1 text-left text-sm ${sel ? "bg-brand/10 font-medium text-brand" : "text-gray-700 hover:bg-gray-100"}`}
                  title={w.name}
                >
                  🧩 {w.name}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// ---- access grid (shared) -------------------------------------------------------
function AccessGrid({ tab }: { tab: string }) {
  const [search, setSearch] = useState("");
  const [surface, setSurface] = useState("");
  const [ptype, setPtype] = useState("");
  const [privOnly, setPrivOnly] = useState(false);
  const [filter, setFilter] = useState<AccessFilter | null>(null);
  const connectionId = useRbacConnectionId();
  const q = useQuery({
    queryKey: ["rbac", "access", tab, search, surface, ptype, privOnly, filter?.scope_id ?? "", filter?.workload_id ?? "", connectionId ?? ""],
    queryFn: () =>
      api.rbacAccess({
        tab,
        search,
        surface,
        principal_type: ptype,
        privileged_only: privOnly,
        limit: 500,
        scope_id: filter?.scope_id,
        subscription_ids: filter?.subscription_ids,
        workload_id: filter?.workload_id,
        connection_id: connectionId,
      }),
  });
  const rows = q.data?.rows ?? [];
  const total = q.data?.total ?? 0;
  const exportFilter = {
    scope_id: filter?.scope_id,
    subscription_ids: filter?.subscription_ids,
    workload_id: filter?.workload_id,
    connection_id: connectionId,
  };

  return (
    <div className="flex h-full min-h-0">
      <FilterRail filter={filter} onChange={setFilter} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search principal / role / scope…"
            className="w-64 rounded border px-2 py-1 text-sm"
          />
          <select value={surface} onChange={(e) => setSurface(e.target.value)} className="rounded border px-2 py-1 text-sm">
            <option value="">All surfaces</option>
            <option value="Azure RBAC">Azure RBAC</option>
            <option value="Entra ID RBAC">Entra ID RBAC</option>
            <option value="Key Vault Access Policy">Key Vault</option>
            <option value="Classic Admin">Classic Admin</option>
          </select>
          <select value={ptype} onChange={(e) => setPtype(e.target.value)} className="rounded border px-2 py-1 text-sm">
            <option value="">All principal types</option>
            <option value="User">User</option>
            <option value="Group">Group</option>
            <option value="ServicePrincipal">Service Principal</option>
          </select>
          <label className="flex items-center gap-1 text-sm text-gray-700">
            <input type="checkbox" checked={privOnly} onChange={(e) => setPrivOnly(e.target.checked)} /> Privileged only
          </label>
          <span className="ml-auto text-xs text-gray-500">{total.toLocaleString()} grant(s)</span>
          <a href={api.rbacExportUrl("csv", tab, exportFilter)} className="rounded border px-2 py-1 text-xs text-brand hover:bg-gray-50">⬇ CSV</a>
          <a href={api.rbacExportUrl("json", tab, exportFilter)} className="rounded border px-2 py-1 text-xs text-brand hover:bg-gray-50">⬇ JSON</a>
          <a href={api.rbacWorkbookUrl(exportFilter)} className="rounded border border-green-300 bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100">⬇ Excel (all tabs)</a>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {q.isLoading ? (
            <div className="p-6 text-sm text-gray-500">Loading…</div>
          ) : rows.length === 0 ? (
            <div className="p-6 text-sm text-gray-500">
              {filter ? `No access matches "${filter.label}". Try a broader scope or clear the filter.` : "No matching access. Run an access scan from the Overview tab."}
            </div>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead className="sticky top-0 bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2">Principal</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Role</th>
                  <th className="px-3 py-2">Scope</th>
                  <th className="px-3 py-2">Path</th>
                  <th className="px-3 py-2">Surface</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const who = (r.effectivePrincipalName || r.principalDisplayName || r.effectivePrincipalId || "—") as string;
                  const upn = (r.effectivePrincipalUserPrincipalName || r.principalUserPrincipalName || "") as string;
                  const scope = scopeCell(r);
                  const path = (r.accessPath as string) || "";
                  return (
                    <tr key={i} className="border-b last:border-0 hover:bg-gray-50">
                      <td className="px-3 py-1.5">
                        <div className="font-medium text-gray-800">{who}</div>
                        {upn && <div className="text-[11px] text-gray-400">{upn}</div>}
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">{(r.effectivePrincipalType || r.principalType || "") as string}</td>
                      <td className="px-3 py-1.5">
                        <span className="text-gray-800">{r.roleName as string}</span> <PrivBadge row={r} />
                      </td>
                      <td className="max-w-[280px] px-3 py-1.5 text-gray-600" title={r.scope as string}>
                        <div className="flex items-center gap-1.5">
                          {scope.icon && <AzureIcon kind={scope.icon} className="h-3.5 w-3.5 shrink-0" />}
                          <span className="min-w-0 truncate">{scope.label}</span>
                        </div>
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">
                        {PATH_LABEL[path] || path}
                        {path === "GroupTransitive" && r.sourceGroupName ? ` (${r.sourceGroupName})` : ""}
                      </td>
                      <td className="px-3 py-1.5 text-[11px] text-gray-500">{r.surface as string}</td>
                      <td className="px-3 py-1.5"></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

// ---- scope freshness table ------------------------------------------------------
function ScopeTable({
  scopes,
  refresh,
  refreshing,
}: {
  scopes: RbacScopeFreshness[];
  refresh: (scope: string, label: string) => void;
  refreshing: Set<string>;
}) {
  if (scopes.length === 0) return <div className="px-4 py-3 text-sm text-gray-500">No scopes scanned yet.</div>;
  return (
    <table className="w-full border-collapse text-sm">
      <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
        <tr>
          <th className="px-3 py-2">Scope</th>
          <th className="px-3 py-2">Type</th>
          <th className="px-3 py-2">Status</th>
          <th className="px-3 py-2">Grants</th>
          <th className="px-3 py-2">Freshness</th>
          <th className="px-3 py-2"></th>
        </tr>
      </thead>
      <tbody>
        {scopes.map((s) => (
          <tr key={s.scope} className="border-t hover:bg-gray-50">
            <td className="px-3 py-1.5 font-medium text-gray-800">
              {s.displayName}
              {s.demo && <span className="ml-1 rounded bg-violet-100 px-1 text-[10px] text-violet-700">demo</span>}
            </td>
            <td className="px-3 py-1.5 text-gray-500">{s.scopeType}</td>
            <td className="px-3 py-1.5">
              <StatusPill status={s.status} />
              {s.collectors_attention > 0 && <span className="ml-1 text-[11px] text-amber-600">⚠ {s.collectors_attention}</span>}
            </td>
            <td className="px-3 py-1.5 text-gray-600">{s.row_count}</td>
            <td className="px-3 py-1.5">
              <StaleBadge stale={s.stale} age={s.age_seconds} />
            </td>
            <td className="px-3 py-1.5">
              <button
                onClick={() => refresh(s.scope, s.displayName)}
                disabled={refreshing.has(s.scope)}
                className="rounded border px-2 py-0.5 text-xs text-brand hover:bg-gray-50 disabled:opacity-50"
              >
                {refreshing.has(s.scope) ? "Refreshing…" : "↻ Refresh"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---- pivots ---------------------------------------------------------------------
function PivotCard({ title, items }: { title: string; items: { label: string; count: number }[] }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 text-sm font-semibold text-gray-800">{title}</div>
      {items.length === 0 ? (
        <div className="text-xs text-gray-400">No data.</div>
      ) : (
        <div className="space-y-1">
          {items.slice(0, 8).map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-xs">
              <div className="w-40 truncate text-gray-600" title={it.label}>{it.label}</div>
              <div className="h-3 flex-1 rounded bg-gray-100">
                <div className="h-3 rounded bg-brand/70" style={{ width: `${(it.count / max) * 100}%` }} />
              </div>
              <div className="w-8 text-right tabular-nums text-gray-500">{it.count}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- tabs -----------------------------------------------------------------------
function OverviewTab({
  data,
  refreshCtl,
  onSeedDemo,
  seeding,
  onPurgeDemo,
  purging,
}: {
  data: RbacOverview;
  refreshCtl: ReturnType<typeof useRbacRefresh>;
  onSeedDemo: () => void;
  seeding: boolean;
  onPurgeDemo: () => void;
  purging: boolean;
}) {
  const k = data.kpis;
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mb-3 flex items-center gap-2">
        <button
          onClick={refreshCtl.refreshAll}
          disabled={refreshCtl.isBusy}
          className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-dark disabled:opacity-50"
        >
          {refreshCtl.refreshing.has("__all__") ? "Scanning…" : "↻ Refresh all scopes"}
        </button>
        <button
          onClick={refreshCtl.refreshDirectory}
          disabled={refreshCtl.isBusy}
          className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {refreshCtl.refreshing.has("directory") ? "Refreshing…" : "↻ Refresh directory"}
        </button>
        <button onClick={onSeedDemo} disabled={seeding} className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
          {seeding ? "Seeding…" : "🎬 Seed demo data"}
        </button>
        {data.demo && (
          <button onClick={onPurgeDemo} disabled={purging} className="rounded border border-red-200 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50">
            {purging ? "Removing…" : "🗑️ Remove demo data"}
          </button>
        )}
        {data.demo && <span className="rounded bg-violet-100 px-2 py-0.5 text-xs text-violet-700">demo dataset</span>}
        <a
          href={api.rbacWorkbookUrl()}
          className="rounded border border-green-300 bg-green-50 px-3 py-1.5 text-sm font-medium text-green-700 hover:bg-green-100"
          title="Download a comprehensive multi-sheet Excel workbook of every RBAC view"
        >
          ⬇ Export to Excel
        </a>
        <span className="ml-auto text-xs text-gray-500">
          {data.connection_configured ? "Azure connection configured" : "No Azure connection — use demo data"}
        </span>
      </div>

      <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <KpiTile label="Total grants" value={k.total_assignments} />
        <KpiTile label="Principals" value={k.unique_principals} tone="sky" />
        <KpiTile label="Privileged" value={k.privileged} tone="red" />
        <KpiTile label="Data-plane" value={k.data_plane} tone="amber" />
        <KpiTile label="Via groups" value={k.group_derived} tone="amber" />
        <KpiTile label="SP owners" value={k.owners} tone="amber" />
        <KpiTile label="Entra roles" value={k.entra_roles} />
        <KpiTile label="PIM eligible" value={k.eligible} />
        <KpiTile label="Scopes" value={k.scopes} />
        <KpiTile label="Subscriptions" value={k.subscriptions} />
      </div>

      {refreshCtl.log.length > 0 && (
        <div className="mb-4 rounded-lg border bg-gray-900 p-2 font-mono text-[11px] text-gray-100">
          <div className="mb-1 text-gray-400">Refreshing {refreshCtl.activeLabel}…</div>
          {refreshCtl.log.slice(-8).map((l, i) => (
            <div key={i} className={l.level === "error" ? "text-red-400" : l.level === "ok" ? "text-green-400" : l.level === "warning" ? "text-amber-300" : "text-gray-200"}>
              {l.message}
            </div>
          ))}
        </div>
      )}

      <div className="mb-4 rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Per-scope freshness</div>
        <ScopeTable scopes={data.scopes} refresh={refreshCtl.refreshScope} refreshing={refreshCtl.refreshing} />
      </div>

      <div className="rounded-lg border bg-white">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-semibold text-gray-800">Directory layer (Entra roles, groups, SP owners)</span>
          <div className="flex items-center gap-2">
            {data.directory.loaded ? <StatusPill status={data.directory.status} /> : <span className="text-xs text-gray-400">not loaded</span>}
            <StaleBadge age={data.directory.age_seconds} stale={(data.directory.age_seconds ?? Infinity) >= data.ttl_s} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 p-3 text-sm sm:grid-cols-4">
          <div><span className="text-gray-500">Rows: </span>{data.directory.row_count}</div>
          <div><span className="text-gray-500">Role defs: </span>{data.directory.role_def_count}</div>
          <div><span className="text-gray-500">Principals: </span>{data.directory.principal_count}</div>
          <div><span className="text-gray-500">Groups: </span>{data.directory.group_count}</div>
        </div>
      </div>
    </div>
  );
}

function ScopesTab({ refreshCtl }: { refreshCtl: ReturnType<typeof useRbacRefresh> }) {
  const connectionId = useRbacConnectionId();
  const q = useQuery({ queryKey: ["rbac", "scopes", connectionId ?? ""], queryFn: () => api.rbacScopes(connectionId) });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  const scopes = q.data?.scopes ?? [];
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Scopes ({scopes.length})</div>
        <ScopeTable scopes={scopes} refresh={refreshCtl.refreshScope} refreshing={refreshCtl.refreshing} />
      </div>
    </div>
  );
}

function RolesTab() {
  const connectionId = useRbacConnectionId();
  const q = useQuery({ queryKey: ["rbac", "roles", connectionId ?? ""], queryFn: () => api.rbacRoles(connectionId) });
  const [search, setSearch] = useState("");
  const roleDefs = (q.data?.role_defs ?? []) as Record<string, unknown>[];
  const principals = (q.data?.principals ?? []) as Record<string, unknown>[];
  const fr = roleDefs.filter((r) => !search || JSON.stringify(r).toLowerCase().includes(search.toLowerCase()));
  const fp = principals.filter((p) => !search || JSON.stringify(p).toLowerCase().includes(search.toLowerCase()));
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search roles / principals…" className="mb-3 w-72 rounded border px-2 py-1 text-sm" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Role definitions ({fr.length})</div>
          <div className="max-h-[60vh] overflow-auto">
            <table className="w-full text-sm">
              <tbody>
                {fr.map((r, i) => (
                  <tr key={i} className="border-b last:border-0">
                    <td className="px-3 py-1.5 font-medium text-gray-800">{String(r.roleName ?? "")}</td>
                    <td className="px-3 py-1.5 text-gray-500">{String(r.roleCategory ?? "")}</td>
                    <td className="px-3 py-1.5">{r.roleIsPrivileged ? <span className="rounded bg-red-100 px-1.5 text-[10px] text-red-700">privileged</span> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Principal directory ({fp.length})</div>
          <div className="max-h-[60vh] overflow-auto">
            <table className="w-full text-sm">
              <tbody>
                {fp.map((p, i) => (
                  <tr key={i} className="border-b last:border-0">
                    <td className="px-3 py-1.5 font-medium text-gray-800">{String(p.displayName ?? "")}</td>
                    <td className="px-3 py-1.5 text-gray-500">{String(p.principalType ?? "")}</td>
                    <td className="px-3 py-1.5 text-[11px] text-gray-400">{String(p.userPrincipalName ?? p.appId ?? "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

function InsightsTab() {
  const [filter, setFilter] = useState<AccessFilter | null>(null);
  const connectionId = useRbacConnectionId();
  const q = useQuery({
    queryKey: ["rbac", "pivots", filter?.scope_id ?? "", filter?.workload_id ?? "", connectionId ?? ""],
    queryFn: () =>
      api.rbacPivots({
        scope_id: filter?.scope_id,
        subscription_ids: filter?.subscription_ids,
        workload_id: filter?.workload_id,
        connection_id: connectionId,
      }),
  });
  const pivots = q.data?.pivots ?? {};
  const labels = q.data?.labels ?? {};
  const keys = Object.keys(labels);
  const exportFilter = {
    scope_id: filter?.scope_id,
    subscription_ids: filter?.subscription_ids,
    workload_id: filter?.workload_id,
    connection_id: connectionId,
  };
  return (
    <div className="flex h-full min-h-0">
      <FilterRail filter={filter} onChange={setFilter} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
          <span className="text-sm font-medium text-gray-700">Insights</span>
          {filter && <span className="text-xs text-gray-500">· filtered to <b>{filter.label}</b></span>}
          <a href={api.rbacWorkbookUrl(exportFilter)} className="ml-auto rounded border border-green-300 bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100">⬇ Excel (all tabs)</a>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {q.isLoading ? (
            <div className="text-sm text-gray-500">Loading…</div>
          ) : keys.length === 0 ? (
            <div className="text-sm text-gray-500">No insights yet. Run an access scan or seed demo data.</div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {keys.map((kk) => (
                <PivotCard key={kk} title={labels[kk]} items={pivots[kk] ?? []} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function DiagnosticsTab() {
  const connectionId = useRbacConnectionId();
  const q = useQuery({ queryKey: ["rbac", "diagnostics", connectionId ?? ""], queryFn: () => api.rbacDiagnostics(connectionId) });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  const collectors = q.data?.collectors ?? [];
  const errors = q.data?.errors ?? [];
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mb-4 rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Collector status ({collectors.length})</div>
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Collector</th>
              <th className="px-3 py-2">Scope</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Rows</th>
              <th className="px-3 py-2">Message</th>
            </tr>
          </thead>
          <tbody>
            {collectors.map((c, i) => (
              <tr key={i} className="border-t">
                <td className="px-3 py-1.5 font-medium text-gray-800">{c.collector}</td>
                <td className="px-3 py-1.5 text-gray-500">{c.scopeLabel}</td>
                <td className="px-3 py-1.5"><StatusPill status={c.status} /></td>
                <td className="px-3 py-1.5 text-gray-600">{c.rowsAdded}</td>
                <td className="px-3 py-1.5 text-[11px] text-gray-500">{c.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {errors.length > 0 && (
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Errors & warnings ({errors.length})</div>
          <table className="w-full text-sm">
            <tbody>
              {errors.map((e, i) => (
                <tr key={i} className="border-t">
                  <td className="px-3 py-1.5 text-gray-700">{e.collector}</td>
                  <td className="px-3 py-1.5"><StatusPill status={e.status} /></td>
                  <td className="px-3 py-1.5 text-[11px] text-gray-500">{e.errorMessage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---- panel ----------------------------------------------------------------------
export function RbacPanel({ tab = "overview" }: { tab?: RbacTab }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [connectionId, setConnectionId] = usePersistedState("azsup.rbac.connectionId", "");
  const [seeding, setSeeding] = useState(false);
  const [purging, setPurging] = useState(false);
  const [err, setErr] = useState("");

  return (
    <RbacConnectionContext.Provider value={connectionId}>
      <RbacPanelBody
        tab={tab}
        navigate={navigate}
        qc={qc}
        connectionId={connectionId}
        setConnectionId={setConnectionId}
        seeding={seeding}
        setSeeding={setSeeding}
        purging={purging}
        setPurging={setPurging}
        err={err}
        setErr={setErr}
      />
    </RbacConnectionContext.Provider>
  );
}

function RbacPanelBody({
  tab,
  navigate,
  qc,
  connectionId,
  setConnectionId,
  seeding,
  setSeeding,
  purging,
  setPurging,
  err,
  setErr,
}: {
  tab: RbacTab;
  navigate: ReturnType<typeof useNavigate>;
  qc: ReturnType<typeof useQueryClient>;
  connectionId: string;
  setConnectionId: (v: string) => void;
  seeding: boolean;
  setSeeding: (v: boolean) => void;
  purging: boolean;
  setPurging: (v: boolean) => void;
  err: string;
  setErr: (v: string) => void;
}) {
  const refreshCtl = useRbacRefresh();

  const overviewQ = useQuery({ queryKey: ["rbac", "overview", connectionId], queryFn: () => api.rbacOverview(connectionId) });

  // Reconnect to any in-flight refresh job on mount (the job survives navigation).
  useEffect(() => {
    let cancelled = false;
    api.rbacJob({ mode: "all", connection_id: connectionId || null }).then((r) => {
      if (!cancelled && r.job?.status === "running") {
        refreshCtl.refreshAll();
      }
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  const setTab = (v: RbacTab) => navigate(v === "overview" ? "/rbac" : `/rbac/${v}`);

  async function seedDemo() {
    setSeeding(true);
    setErr("");
    try {
      await api.rbacDemoSeed();
      for (const k of ["overview", "scopes", "access", "pivots", "roles", "diagnostics", "runs"]) {
        qc.invalidateQueries({ queryKey: ["rbac", k] });
      }
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setSeeding(false);
    }
  }

  async function purgeDemo() {
    if (!window.confirm("Remove the RBAC demo dataset? This only clears the synthetic demo data; re-seed any time.")) return;
    setPurging(true);
    setErr("");
    try {
      await api.rbacDemoPurge();
      for (const k of ["overview", "scopes", "access", "pivots", "roles", "diagnostics", "runs", "scope-tree"]) {
        qc.invalidateQueries({ queryKey: ["rbac", k] });
      }
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setPurging(false);
    }
  }

  const data = overviewQ.data;

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      {/* Header + tab bar */}
      <div className="border-b bg-white px-4 pt-3">
        <div className="mb-2 flex items-center gap-2">
          <h1 className="text-lg font-semibold text-gray-900">RBAC — Access Review</h1>
          <span className="text-xs text-gray-500">Who can access what across Azure RBAC, Entra roles, groups & ownership</span>
          <div className="ml-auto">
            <ConnectionScopePicker value={connectionId} onChange={setConnectionId} />
          </div>
        </div>
        <div className="flex items-center gap-1">
          {RBAC_NAV.map(({ id, label }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`rounded-t-lg px-3 py-1.5 text-sm font-medium ${
                tab === id ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="border-b bg-red-50 px-4 py-2 text-sm text-red-700">{err}</div>}

      {overviewQ.isLoading ? (
        <div className="p-6 text-sm text-gray-500">Loading…</div>
      ) : data && data.never_loaded && tab === "overview" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          <div className="text-4xl">🛡️</div>
          <div className="text-lg font-semibold text-gray-800">No access scan loaded yet</div>
          <p className="max-w-md text-sm text-gray-500">
            Run an access scan to inventory who can access what, or load the demo dataset to explore the
            review without an Azure connection.
          </p>
          <div className="flex gap-2">
            <button onClick={refreshCtl.refreshAll} disabled={refreshCtl.isBusy} className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-dark disabled:opacity-50">
              {refreshCtl.isBusy ? "Scanning…" : "↻ Run access scan"}
            </button>
            <button onClick={seedDemo} disabled={seeding} className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
              {seeding ? "Seeding…" : "🎬 Seed demo data"}
            </button>
          </div>
          {refreshCtl.log.length > 0 && (
            <div className="mt-2 w-full max-w-lg rounded bg-gray-900 p-2 text-left font-mono text-[11px] text-gray-100">
              {refreshCtl.log.slice(-6).map((l, i) => (
                <div key={i}>{l.message}</div>
              ))}
            </div>
          )}
        </div>
      ) : !data ? (
        <div className="p-6 text-sm text-gray-500">No data.</div>
      ) : tab === "overview" ? (
        <OverviewTab data={data} refreshCtl={refreshCtl} onSeedDemo={seedDemo} seeding={seeding} onPurgeDemo={purgeDemo} purging={purging} />
      ) : tab === "effective" ? (
        <AccessGrid tab="effective" />
      ) : tab === "privileged" ? (
        <AccessGrid tab="privileged" />
      ) : tab === "scopes" ? (
        <ScopesTab refreshCtl={refreshCtl} />
      ) : tab === "roles" ? (
        <RolesTab />
      ) : tab === "insights" ? (
        <InsightsTab />
      ) : (
        <DiagnosticsTab />
      )}
    </div>
  );
}
