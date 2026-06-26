// Power-BI-style expandable drill grid for the Tag census. A tag-key row expands to its values,
// each value to the subscriptions carrying it, each subscription to its resource types, and each
// type to the leaf resources. Every level is lazily fetched from the cached census (no Azure
// call) on first expand and then React-Query-cached. Option B hierarchy:
//
//   key → value → subscription → resource type → resource (leaf)
//
// P2 niceties: "use as filter" on any node (prefills the Ask console via onUseFilter), CSV export
// of an expanded value's resource subtree, a casing-fold toggle, and reconciling counts shown at
// every level.
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type TagScopeSel, type TagCensusKey, type TagDrillRow } from "../../api";
import { InlineSearch, useDebounced } from "../../utils/perf";

const CAT_COLORS: Record<string, string> = {
  billing: "#2563eb", ownership: "#7c3aed", environment: "#0891b2", application: "#16a34a",
  organization: "#d97706", security: "#dc2626", lifecycle: "#db2777", operations: "#0d9488", other: "#64748b",
};

function shortType(t: string): string {
  // microsoft.storage/storageaccounts → storageaccounts (provider/Type → Type)
  const seg = (t || "").split("/").slice(1).join("/");
  return seg || t || "—";
}

type DrillNode =
  | { level: "value"; key: string; value: string }
  | { level: "subscription"; key: string; value: string; subscription_id: string; name: string }
  | { level: "type"; key: string; value: string; subscription_id: string; type: string };

function nodePath(n: DrillNode): string {
  if (n.level === "value") return `${n.key}\u0000${n.value}`;
  if (n.level === "subscription") return `${n.key}\u0000${n.value}\u0000${n.subscription_id}`;
  return `${n.key}\u0000${n.value}\u0000${n.subscription_id}\u0000${n.type}`;
}

// A lazily-loaded, expandable sub-tree for one node. Fetches its children on first mount.
function DrillChildren({
  sel, node, foldCasing, depth, expanded, toggle, onUseFilter,
}: {
  sel: TagScopeSel;
  node: DrillNode;
  foldCasing: boolean;
  depth: number;
  expanded: Set<string>;
  toggle: (p: string) => void;
  onUseFilter?: (text: string) => void;
}) {
  // The query params for THIS node's children depend on its level.
  const q =
    node.level === "value" ? { key: node.key, value: node.value, fold_casing: foldCasing }
    : node.level === "subscription" ? { key: node.key, value: node.value, subscription_id: node.subscription_id, fold_casing: foldCasing }
    : { key: node.key, value: node.value, subscription_id: node.subscription_id, resource_type: node.type, fold_casing: foldCasing };

  const dq = useQuery({
    queryKey: ["tagintel", "drill", sel.connection_id ?? "", sel.scope ?? "", sel.workload_id ?? "", nodePath(node), foldCasing],
    queryFn: () => api.tagintelCensusDrill(sel, q),
  });

  const pad = { paddingLeft: `${depth * 18 + 28}px` };
  if (dq.isLoading) return <tr><td colSpan={4} className="px-4 py-1.5 text-[11px] text-gray-400" style={pad}>Loading…</td></tr>;
  const rows = dq.data?.rows ?? [];
  if (rows.length === 0) return <tr><td colSpan={4} className="px-4 py-1.5 text-[11px] text-gray-300" style={pad}>No items.</td></tr>;

  return (
    <>
      {rows.map((r) => (
        <DrillRow key={drillRowKey(node, r)} sel={sel} parent={node} row={r} foldCasing={foldCasing}
          depth={depth} expanded={expanded} toggle={toggle} onUseFilter={onUseFilter} />
      ))}
      {dq.data?.truncated && (
        <tr><td colSpan={4} className="px-4 py-1 text-[11px] italic text-amber-600" style={pad}>+{(dq.data.total ?? 0) - rows.length} more — narrow the scope or use the Ask console.</td></tr>
      )}
    </>
  );
}

