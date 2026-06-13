import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type Workload, type WorkloadNode, type WorkloadNodeKind } from "../api";
import { formatError } from "../utils/format";
import { ResourcePicker } from "./ResourcePicker";
import { AutopilotModal, TypeChips } from "./AutopilotModal";
import { AzureIcon, friendlyResourceType } from "./AzureIcon";

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


export function WorkloadsPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const [editing, setEditing] = useState<Partial<Workload> | null>(null);
  const [autopilot, setAutopilot] = useState(false);
  const [refreshing, setRefreshing] = useState<string>("");
  const [msg, setMsg] = useState("");
  const [notice, setNotice] = useState("");
  const [showTrash, setShowTrash] = useState(false);

  const workloads = wlQ.data?.workloads ?? [];

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

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
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
        {wlQ.isLoading && <div className="text-sm text-gray-500">Loading…</div>}

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {workloads.map((w) => (
            <div key={w.id} className="rounded-xl border bg-white p-4 shadow-sm">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-semibold text-gray-800">{w.name}</span>
                    {w.origin?.kind && (
                      <span className="shrink-0 rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">
                        autopilot
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-xs text-gray-500">{w.description}</p>
                </div>
              </div>
              {w.summary && (w.summary.types?.length ?? 0) > 0 ? (
                <div className="mt-2"><TypeChips types={w.summary.types} max={6} /></div>
              ) : (
                <div className="mt-2 text-[11px] text-gray-500">{countByKind(w.nodes) || "No resources yet"}</div>
              )}
              <div className="mt-3 flex items-center gap-2">
                <button
                  onClick={() => void refresh(w.id)}
                  disabled={refreshing === w.id}
                  title="Re-scan this workload's scope for added/removed resources"
                  className="flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60"
                >
                  <span className={refreshing === w.id ? "inline-block animate-spin" : ""}>↻</span>
                  {refreshing === w.id ? "Refreshing…" : "Refresh"}
                </button>
                <button
                  onClick={() => setEditing(w)}
                  className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50"
                >
                  Edit
                </button>
                <a
                  href={`/assessments`}
                  onClick={(e) => {
                    e.preventDefault();
                    sessionStorage.setItem("azsup.assessWorkload", w.id);
                    navigate("/assessments");
                  }}
                  title="Run a Well-Architected assessment against this workload"
                  className="rounded-lg border border-brand/40 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10"
                >
                  ✓ Run assessment
                </a>
                <button
                  onClick={() => void remove(w.id)}
                  className="ml-auto rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50"
                >
                  Delete
                </button>
              </div>
              {w.last_refreshed && (
                <div className="mt-1.5 text-[10px] text-gray-400">
                  Last refreshed {new Date(w.last_refreshed).toLocaleString()}
                </div>
              )}
            </div>
          ))}
        </div>

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

function WorkloadForm({
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
