import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type TreeNode } from "../api";
import { formatError } from "../utils/format";

// One row of the subscription picker's management-group → subscription tree. MGs expand
// (lazily); subscriptions are the selectable leaves.
function ScopeNodeRow({
  node,
  depth,
  selectedId,
  expanded,
  loading,
  childrenMap,
  onToggle,
  onPick,
}: {
  node: TreeNode;
  depth: number;
  selectedId: string;
  expanded: Set<string>;
  loading: Set<string>;
  childrenMap: Map<string, TreeNode[]>;
  onToggle: (n: TreeNode) => void;
  onPick: (n: TreeNode) => void;
}) {
  const isMg = node.kind === "mg";
  const isOpen = expanded.has(node.id);
  const isLoading = loading.has(node.id);
  const kids = childrenMap.get(node.id) ?? [];
  const selected = node.kind === "subscription" && selectedId === node.id;
  return (
    <div>
      <div
        className={`flex items-center gap-1 rounded px-1 py-1 text-xs ${selected ? "bg-brand/10 font-medium text-brand" : "text-gray-700 hover:bg-gray-100"}`}
        style={{ paddingLeft: depth * 14 + 4 }}
      >
        {isMg ? (
          <button onClick={() => onToggle(node)} className="w-4 shrink-0 text-gray-400" title={isOpen ? "Collapse" : "Expand"}>
            {isLoading ? "…" : isOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <button
          onClick={() => (isMg ? onToggle(node) : onPick(node))}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          title={node.kind === "subscription" ? node.id : node.name}
        >
          <span className="shrink-0">{isMg ? "🗂️" : "📦"}</span>
          <span className="truncate">{node.name}</span>
        </button>
      </div>
      {isMg && isOpen && kids.map((c) => (
        <ScopeNodeRow
          key={c.id}
          node={c}
          depth={depth + 1}
          selectedId={selectedId}
          expanded={expanded}
          loading={loading}
          childrenMap={childrenMap}
          onToggle={onToggle}
          onPick={onPick}
        />
      ))}
      {isMg && isOpen && !isLoading && kids.length === 0 && (
        <div className="py-1 text-[11px] text-gray-400" style={{ paddingLeft: (depth + 1) * 14 + 8 }}>
          (no subscriptions)
        </div>
      )}
    </div>
  );
}

// Subscription scope selector: a dropdown showing the live management-group + subscription
// tree so operators pick a subscription instead of pasting its GUID. Uses the DEFAULT Azure
// connection (the same one the coverage/profiler scans run with) and lazily expands MGs on
// demand. Shared by Monitoring/Telemetry/Backup-DR coverage and the Performance Profiler.
export function SubscriptionScopePicker({
  value,
  valueName,
  onPick,
}: {
  value: string;
  valueName: string;
  onPick: (id: string, name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [childrenMap, setChildrenMap] = useState<Map<string, TreeNode[]>>(new Map());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections });
  const connections = connQ.data?.connections ?? [];
  const connectionId = connections.find((c) => c.is_default)?.id || connections[0]?.id || "";

  // Top of the tree (root management groups, or a flat subscription list when no MGs exist).
  // Only fetched once the dropdown is opened, to avoid a Resource Graph call on every render.
  const topQ = useQuery({
    queryKey: ["scope-tree", connectionId],
    queryFn: () => api.workloadTree({ connection_id: connectionId, group_by: "mg" }),
    enabled: open && !!connectionId,
  });
  const topNodes = topQ.data?.nodes ?? [];

  // Close the popover on an outside click.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  async function toggle(node: TreeNode) {
    const next = new Set(expanded);
    if (next.has(node.id)) {
      next.delete(node.id);
      setExpanded(next);
      return;
    }
    next.add(node.id);
    setExpanded(next);
    if (!childrenMap.has(node.id)) {
      setLoading((l) => new Set(l).add(node.id));
      try {
        const r = await api.workloadTree({ connection_id: connectionId, kind: node.kind, node_id: node.id });
        const kids = r.nodes.filter((n) => n.kind === "mg" || n.kind === "subscription");
        setChildrenMap((c) => new Map(c).set(node.id, kids));
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

  function pick(node: TreeNode) {
    onPick(node.id, node.name);
    setOpen(false);
  }

  return (
    <div ref={boxRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-64 items-center gap-1 rounded-lg border px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
        title={value ? `${valueName || ""} ${value}`.trim() : "Pick a subscription from the management-group tree"}
      >
        <span className="shrink-0">📦</span>
        <span className="truncate">{value ? valueName || value : "Select subscription…"}</span>
        <span className="ml-auto shrink-0 text-gray-400">▾</span>
      </button>
      {open && (
        <div className="absolute right-0 z-40 mt-1 max-h-80 w-80 overflow-auto rounded-lg border bg-white p-1 shadow-lg">
          {!connectionId ? (
            <div className="p-3 text-xs text-gray-400">
              No Azure connection configured. Add one under Settings → Connections.
            </div>
          ) : topQ.isLoading ? (
            <div className="p-3 text-xs text-gray-400">Loading subscriptions…</div>
          ) : topQ.isError ? (
            <div className="p-3 text-xs text-red-600">{formatError(topQ.error)}</div>
          ) : error ? (
            <div className="p-3 text-xs text-red-600">{error}</div>
          ) : topNodes.length === 0 ? (
            <div className="p-3 text-xs text-gray-400">No subscriptions found for this connection.</div>
          ) : (
            topNodes.map((n) => (
              <ScopeNodeRow
                key={n.id}
                node={n}
                depth={0}
                selectedId={value}
                expanded={expanded}
                loading={loading}
                childrenMap={childrenMap}
                onToggle={toggle}
                onPick={pick}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}
