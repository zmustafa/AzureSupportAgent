import { useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AzureIcon } from "./AzureIcon";
import { api } from "../api";
import type { PolicyInventory, PolicyAssignment } from "../api";

// ============================================================================ shared
// Governance/audit views over policy ASSIGNMENTS, reproducing (and surpassing) the HQY team's
// Excel pivot workbook: a flat register + pivot perspectives (by person, by subscription, a date
// timeline with a slicer, and a configurable pivot builder). All client-side over inv.assignments.

const BLANK = "(blank)";

// PU5 — small removable active-filter chip.
function FilterChip({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <span className="flex items-center gap-1 rounded-md bg-brand/10 px-2 py-0.5 text-[11px] text-brand">
      {label}
      <button onClick={onClear} className="text-brand/60 hover:text-brand">✕</button>
    </span>
  );
}

// ----- dimensions ----------------------------------------------------------------
export type PivotDim = "assigned_by" | "management_group" | "subscription" | "policy" | "dt";
type DtGranularity = "day" | "month" | "year";

const DIM_LABEL: Record<PivotDim, string> = {
  assigned_by: "Assigned by",
  management_group: "Management group",
  subscription: "Subscription",
  policy: "Policy / Initiative",
  dt: "Created (date)",
};

function pad(n: number): string { return n < 10 ? `0${n}` : `${n}`; }