function drillRowKey(parent: DrillNode, r: TagDrillRow): string {
  if (parent.level === "value") return `sub:${r.subscription_id}`;
  if (parent.level === "subscription") return `type:${r.type}`;
  return `res:${r.id}`;
}

// One row at any drill level below the key. Leaf rows (resources) don't expand.
function DrillRow({
  sel, parent, row, foldCasing, depth, expanded, toggle, onUseFilter,
}: {
  sel: TagScopeSel;
  parent: DrillNode;
  row: TagDrillRow;
  foldCasing: boolean;
  depth: number;
  expanded: Set<string>;
  toggle: (p: string) => void;
  onUseFilter?: (text: string) => void;
}) {
  // Determine THIS row's node (one level below parent) + its label/count.
  let node: DrillNode | null = null;
  let label = "";
  let filterText = "";
  if (parent.level === "value") {
    node = { level: "subscription", key: parent.key, value: parent.value, subscription_id: row.subscription_id || "", name: row.name || "(unknown)" };
    label = row.name || row.subscription_id || "(unknown)";
    filterText = `resources where ${parent.key}=${parent.value} in subscription ${label}`;
  } else if (parent.level === "subscription") {
    node = { level: "type", key: parent.key, value: parent.value, subscription_id: parent.subscription_id, type: row.type || "" };
    label = shortType(row.type || "");
    filterText = `${shortType(row.type || "")} resources where ${parent.key}=${parent.value}`;
  } else {
    // leaf resource — no further expansion
    label = row.name || (row.id || "").split("/").pop() || "—";
  }

  const isLeaf = parent.level === "type";
  const path = node ? nodePath(node) : "";
  const open = !!path && expanded.has(path);
  const pad = { paddingLeft: `${depth * 18 + 28}px` };

  return (
    <>
      <tr className={`border-t ${isLeaf ? "" : "cursor-pointer hover:bg-gray-50"}`} onClick={() => !isLeaf && path && toggle(path)}>
        <td className="py-1.5 pr-2">
          <div className="flex items-center" style={pad}>
            {!isLeaf && <span className="mr-1 inline-block w-3 text-gray-400">{open ? "▾" : "▸"}</span>}
            {isLeaf && <span className="mr-1 inline-block w-3 text-gray-300">•</span>}
            <span className={`truncate ${isLeaf ? "text-gray-600" : "font-medium text-gray-800"}`} title={parent.level === "type" ? row.id : label}>{label}</span>
            {parent.level === "value" && row.distinct_types !== undefined && <span className="ml-2 text-[10px] text-gray-400">{row.distinct_types} type(s)</span>}
            {parent.level === "subscription" && row.count !== undefined && <span className="ml-2 text-[10px] text-gray-400">{row.count} res</span>}
          </div>
        </td>
        <td className="px-2 text-[11px] text-gray-400">{parent.level === "type" ? row.resource_group : ""}</td>
        <td className="px-2 text-right tabular-nums text-gray-600">{row.count ?? ""}</td>
        <td className="px-2 text-right">
          {onUseFilter && filterText && (
            <button onClick={(e) => { e.stopPropagation(); onUseFilter(filterText); }} className="rounded border px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-brand" title="Ask about this subset">⌕</button>
          )}
        </td>
      </tr>
      {open && node && (
        <DrillChildren sel={sel} node={node} foldCasing={foldCasing} depth={depth + 1} expanded={expanded} toggle={toggle} onUseFilter={onUseFilter} />
      )}
    </>
  );
}

