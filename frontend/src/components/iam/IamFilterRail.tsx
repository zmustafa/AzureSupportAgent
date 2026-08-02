/** Scope / workload filter rail — the left sidebar shared by the access grid and Insights.
 *
 *  Two modes: the Azure scope hierarchy (management group -> subscription, with per-node grant
 *  counts) and the flat workload list. Picking the tree root clears the filter.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type IamScopeNode } from "../../api";
import { AzureIcon } from "../AzureIcon";
import { type AccessFilter, useIamConnectionId } from "./IamShared";

function ScopeTreeRow({
  node,
  depth,
  selectedId,
  expanded,
  toggle,
  onPick,
}: {
  node: IamScopeNode;
  depth: number;
  selectedId: string;
  expanded: Set<string>;
  toggle: (id: string) => void;
  onPick: (node: IamScopeNode) => void;
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

export function FilterRail({ filter, onChange }: { filter: AccessFilter | null; onChange: (f: AccessFilter | null) => void }) {
  const [mode, setMode] = useState<"scope" | "workload">("scope");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const connectionId = useIamConnectionId();
  const treeQ = useQuery({ queryKey: ["iam", "scope-tree", connectionId ?? ""], queryFn: () => api.iamScopeTree(connectionId), staleTime: 5 * 60 * 1000 });
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

  function pickScope(node: IamScopeNode) {
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