// A dimension value: a human label + a sortable key (so dates sort chronologically and
// "(blank)" always sorts last, regardless of its display text).
function dimValue(a: PolicyAssignment, dim: PivotDim, gran: DtGranularity): { label: string; sort: string } {
  switch (dim) {
    case "assigned_by": {
      const v = (a.assigned_by || "").trim();
      return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" };
    }
    case "management_group": {
      const v = (a.management_group_display || a.management_group_name || "").trim();
      return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" };
    }
    case "subscription": {
      const v = (a.subscription_name || "").trim() || (a.subscription_id ? `${a.subscription_id.slice(0, 8)}…` : "");
      return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" };
    }
    case "policy": {
      const v = (a.display_name || a.definition_name || "").trim();
      return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" };
    }
    case "dt": {
      const d = parseDate(a.created_on);
      if (!d) return { label: BLANK, sort: "\uffff" };
      if (gran === "year") return { label: `${d.getFullYear()}`, sort: `${d.getFullYear()}` };
      if (gran === "month") {
        const k = `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
        return { label: k, sort: k };
      }
      const k = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      return { label: d.toLocaleDateString(), sort: k };
    }
  }
}

function parseDate(s?: string): Date | null {
  if (!s) return null;
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

// Enforcement column split. Azure uses Default (enforcing) and DoNotEnforce (dry-run); keep a
// stable column order with any other values appended.
function splitValues(rows: PolicyAssignment[]): string[] {
  const set = new Set<string>();
  rows.forEach((a) => set.add(a.enforcement_mode || "Default"));
  const ordered = ["Default", "DoNotEnforce"].filter((v) => set.has(v));
  for (const v of set) if (!ordered.includes(v)) ordered.push(v);
  return ordered;
}

// ----- recursive pivot tree ------------------------------------------------------
export type PivotNode = {
  key: string;
  label: string;
  depth: number;
  counts: Record<string, number>;
  total: number;
  children: PivotNode[];
};

function emptyCounts(splits: string[]): Record<string, number> {
  const c: Record<string, number> = {};
  splits.forEach((s) => (c[s] = 0));
  return c;
}

function buildPivot(rows: PolicyAssignment[], levels: PivotDim[], splits: string[], gran: DtGranularity): PivotNode[] {
  function recurse(items: PolicyAssignment[], depth: number, keyPrefix: string): PivotNode[] {
    if (depth >= levels.length) return [];
    const dim = levels[depth];
    const groups = new Map<string, { sort: string; items: PolicyAssignment[] }>();
    for (const a of items) {
      const { label, sort } = dimValue(a, dim, gran);
      let g = groups.get(label);
      if (!g) { g = { sort, items: [] }; groups.set(label, g); }
      g.items.push(a);
    }
    const nodes: PivotNode[] = [];
    for (const [label, g] of groups) {
      const counts = emptyCounts(splits);
      for (const a of g.items) counts[a.enforcement_mode || "Default"] = (counts[a.enforcement_mode || "Default"] || 0) + 1;
      nodes.push({
        key: `${keyPrefix}/${label}`,
        label,
        depth,
        counts,
        total: g.items.length,
        children: recurse(g.items, depth + 1, `${keyPrefix}/${label}`),
      });
    }
    nodes.sort((a, b) => {
      const sa = groups.get(a.label)!.sort;
      const sb = groups.get(b.label)!.sort;
      return sa < sb ? -1 : sa > sb ? 1 : 0;
    });
    return nodes;
  }
  return recurse(rows, 0, "");
}

// ----- tiny UI atoms (self-contained so this module has no PolicyView coupling) --
function ScopeGlyph({ dim }: { dim: PivotDim }) {
  if (dim === "subscription") return <AzureIcon kind="subscription" className="inline h-3.5 w-3.5" />;
  if (dim === "management_group") return <AzureIcon kind="mg" className="inline h-3.5 w-3.5" />;
  if (dim === "assigned_by") return <span>👤</span>;
  if (dim === "dt") return <span>📅</span>;
  return <span>📜</span>;
}

function csvEscape(v: string): string {
  return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

function download(name: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Build the sheet for a flattened pivot tree (rows + count columns) with per-row outline levels so
// Excel renders the pivot's expand/collapse hierarchy natively.
function pivotSheet(name: string, tree: PivotNode[], splits: string[], grand: { counts: Record<string, number>; total: number }, dimLabels: string): import("../api").PolicyXlsxSheet {
  const columns = [dimLabels, ...splits.map((s) => (s === "DoNotEnforce" ? "Dry-run" : s === "Default" ? "Enforced" : s)), "Total"];
  const rows: (string | number)[][] = [];
  const outline: number[] = [];
  const walk = (ns: PivotNode[]) => {
    for (const n of ns) {
      rows.push([`${"    ".repeat(n.depth)}${n.label}`, ...splits.map((s) => n.counts[s] || 0), n.total]);
      outline.push(n.depth);
      if (n.children.length) walk(n.children);
    }
  };
  walk(tree);
  rows.push(["Grand total", ...splits.map((s) => grand.counts[s] || 0), grand.total]);
  outline.push(0);
  return { name, columns, rows, outline_levels: outline };
}

function stamp(): string {
  return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
}

// ----- savable custom perspectives (localStorage, like adding spreadsheet tabs) --
type Perspective = { id: string; name: string; levels: PivotDim[]; gran: DtGranularity };
const PERSP_KEY = "azsup.policy.perspectives.v1";

function loadPerspectives(): Perspective[] {
  try {
    const raw = localStorage.getItem(PERSP_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((p) => p && Array.isArray(p.levels)) : [];
  } catch { return []; }
}
function savePerspectives(list: Perspective[]) {
  try { localStorage.setItem(PERSP_KEY, JSON.stringify(list)); } catch { /* ignore */ }
}

// ============================================================================ Pivot table
function flatten(nodes: PivotNode[], expanded: Set<string>, out: PivotNode[] = []): PivotNode[] {
  for (const n of nodes) {
    out.push(n);
    if (n.children.length && expanded.has(n.key)) flatten(n.children, expanded, out);
  }
  return out;
}

function PivotTable({
  rows, levels, gran,
}: { rows: PolicyAssignment[]; levels: PivotDim[]; gran: DtGranularity }) {
  const splits = useMemo(() => splitValues(rows), [rows]);
  const baseTree = useMemo(() => buildPivot(rows, levels, splits, gran), [rows, levels, splits, gran]);
  // Default-expand the first level only.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Column sort: "" = natural dimension order; "label" sorts by the row label; a split name or
  // "__total" sorts by that measure. Applied recursively within every level of the hierarchy.
  const [sortCol, setSortCol] = useState<string>("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const tree = useMemo(() => {
    if (!sortCol) return baseTree;
    const cmp = (a: PivotNode, b: PivotNode) => {
      let d: number;
      if (sortCol === "label") d = a.label.localeCompare(b.label);
      else if (sortCol === "__total") d = a.total - b.total;
      else d = (a.counts[sortCol] || 0) - (b.counts[sortCol] || 0);
      if (d === 0) d = a.label.localeCompare(b.label);
      return sortDir === "asc" ? d : -d;
    };
    const sortNodes = (ns: PivotNode[]): PivotNode[] =>
      [...ns].sort(cmp).map((n) => (n.children.length ? { ...n, children: sortNodes(n.children) } : n));
    return sortNodes(baseTree);
  }, [baseTree, sortCol, sortDir]);

  function toggleSort(col: string) {
    if (sortCol === col) {
      // label cycles asc/desc; clicking a measure again flips; a third click on label clears.
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir(col === "label" ? "asc" : "desc");
    }
  }
  const sortArrow = (col: string) => (sortCol === col ? (sortDir === "asc" ? " ▲" : " ▼") : " ↕");

  const grand = useMemo(() => {
    const c = emptyCounts(splits);
    for (const a of rows) c[a.enforcement_mode || "Default"] = (c[a.enforcement_mode || "Default"] || 0) + 1;
    return { counts: c, total: rows.length };
  }, [rows, splits]);

  const visible = useMemo(() => flatten(tree, expanded), [tree, expanded]);

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }
  function expandAll() {
    const all = new Set<string>();
    const walk = (ns: PivotNode[]) => ns.forEach((n) => { if (n.children.length) { all.add(n.key); walk(n.children); } });
    walk(tree);
    setExpanded(all);
  }

  function exportCsv() {
    const header = ["Group", ...splits, "Total"];
    const lines = [header.map(csvEscape).join(",")];
    const walk = (ns: PivotNode[]) => {
      for (const n of ns) {
        const indent = "  ".repeat(n.depth);
        lines.push([`${indent}${n.label}`, ...splits.map((s) => String(n.counts[s] || 0)), String(n.total)].map(csvEscape).join(","));
        if (n.children.length) walk(n.children);
      }
    };
    walk(tree);
    lines.push(["Grand total", ...splits.map((s) => String(grand.counts[s] || 0)), String(grand.total)].map(csvEscape).join(","));
    download(`policy-pivot-${stamp()}.csv`, lines.join("\n"), "text/csv");
  }

  async function exportXlsx() {
    const dimLabels = levels.map((l) => DIM_LABEL[l]).join(" ▸ ");
    const pivot = pivotSheet("Pivot", tree, splits, grand, dimLabels);
    // Raw-data sheet: the flat assignments feeding this pivot.
    const raw = {
      name: "Raw data",
      columns: ["EnforcementMode", "Subscription", "ManagementGroup", "PolicyOrInitiative", "AssignedBy", "CreatedOn", "Description", "AssignmentId"],
      rows: rows.map((a) => [
        a.enforcement_mode || "Default", a.subscription_name || "", a.management_group_display || a.management_group_name || "",
        a.display_name || a.definition_name || "", a.assigned_by || "", a.created_on || "", a.description || "", a.id,
      ] as (string | number)[]),
    };
    const blob = await api.policyExportXlsx(`policy-pivot-${stamp()}`, [pivot, raw]);
    downloadBlob(`policy-pivot-${stamp()}.xlsx`, blob);
  }

  if (!rows.length) {
    return <div className="rounded-lg border border-dashed bg-gray-50/60 p-6 text-center text-xs text-gray-400">No assignments in scope.</div>;
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <button onClick={expandAll} className="rounded border px-2 py-0.5 hover:bg-gray-50">Expand all</button>
        <button onClick={() => setExpanded(new Set())} className="rounded border px-2 py-0.5 hover:bg-gray-50">Collapse all</button>
        <span className="ml-auto">{rows.length} assignment(s)</span>
        <button onClick={exportCsv} className="rounded border px-2 py-0.5 hover:bg-gray-50">⬇ CSV</button>
        <button onClick={() => void exportXlsx()} className="rounded border border-green-300 bg-green-50 px-2 py-0.5 text-green-700 hover:bg-green-100">⬇ Excel</button>
      </div>
      <div className="overflow-x-auto rounded-xl border bg-white shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-[11px] uppercase text-gray-500">
            <tr>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("label")}>{levels.map((l) => DIM_LABEL[l]).join(" ▸ ")}{sortArrow("label")}</th>
              {splits.map((s) => (
                <th key={s} onClick={() => toggleSort(s)} className={`cursor-pointer select-none px-3 py-2 text-right font-medium hover:text-gray-900 ${s === "DoNotEnforce" ? "text-amber-700" : ""}`}>
                  {s === "DoNotEnforce" ? "Dry-run" : s === "Default" ? "Enforced" : s}{sortArrow(s)}
                </th>
              ))}
              <th className="cursor-pointer select-none px-3 py-2 text-right font-medium hover:text-gray-900" onClick={() => toggleSort("__total")}>Total{sortArrow("__total")}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((n) => {
              const dim = levels[n.depth];
              const hasKids = n.children.length > 0;
              const open = expanded.has(n.key);
              return (
                <tr key={n.key} className={`border-t hover:bg-gray-50 ${n.depth === 0 ? "font-medium text-gray-800" : "text-gray-700"}`}>
                  <td className="px-3 py-1.5">
                    <span style={{ paddingLeft: n.depth * 16 }} className="inline-flex items-center gap-1.5">
                      {hasKids ? (
                        <button onClick={() => toggle(n.key)} className="w-4 shrink-0 text-gray-400">{open ? "▾" : "▸"}</button>
                      ) : (
                        <span className="w-4 shrink-0" />
                      )}
                      <ScopeGlyph dim={dim} />
                      <span className={n.label === BLANK ? "italic text-gray-400" : ""}>{n.label}</span>
                    </span>
                  </td>
                  {splits.map((s) => (
                    <td key={s} className={`px-3 py-1.5 text-right tabular-nums ${s === "DoNotEnforce" && (n.counts[s] || 0) > 0 ? "font-semibold text-amber-700" : "text-gray-600"}`}>
                      {n.counts[s] || ""}
                    </td>
                  ))}
                  <td className="px-3 py-1.5 text-right font-medium tabular-nums text-gray-800">
                    {n.total}
                  </td>
                </tr>
              );
            })}
            <tr className="border-t-2 bg-gray-50 font-semibold text-gray-800">
              <td className="px-3 py-2">Grand total</td>
              {splits.map((s) => (
                <td key={s} className="px-3 py-2 text-right tabular-nums">{grand.counts[s] || 0}</td>
              ))}
              <td className="px-3 py-2 text-right tabular-nums">{grand.total}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ============================================================================ Date slicer
function DateSlicer({
  rows, value, onChange,
}: { rows: PolicyAssignment[]; value: [number, number] | null; onChange: (v: [number, number] | null) => void }) {
  // Build the assignment-creation span + a per-day histogram for the brush.
  const { minTs, maxTs, buckets } = useMemo(() => {
    const ts = rows.map((a) => parseDate(a.created_on)).filter(Boolean).map((d) => (d as Date).getTime());
    if (!ts.length) return { minTs: 0, maxTs: 0, buckets: [] as number[] };
    const lo = Math.min(...ts), hi = Math.max(...ts);
    const N = 48;
    const span = Math.max(1, hi - lo);
    const b = new Array(N).fill(0);
    for (const t of ts) {
      const i = Math.min(N - 1, Math.floor(((t - lo) / span) * N));
      b[i]++;
    }
    return { minTs: lo, maxTs: hi, buckets: b };
  }, [rows]);

  if (!minTs) return null;
  const lo = value ? value[0] : minTs;
  const hi = value ? value[1] : maxTs;
  const maxBar = Math.max(1, ...buckets);
  const pct = (t: number) => ((t - minTs) / Math.max(1, maxTs - minTs)) * 100;

  const dayMs = 86400000;
  const toDate = (t: number) => new Date(t).toLocaleDateString();

  function setLo(t: number) { onChange([Math.min(t, hi), hi]); }
  function setHi(t: number) { onChange([lo, Math.max(t, lo)]); }

  return (
    <div className="rounded-xl border bg-white p-3 shadow-sm">
      <div className="mb-1 flex items-center gap-2 text-[11px] text-gray-500">
        <span className="font-medium text-gray-700">📅 Created-on window</span>
        <span className="tabular-nums">{toDate(lo)} → {toDate(hi)}</span>
        <div className="ml-auto flex items-center gap-1">
          {([
            ["30d", 30], ["90d", 90], ["1y", 365],
          ] as const).map(([lbl, days]) => (
            <button key={lbl} onClick={() => onChange([Math.max(minTs, maxTs - days * dayMs), maxTs])} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">{lbl}</button>
          ))}
          <button onClick={() => onChange(null)} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">All</button>
        </div>
      </div>
      {/* histogram */}
      <div className="relative h-10 w-full">
        <div className="flex h-full w-full items-end gap-px">
          {buckets.map((b, i) => {
            const t = minTs + (i / buckets.length) * (maxTs - minTs);
            const inRange = t >= lo - (maxTs - minTs) / buckets.length && t <= hi;
            return <div key={i} className={`flex-1 rounded-t ${inRange ? "bg-brand/60" : "bg-gray-200"}`} style={{ height: `${(b / maxBar) * 100}%` }} />;
          })}
        </div>
        {/* selected band */}
        <div className="pointer-events-none absolute inset-y-0 rounded bg-brand/10" style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
      </div>
      {/* dual range */}
      <div className="relative mt-1 h-4">
        <div className="pointer-events-none absolute top-1/2 h-1 w-full -translate-y-1/2 rounded-full bg-gray-200" />
        <div className="pointer-events-none absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-brand" style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
        <input type="range" min={minTs} max={maxTs} step={dayMs} value={lo} onChange={(e) => setLo(Number(e.target.value))}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
        <input type="range" min={minTs} max={maxTs} step={dayMs} value={hi} onChange={(e) => setHi(Number(e.target.value))}
          className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
      </div>
    </div>
  );
}

// ============================================================================ Tabs

function enfBadge(mode: string) {
  return mode === "Default"
    ? <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700">Enforced</span>
    : <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">Dry-run</span>;
}

function fmtDate(s?: string): string {
  const d = parseDate(s);
  return d ? d.toLocaleDateString() : "—";
}

// --- Tab 1: flat Assignments register ("raw data") -------------------------------
type SortKey = "policy" | "subscription" | "mg" | "assigned_by" | "created" | "enforcement";

export function AssignmentsRegister({ inv }: { inv: PolicyInventory }) {
  const [q, setQ] = useState("");
  const [enf, setEnf] = useState("all");
  const [scope, setScope] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("created");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [toast, setToast] = useState(""); // PU7 — export feedback

  const assignedByOptions = useMemo(() => {
    const s = new Set<string>();
    inv.assignments.forEach((a) => a.assigned_by && s.add(a.assigned_by));
    return Array.from(s).sort();
  }, [inv.assignments]);
  const [byFilter, setByFilter] = useState("all");

  const rows = useMemo(() => {
    const ql = q.toLowerCase();
    const out = inv.assignments.filter((a) => {
      if (enf !== "all" && (a.enforcement_mode || "Default") !== enf) return false;
      if (scope !== "all" && a.scope_kind !== scope) return false;
      if (byFilter !== "all" && (a.assigned_by || BLANK) !== byFilter) return false;
      if (ql) {
        const hay = `${a.display_name} ${a.definition_name} ${a.subscription_name} ${a.management_group_display || a.management_group_name} ${a.assigned_by} ${a.scope_label}`.toLowerCase();
        if (!hay.includes(ql)) return false;
      }
      return true;
    });
    const val = (a: PolicyAssignment): string | number => {
      switch (sortKey) {
        case "policy": return (a.display_name || a.definition_name || "").toLowerCase();
        case "subscription": return (a.subscription_name || "").toLowerCase();
        case "mg": return (a.management_group_display || a.management_group_name || "").toLowerCase();
        case "assigned_by": return (a.assigned_by || "").toLowerCase();
        case "enforcement": return a.enforcement_mode || "";
        case "created": return parseDate(a.created_on)?.getTime() ?? 0;
      }
    };
    out.sort((a, b) => {
      const va = val(a), vb = val(b);
      let cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [inv.assignments, q, enf, scope, byFilter, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir(k === "created" ? "desc" : "asc"); }
  }
  const arrow = (k: SortKey) => (sortKey === k ? (sortDir === "asc" ? " ▲" : " ▼") : " ↕");

  function exportCsv() {
    const cols = ["EnforcementMode", "SubscriptionName", "ManagementGroupName", "PolicyOrInitiativeName", "AssignmentId", "AssignedBy", "CreatedOn", "Description"];
    const lines = [cols.join(",")];
    for (const a of rows) {
      lines.push([
        a.enforcement_mode || "Default", a.subscription_name || "", a.management_group_display || a.management_group_name || "",
        a.display_name || a.definition_name || "", a.id, a.assigned_by || "", a.created_on || "", a.description || "",
      ].map(csvEscape).join(","));
    }
    download(`policy-assignments-${stamp()}.csv`, lines.join("\n"), "text/csv");
    setToast(`Exported ${rows.length} assignment${rows.length === 1 ? "" : "s"} to CSV`);
    setTimeout(() => setToast(""), 2800);
  }

  async function exportXlsx() {
    const cols = ["EnforcementMode", "SubscriptionName", "ManagementGroupName", "PolicyOrInitiativeName", "AssignmentId", "AssignedBy", "CreatedOn", "Description"];
    const xrows = rows.map((a) => [
      a.enforcement_mode || "Default", a.subscription_name || "", a.management_group_display || a.management_group_name || "",
      a.display_name || a.definition_name || "", a.id, a.assigned_by || "", a.created_on || "", a.description || "",
    ] as (string | number)[]);
    const blob = await api.policyExportXlsx(`policy-assignments-${stamp()}`, [{ name: "Assignments", columns: cols, rows: xrows }]);
    downloadBlob(`policy-assignments-${stamp()}.xlsx`, blob);
    setToast(`Exported ${rows.length} assignment${rows.length === 1 ? "" : "s"} to Excel`);
    setTimeout(() => setToast(""), 2800);
  }

  const dryrun = rows.filter((a) => (a.enforcement_mode || "Default") === "DoNotEnforce").length;

  // PP3 — windowed table body: only the visible rows are live <tr> (a real tenant can have
  // thousands of assignments). Spacer rows preserve the table layout + header.
  const scrollRef = useRef<HTMLDivElement>(null);
  const rowVirt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 33,
    overscan: 14,
  });
  const vItems = rowVirt.getVirtualItems();
  const padTop = vItems.length ? vItems[0].start : 0;
  const padBottom = vItems.length ? rowVirt.getTotalSize() - vItems[vItems.length - 1].end : 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select value={enf} onChange={(e) => setEnf(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
          <option value="all">All enforcement</option>
          <option value="Default">Enforced</option>
          <option value="DoNotEnforce">Dry-run</option>
        </select>
        <select value={scope} onChange={(e) => setScope(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
          <option value="all">All scopes</option>
          <option value="managementGroup">Management group</option>
          <option value="subscription">Subscription</option>
          <option value="resourceGroup">Resource group</option>
        </select>
        <select value={byFilter} onChange={(e) => setByFilter(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
          <option value="all">All assigners</option>
          {assignedByOptions.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search assignment / policy…" className="min-w-[180px] flex-1 rounded-md border px-2 py-1 text-xs" />
        <span className="text-[11px] text-gray-400">{rows.length} of {inv.assignments.length}{dryrun ? ` · ${dryrun} dry-run` : ""}</span>
        {toast && <span className="rounded bg-green-50 px-1.5 py-0.5 text-[11px] font-medium text-green-700">✓ {toast}</span>}
        <button onClick={exportCsv} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">⬇ CSV</button>
        <button onClick={() => void exportXlsx()} className="rounded-md border border-green-300 bg-green-50 px-2 py-1 text-xs text-green-700 hover:bg-green-100">⬇ Excel</button>
      </div>
      {/* PU5 — active filter chips. */}
      {(enf !== "all" || scope !== "all" || byFilter !== "all" || q.trim()) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {enf !== "all" && <FilterChip label={`Enforcement: ${enf === "DoNotEnforce" ? "Dry-run" : "Enforced"}`} onClear={() => setEnf("all")} />}
          {scope !== "all" && <FilterChip label={`Scope: ${scope}`} onClear={() => setScope("all")} />}
          {byFilter !== "all" && <FilterChip label={`Assigned by: ${byFilter}`} onClear={() => setByFilter("all")} />}
          {q.trim() && <FilterChip label={`“${q.trim()}”`} onClear={() => setQ("")} />}
          <button onClick={() => { setEnf("all"); setScope("all"); setByFilter("all"); setQ(""); }} className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 hover:bg-gray-50">Clear all</button>
        </div>
      )}
      <div ref={scrollRef} className="max-h-[64vh] overflow-auto rounded-xl border bg-white shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="sticky top-0 z-10 bg-gray-50 text-[11px] uppercase text-gray-500 shadow-sm">
            <tr>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("enforcement")}>Enforce{arrow("enforcement")}</th>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("subscription")}>Subscription{arrow("subscription")}</th>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("mg")}>Mgmt group{arrow("mg")}</th>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("policy")}>Policy / Initiative{arrow("policy")}</th>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("assigned_by")}>Assigned by{arrow("assigned_by")}</th>
              <th className="cursor-pointer px-3 py-2 font-medium" onClick={() => toggleSort("created")}>Created{arrow("created")}</th>
              <th className="px-3 py-2 font-medium">Description</th>
            </tr>
          </thead>
          <tbody>
            {padTop > 0 && <tr style={{ height: padTop }} aria-hidden />}
            {vItems.map((vi) => {
              const a = rows[vi.index];
              return (
              <tr key={a.id} ref={rowVirt.measureElement} data-index={vi.index} className={`border-t hover:bg-gray-50 ${(a.enforcement_mode || "Default") === "DoNotEnforce" ? "bg-amber-50/40" : ""}`}>
                <td className="px-3 py-1.5">{enfBadge(a.enforcement_mode || "Default")}</td>
                <td className="px-3 py-1.5 text-gray-700">{a.subscription_name || (a.scope_kind === "managementGroup" ? "—" : "—")}</td>
                <td className="px-3 py-1.5 text-gray-700">{a.management_group_display || a.management_group_name || "—"}</td>
                <td className="px-3 py-1.5">
                  <div className="font-medium text-gray-800">{a.display_name || a.definition_name}</div>
                  {a.is_initiative && <span className="text-[10px] text-violet-600">initiative</span>}
                </td>
                <td className="px-3 py-1.5 text-gray-700">{a.assigned_by || <span className="italic text-gray-400">{BLANK}</span>}</td>
                <td className="px-3 py-1.5 tabular-nums text-gray-600" title={a.created_on || ""}>{fmtDate(a.created_on)}</td>
                <td className="max-w-[260px] truncate px-3 py-1.5 text-gray-500" title={a.description}>{a.description || "—"}</td>
              </tr>
              );
            })}
            {padBottom > 0 && <tr style={{ height: padBottom }} aria-hidden />}
            {!rows.length && <tr><td colSpan={7} className="px-3 py-8 text-center text-sm text-gray-400">No assignments match the filters.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- Tab 2/3: preset single-purpose pivots ---------------------------------------
export function ByPersonPivot({ inv }: { inv: PolicyInventory }) {
  return (
    <div className="space-y-2">
      <p className="text-xs text-gray-500">Assignments grouped by who created them, then management group → policy. Dry-run (DoNotEnforce) counts are highlighted.</p>
      <PivotTable rows={inv.assignments} levels={["assigned_by", "management_group", "policy"]} gran="day" />
    </div>
  );
}

export function BySubscriptionPivot({ inv }: { inv: PolicyInventory }) {
  return (
    <div className="space-y-2">
      <p className="text-xs text-gray-500">Assignments grouped by subscription, then management group → policy — to spot over/under-governed scopes.</p>
      <PivotTable rows={inv.assignments} levels={["subscription", "management_group", "policy"]} gran="day" />
    </div>
  );
}

// --- Tab 4: timeline with the date slicer ----------------------------------------
export function TimelinePivot({ inv }: { inv: PolicyInventory }) {
  const [gran, setGran] = useState<DtGranularity>("day");
  const [range, setRange] = useState<[number, number] | null>(null);

  const filtered = useMemo(() => {
    if (!range) return inv.assignments;
    const [lo, hi] = range;
    return inv.assignments.filter((a) => {
      const d = parseDate(a.created_on);
      if (!d) return false;
      const t = d.getTime();
      return t >= lo && t <= hi + 86400000; // inclusive of the end day
    });
  }, [inv.assignments, range]);

  return (
    <div className="space-y-3">
      <DateSlicer rows={inv.assignments} value={range} onChange={setRange} />
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-gray-500">Granularity</span>
        <div className="inline-flex overflow-hidden rounded-md border text-xs">
          {(["day", "month", "year"] as const).map((g) => (
            <button key={g} onClick={() => setGran(g)} className={`px-2 py-1 capitalize ${gran === g ? "bg-gray-900 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{g}</button>
          ))}
        </div>
        <span className="ml-auto text-[11px] text-gray-400">{filtered.length} of {inv.assignments.length} in window</span>
      </div>
      <PivotTable rows={filtered} levels={["dt", "assigned_by", "management_group", "policy"]} gran={gran} />
    </div>
  );
}