export function TagKeyDrillGrid({
  keys, sel, onUseFilter, initialFilter = "", onFilterChange,
}: {
  keys: TagCensusKey[];
  sel: TagScopeSel;
  onUseFilter?: (text: string) => void;
  initialFilter?: string;
  onFilterChange?: (value: string) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [foldCasing, setFoldCasing] = useState(true);
  const [q, setQ] = useState(initialFilter);
  const dq = useDebounced(q, 150);
  // Keep the box in sync if the deep-link param changes (e.g. user follows another ?key= link).
  useEffect(() => { setQ(initialFilter); }, [initialFilter]);
  // Reflect the active filter back into the URL so the view is shareable/deep-linkable. Ref the
  // callback so an inline parent function doesn't retrigger this effect every render.
  const onFilterChangeRef = useRef(onFilterChange);
  onFilterChangeRef.current = onFilterChange;
  useEffect(() => { onFilterChangeRef.current?.(dq.trim()); }, [dq]);
  const toggle = (p: string) => setExpanded((s) => { const n = new Set(s); n.has(p) ? n.delete(p) : n.add(p); return n; });

  // TU5 — filter the (potentially long) tag-key list by name/category.
  const shownKeys = useMemo(() => {
    const needle = dq.trim().toLowerCase();
    if (!needle) return keys;
    return keys.filter((k) => k.key.toLowerCase().includes(needle) || (k.category || "").toLowerCase().includes(needle) || k.casing_variants.some((v) => v.toLowerCase().includes(needle)));
  }, [keys, dq]);

  // Each top-level KEY row expands into a synthetic "value-level" node whose children are the
  // distinct values of that key (drilled lazily).
  return (
    <div className="rounded-xl border bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
        <span className="text-sm font-medium text-gray-700">Tag keys ({keys.length})</span>
        <div className="flex items-center gap-3">
          <InlineSearch q={q} setQ={setQ} shown={shownKeys.length} total={keys.length} placeholder="Filter keys…" width="w-44" />
          <label className="flex items-center gap-1 text-[11px] text-gray-500" title="Roll casing/spelling variants of a key together (Environment + environment)">
            <input type="checkbox" checked={foldCasing} onChange={(e) => { setFoldCasing(e.target.checked); setExpanded(new Set()); }} /> fold casing
          </label>
          {expanded.size > 0 && <button onClick={() => setExpanded(new Set())} className="text-[11px] text-gray-400 hover:text-brand">collapse all</button>}
        </div>
      </div>
      <div className="max-h-[460px] overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 z-10 bg-gray-50 text-left text-[11px] uppercase text-gray-400">
            <tr><th className="px-4 py-2">Key / value / sub / type / resource</th><th className="px-2">Detail</th><th className="px-2 text-right">Count</th><th className="px-2" /></tr>
          </thead>
          <tbody>
            {shownKeys.map((k) => {
              const path = `key\u0000${k.key}`;
              const open = expanded.has(path);
              return (
                <KeyRow key={k.key} k={k} sel={sel} foldCasing={foldCasing} open={open}
                  onToggle={() => toggle(path)} expanded={expanded} toggle={toggle} onUseFilter={onUseFilter} />
              );
            })}
            {shownKeys.length === 0 && <tr><td colSpan={4} className="px-4 py-4 text-center text-[11px] text-gray-400">No keys match “{dq}”.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// The top-level tag-KEY row. Expands to its distinct values (the "value" drill level on the key).
function KeyRow({
  k, sel, foldCasing, open, onToggle, expanded, toggle, onUseFilter,
}: {
  k: TagCensusKey;
  sel: TagScopeSel;
  foldCasing: boolean;
  open: boolean;
  onToggle: () => void;
  expanded: Set<string>;
  toggle: (p: string) => void;
  onUseFilter?: (text: string) => void;
}) {
  // Children of a KEY = its values. We query with key only (value undefined) → level "value".
  const dq = useQuery({
    queryKey: ["tagintel", "drill", sel.connection_id ?? "", sel.scope ?? "", sel.workload_id ?? "", `key\u0000${k.key}`, foldCasing],
    queryFn: () => api.tagintelCensusDrill(sel, { key: k.key, fold_casing: foldCasing }),
    enabled: open,
  });
  const valueRows = dq.data?.rows ?? [];

  return (
    <>
      <tr className="cursor-pointer border-t hover:bg-gray-50" onClick={onToggle}>
        <td className="py-1.5 pr-2">
          <div className="flex items-center pl-2">
            <span className="mr-1 inline-block w-3 text-gray-400">{open ? "▾" : "▸"}</span>
            <span className="font-medium text-gray-800">{k.key}</span>
            {k.casing_variants.length > 0 && <span className="ml-1 rounded bg-red-100 px-1 text-[10px] text-red-600" title={`Casing variants: ${k.casing_variants.join(", ")}`}>±{k.casing_variants.length}</span>}
            {k.high_cardinality && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-700">high-card</span>}
          </div>
        </td>
        <td className="px-2"><span className="rounded px-1.5 py-0.5 text-[10px] font-medium text-white" style={{ background: CAT_COLORS[k.category] }}>{k.category}</span></td>
        <td className="px-2 text-right tabular-nums text-gray-700">{k.count.toLocaleString()}</td>
        <td className="px-2 text-right">
          <div className="flex items-center justify-end gap-1">
            <span className="text-[11px] text-gray-400" title={`${k.distinct_values} distinct value(s)`}>{k.distinct_values}v</span>
            <span className="inline-flex items-center gap-1 text-[11px] text-gray-500"><span className="h-1.5 w-12 overflow-hidden rounded-full bg-gray-100"><span className="block h-full rounded-full bg-brand" style={{ width: `${k.coverage_pct}%` }} /></span>{k.coverage_pct}%</span>
          </div>
        </td>
      </tr>
      {open && (dq.isLoading ? (
        <tr><td colSpan={4} className="px-4 py-1.5 pl-10 text-[11px] text-gray-400">Loading values…</td></tr>
      ) : valueRows.length === 0 ? (
        <tr><td colSpan={4} className="px-4 py-1.5 pl-10 text-[11px] text-gray-300">No values.</td></tr>
      ) : (
        <>
          {valueRows.map((vr) => {
            const valNode: DrillNode = { level: "value", key: k.key, value: vr.value || "" };
            const vpath = nodePath(valNode);
            const vopen = expanded.has(vpath);
            return (
              <ValueRow key={`val:${vr.value}`} sel={sel} node={valNode} row={vr} foldCasing={foldCasing}
                open={vopen} toggle={toggle} expanded={expanded} onUseFilter={onUseFilter} />
            );
          })}
          {dq.data?.truncated && <tr><td colSpan={4} className="px-4 py-1 pl-10 text-[11px] italic text-amber-600">+{(dq.data.total ?? 0) - valueRows.length} more values.</td></tr>}
        </>
      ))}
    </>
  );
}

// A value row under a key. Expands to subscriptions (the "subscription" drill level).
function ValueRow({
  sel, node, row, foldCasing, open, toggle, expanded, onUseFilter,
}: {
  sel: TagScopeSel;
  node: Extract<DrillNode, { level: "value" }>;
  row: TagDrillRow;
  foldCasing: boolean;
  open: boolean;
  toggle: (p: string) => void;
  expanded: Set<string>;
  onUseFilter?: (text: string) => void;
}) {
  const path = nodePath(node);
  return (
    <>
      <tr className="cursor-pointer border-t bg-gray-50/30 hover:bg-gray-50" onClick={() => toggle(path)}>
        <td className="py-1.5 pr-2">
          <div className="flex items-center" style={{ paddingLeft: "28px" }}>
            <span className="mr-1 inline-block w-3 text-gray-400">{open ? "▾" : "▸"}</span>
            <span className="truncate font-medium text-gray-700" title={node.value}>{node.value || "(empty)"}</span>
            {row.subscription_count !== undefined && <span className="ml-2 text-[10px] text-gray-400">{row.subscription_count} sub(s) · {row.distinct_types} type(s)</span>}
          </div>
        </td>
        <td className="px-2" />
        <td className="px-2 text-right tabular-nums text-gray-600">{row.count ?? ""}</td>
        <td className="px-2 text-right">
          {onUseFilter && <button onClick={(e) => { e.stopPropagation(); onUseFilter(`resources where ${node.key}=${node.value}`); }} className="rounded border px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-brand" title="Ask about this value">⌕</button>}
        </td>
      </tr>
      {open && (
        <DrillChildren sel={sel} node={node} foldCasing={foldCasing} depth={2} expanded={expanded} toggle={toggle} onUseFilter={onUseFilter} />
      )}
    </>
  );
}