// --- Tab 5: configurable pivot builder -------------------------------------------
const ALL_DIMS: PivotDim[] = ["assigned_by", "subscription", "management_group", "policy", "dt"];

export function PivotBuilder({ inv }: { inv: PolicyInventory }) {
  const [levels, setLevels] = useState<PivotDim[]>(["assigned_by", "management_group"]);
  const [gran, setGran] = useState<DtGranularity>("day");
  const [saved, setSaved] = useState<Perspective[]>(() => loadPerspectives());

  function addLevel(d: PivotDim) { if (!levels.includes(d)) setLevels([...levels, d]); }
  function removeLevel(d: PivotDim) { setLevels(levels.filter((l) => l !== d)); }
  function moveLevel(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= levels.length) return;
    const next = [...levels];
    [next[i], next[j]] = [next[j], next[i]];
    setLevels(next);
  }

  function savePerspective() {
    const name = window.prompt("Name this perspective:", levels.map((l) => DIM_LABEL[l]).join(" ▸ "));
    if (!name) return;
    const list = [...saved.filter((p) => p.name !== name), { id: `${Date.now()}`, name, levels: [...levels], gran }];
    setSaved(list); savePerspectives(list);
  }
  function applyPerspective(p: Perspective) { setLevels(p.levels); setGran(p.gran); }
  function deletePerspective(id: string) {
    const list = saved.filter((p) => p.id !== id);
    setSaved(list); savePerspectives(list);
  }

  const PRESETS: { label: string; levels: PivotDim[] }[] = [
    { label: "By person", levels: ["assigned_by", "management_group", "policy"] },
    { label: "By subscription", levels: ["subscription", "management_group", "policy"] },
    { label: "Timeline", levels: ["dt", "assigned_by", "policy"] },
    { label: "By initiative", levels: ["policy", "subscription"] },
  ];

  return (
    <div className="space-y-3">
      <div className="rounded-xl border bg-white p-3 shadow-sm">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium uppercase text-gray-400">Rows</span>
          {levels.map((d, i) => (
            <span key={d} className="inline-flex items-center gap-1 rounded-md border bg-gray-50 px-2 py-1 text-xs">
              <button onClick={() => moveLevel(i, -1)} className="text-gray-400 hover:text-gray-700" title="Move up/left">◂</button>
              <ScopeGlyph dim={d} /> {DIM_LABEL[d]}
              <button onClick={() => moveLevel(i, 1)} className="text-gray-400 hover:text-gray-700" title="Move down/right">▸</button>
              <button onClick={() => removeLevel(d)} className="ml-1 text-gray-400 hover:text-red-600" title="Remove">✕</button>
            </span>
          ))}
          {!levels.length && <span className="text-xs italic text-gray-400">Add a dimension →</span>}
          <div className="ml-auto flex items-center gap-1">
            {ALL_DIMS.filter((d) => !levels.includes(d)).map((d) => (
              <button key={d} onClick={() => addLevel(d)} className="rounded-md border border-dashed px-2 py-1 text-xs text-gray-500 hover:bg-gray-50">+ {DIM_LABEL[d]}</button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t pt-2">
          <span className="text-[11px] font-medium uppercase text-gray-400">Presets</span>
          {PRESETS.map((p) => (
            <button key={p.label} onClick={() => setLevels(p.levels)} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">{p.label}</button>
          ))}
          <button onClick={savePerspective} disabled={!levels.length} className="rounded-md border border-brand/40 bg-brand/5 px-2 py-1 text-xs text-brand hover:bg-brand/10 disabled:opacity-50">💾 Save perspective</button>
          {levels.includes("dt") && (
            <span className="ml-auto inline-flex items-center gap-1">
              <span className="text-[11px] text-gray-500">Date</span>
              <div className="inline-flex overflow-hidden rounded-md border text-xs">
                {(["day", "month", "year"] as const).map((g) => (
                  <button key={g} onClick={() => setGran(g)} className={`px-2 py-1 capitalize ${gran === g ? "bg-gray-900 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{g}</button>
                ))}
              </div>
            </span>
          )}
        </div>
        {saved.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-t pt-2">
            <span className="text-[11px] font-medium uppercase text-gray-400">Saved</span>
            {saved.map((p) => (
              <span key={p.id} className="inline-flex items-center gap-1 rounded-md border bg-white px-2 py-1 text-xs">
                <button onClick={() => applyPerspective(p)} className="text-gray-700 hover:text-brand" title={p.levels.map((l) => DIM_LABEL[l]).join(" ▸ ")}>⭐ {p.name}</button>
                <button onClick={() => deletePerspective(p.id)} className="text-gray-300 hover:text-red-600" title="Delete">✕</button>
              </span>
            ))}
          </div>
        )}
      </div>
      {levels.length
        ? <PivotTable rows={inv.assignments} levels={levels} gran={gran} />
        : <div className="rounded-lg border border-dashed bg-gray-50/60 p-6 text-center text-xs text-gray-400">Add at least one row dimension to build a pivot.</div>}
    </div>
  );
}

// --- Tab 6: Governance insights (deterministic cards, no AI) ----------------------
export function GovernanceInsights({ inv }: { inv: PolicyInventory }) {
  const a = inv.assignments;
  const stats = useMemo(() => {
    const total = a.length;
    const dryrun = a.filter((x) => (x.enforcement_mode || "Default") === "DoNotEnforce");
    const noDesc = a.filter((x) => !(x.description || "").trim());
    const initiatives = a.filter((x) => x.is_initiative).length;

    // Attribution: auto (Security Center / Defender / empty) vs human.
    const byAssigner = new Map<string, number>();
    let auto = 0, human = 0, unattributed = 0;
    for (const x of a) {
      const by = (x.assigned_by || "").trim();
      if (!by) { unattributed++; byAssigner.set(BLANK, (byAssigner.get(BLANK) || 0) + 1); continue; }
      byAssigner.set(by, (byAssigner.get(by) || 0) + 1);
      if (/security center|defender|policy insights|asc /i.test(by)) auto++; else human++;
    }
    const topAssigners = [...byAssigner.entries()].sort((p, q) => q[1] - p[1]).slice(0, 8);

    // Per-scope density.
    const byScope = new Map<string, number>();
    for (const x of a) {
      const label = x.subscription_name || (x.management_group_name ? `MG: ${x.management_group_display || x.management_group_name}` : x.scope_label || BLANK);
      byScope.set(label, (byScope.get(label) || 0) + 1);
    }
    const topScopes = [...byScope.entries()].sort((p, q) => q[1] - p[1]).slice(0, 8);

    // Recently created (30 / 90 days).
    const now = Date.now();
    const d30 = a.filter((x) => { const d = parseDate(x.created_on); return d && now - d.getTime() <= 30 * 86400000; });
    const d90 = a.filter((x) => { const d = parseDate(x.created_on); return d && now - d.getTime() <= 90 * 86400000; });
    const recent = [...a]
      .map((x) => ({ x, t: parseDate(x.created_on)?.getTime() ?? 0 }))
      .filter((r) => r.t > 0)
      .sort((p, q) => q.t - p.t)
      .slice(0, 10)
      .map((r) => r.x);

    return { total, dryrun, noDesc, initiatives, auto, human, unattributed, topAssigners, topScopes, d30: d30.length, d90: d90.length, recent };
  }, [a]);

  if (!a.length) {
    return <div className="rounded-lg border border-dashed bg-gray-50/60 p-6 text-center text-xs text-gray-400">No assignments in scope. Run a refresh to load policy assignments.</div>;
  }

  return (
    <div className="space-y-3">
      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <Kpi label="Assignments" value={stats.total} />
        <Kpi label="Dry-run (DoNotEnforce)" value={stats.dryrun.length} tone={stats.dryrun.length ? "text-amber-700" : "text-gray-900"} />
        <Kpi label="Initiatives" value={stats.initiatives} />
        <Kpi label="No description" value={stats.noDesc.length} tone={stats.noDesc.length ? "text-amber-700" : "text-gray-900"} />
        <Kpi label="New · 30d" value={stats.d30} />
        <Kpi label="New · 90d" value={stats.d90} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {/* Dry-run exposure */}
        <InsightCard title="🚨 Dry-run exposure" subtitle="Assignments set to DoNotEnforce — assigned but not enforcing.">
          {stats.dryrun.length === 0 ? (
            <p className="text-xs text-green-600">No dry-run assignments — everything in scope is enforcing.</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {stats.dryrun.slice(0, 12).map((x) => (
                <li key={x.id} className="flex items-center justify-between gap-2 rounded bg-amber-50/60 px-2 py-1">
                  <span className="truncate text-gray-800" title={x.display_name}>{x.display_name || x.definition_name}</span>
                  <span className="shrink-0 text-gray-500">{x.subscription_name || x.scope_label}</span>
                </li>
              ))}
            </ul>
          )}
        </InsightCard>

        {/* Attribution */}
        <InsightCard title="👥 Attribution" subtitle={`${stats.auto} auto-created · ${stats.human} human-authored · ${stats.unattributed} unattributed`}>
          <ul className="space-y-1 text-xs">
            {stats.topAssigners.map(([name, n]) => (
              <li key={name} className="flex items-center justify-between gap-2">
                <span className={`truncate ${name === BLANK ? "italic text-gray-400" : "text-gray-800"}`}>{name}</span>
                <Bar n={n} max={stats.topAssigners[0]?.[1] || 1} />
              </li>
            ))}
          </ul>
        </InsightCard>

        {/* Per-scope density */}
        <InsightCard title="🗂️ Per-scope density" subtitle="Where assignments concentrate (top scopes).">
          <ul className="space-y-1 text-xs">
            {stats.topScopes.map(([name, n]) => (
              <li key={name} className="flex items-center justify-between gap-2">
                <span className={`truncate ${name === BLANK ? "italic text-gray-400" : "text-gray-800"}`}>{name}</span>
                <Bar n={n} max={stats.topScopes[0]?.[1] || 1} />
              </li>
            ))}
          </ul>
        </InsightCard>

        {/* Recently created */}
        <InsightCard title="🕑 Recently created" subtitle="Newest assignments by created-on date.">
          <ul className="space-y-1 text-xs">
            {stats.recent.map((x) => (
              <li key={x.id} className="flex items-center justify-between gap-2">
                <span className="truncate text-gray-800" title={x.display_name}>{x.display_name || x.definition_name}</span>
                <span className="shrink-0 tabular-nums text-gray-500">{fmtDate(x.created_on)}</span>
              </li>
            ))}
            {!stats.recent.length && <li className="text-gray-400">No created-on dates available.</li>}
          </ul>
        </InsightCard>
      </div>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2 shadow-sm">
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}

function InsightCard({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border bg-white p-3 shadow-sm">
      <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
      {subtitle && <p className="mb-2 mt-0.5 text-[11px] text-gray-500">{subtitle}</p>}
      <div className="mt-1">{children}</div>
    </div>
  );
}

function Bar({ n, max }: { n: number; max: number }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-gray-200">
        <span className="block h-full bg-brand" style={{ width: `${Math.max(4, (n / max) * 100)}%` }} />
      </span>
      <span className="w-6 text-right tabular-nums text-gray-700">{n}</span>
    </span>
  );
}
