import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  api,
  type PolicyInventory,
  type PolicyExemption,
  type PolicyAssignment,
  type PolicyExemptionGuardrails,
  type PolicyExemptionPayload,
  type PolicyExemptionPlan,
} from "../api";
import { formatError } from "../utils/format";
import { AzureIcon } from "./AzureIcon";

// Central management for Azure Policy EXEMPTIONS: a registry of every exemption (where it's
// granted, what assignment it waives, when it expires, who created it), hygiene KPIs, and
// create/extend/remove with guardrails + a dedicated confirm-with-diff modal. Writes go to Azure
// via the backend (ARM PUT/DELETE) when the connection is write-enabled; otherwise the operator
// copies the generated az CLI.

const STATUS_TONE: Record<string, string> = {
  expired: "bg-red-100 text-red-700",
  expiring_soon: "bg-amber-100 text-amber-700",
  active: "bg-green-100 text-green-700",
  never: "bg-gray-100 text-gray-600",
};
const STATUS_LABEL: Record<string, string> = {
  expired: "Expired", expiring_soon: "Expiring soon", active: "Active", never: "Never expires",
};

function fmtDate(s?: string): string {
  if (!s) return "—";
  const d = new Date(s);
  return isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}
function daysLabel(e: PolicyExemption): string {
  if (e.status === "never") return "no expiry";
  if (e.days_left == null) return "";
  if (e.days_left < 0) return `${Math.abs(e.days_left)}d ago`;
  return `${e.days_left}d left`;
}
function csvEscape(v: string): string { return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v; }
function download(name: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function downloadBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ScopeGlyph({ kind }: { kind: string }) {
  if (kind === "subscription") return <AzureIcon kind="subscription" className="h-4 w-4" />;
  if (kind === "resourceGroup") return <AzureIcon kind="resource_group" className="h-4 w-4" />;
  if (kind === "managementGroup") return <AzureIcon kind="mg" className="h-4 w-4" />;
  return <span>🌐</span>;
}

function scopeName(e: PolicyExemption): string {
  return e.subscription_name || e.management_group_display || e.scope_label || e.scope;
}

// Sortable columns for the exemptions table. "" = no override (default status order kept).
type ExSortKey = "exemption" | "assignment" | "scope" | "category" | "status" | "expires";
const _STATUS_RANK: Record<string, number> = { expired: 0, expiring_soon: 1, active: 2, never: 3 };
function exSortValue(e: PolicyExemption, key: ExSortKey): string | number {
  switch (key) {
    case "exemption": return (e.display_name || "").toLowerCase();
    case "assignment": return (e.assignment_name || "").toLowerCase();
    case "scope": return scopeName(e).toLowerCase();
    case "category": return (e.category || "").toLowerCase();
    case "status": return _STATUS_RANK[e.status || "active"] ?? 2;
    case "expires": {
      // Never-expiring sorts last on asc; undated/parse-fail also last.
      if (e.status === "never" || !e.expires_on) return Number.POSITIVE_INFINITY;
      const t = new Date(e.expires_on).getTime();
      return isNaN(t) ? Number.POSITIVE_INFINITY : t;
    }
  }
}

// ============================================================ pivot engine (exemptions)
type ExDim = "scope" | "assignment" | "category" | "status" | "created" | "created_by";
type DtGran = "day" | "month" | "year";
type ExSplit = "none" | "category" | "status";
const BLANK = "(blank)";

const EX_DIM_LABEL: Record<ExDim, string> = {
  scope: "Scope", assignment: "Target assignment", category: "Category",
  status: "Status", created: "Created (date)", created_by: "Created by",
};
const ALL_EX_DIMS: ExDim[] = ["scope", "assignment", "category", "status", "created", "created_by"];

function pad(n: number) { return n < 10 ? `0${n}` : `${n}`; }
function exDimValue(e: PolicyExemption, dim: ExDim, gran: DtGran): { label: string; sort: string } {
  switch (dim) {
    case "scope": { const v = scopeName(e); return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" }; }
    case "assignment": { const v = (e.assignment_name || "").trim(); return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" }; }
    case "category": { const v = (e.category || "Waiver").trim(); return { label: v, sort: v.toLowerCase() }; }
    case "status": { const v = STATUS_LABEL[e.status || "active"] || "Active"; return { label: v, sort: (e.status || "active") }; }
    case "created_by": { const v = (e.created_by || "").trim(); return v ? { label: v, sort: v.toLowerCase() } : { label: BLANK, sort: "\uffff" }; }
    case "created": {
      const d = e.created_on ? new Date(e.created_on) : null;
      if (!d || isNaN(d.getTime())) return { label: BLANK, sort: "\uffff" };
      if (gran === "year") return { label: `${d.getFullYear()}`, sort: `${d.getFullYear()}` };
      if (gran === "month") { const k = `${d.getFullYear()}-${pad(d.getMonth() + 1)}`; return { label: k, sort: k }; }
      const k = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      return { label: d.toLocaleDateString(), sort: k };
    }
  }
}
function exSplitValue(e: PolicyExemption, split: ExSplit): string {
  if (split === "category") return e.category || "Waiver";
  if (split === "status") return STATUS_LABEL[e.status || "active"] || "Active";
  return "Count";
}
function exSplitColumns(rows: PolicyExemption[], split: ExSplit): string[] {
  if (split === "none") return ["Count"];
  const set = new Set<string>();
  rows.forEach((e) => set.add(exSplitValue(e, split)));
  const ordered = split === "category" ? ["Waiver", "Mitigated"] : ["Expired", "Expiring soon", "Active", "Never expires"];
  const out = ordered.filter((v) => set.has(v));
  for (const v of set) if (!out.includes(v)) out.push(v);
  return out;
}

type ExPivotNode = { key: string; label: string; depth: number; counts: Record<string, number>; total: number; children: ExPivotNode[] };

function buildExPivot(rows: PolicyExemption[], levels: ExDim[], splits: string[], split: ExSplit, gran: DtGran): ExPivotNode[] {
  function recurse(items: PolicyExemption[], depth: number, prefix: string): ExPivotNode[] {
    if (depth >= levels.length) return [];
    const dim = levels[depth];
    const groups = new Map<string, { sort: string; items: PolicyExemption[] }>();
    for (const e of items) {
      const { label, sort } = exDimValue(e, dim, gran);
      let g = groups.get(label);
      if (!g) { g = { sort, items: [] }; groups.set(label, g); }
      g.items.push(e);
    }
    const nodes: ExPivotNode[] = [];
    for (const [label, g] of groups) {
      const counts: Record<string, number> = {};
      splits.forEach((s) => (counts[s] = 0));
      for (const e of g.items) { const sv = exSplitValue(e, split); counts[sv] = (counts[sv] || 0) + 1; }
      nodes.push({ key: `${prefix}/${label}`, label, depth, counts, total: g.items.length, children: recurse(g.items, depth + 1, `${prefix}/${label}`) });
    }
    nodes.sort((a, b) => { const sa = groups.get(a.label)!.sort, sb = groups.get(b.label)!.sort; return sa < sb ? -1 : sa > sb ? 1 : 0; });
    return nodes;
  }
  return recurse(rows, 0, "");
}

function flattenEx(nodes: ExPivotNode[], expanded: Set<string>, out: ExPivotNode[] = []): ExPivotNode[] {
  for (const n of nodes) { out.push(n); if (n.children.length && expanded.has(n.key)) flattenEx(n.children, expanded, out); }
  return out;
}

// Savable perspectives (localStorage) — same idea as the assignment Pivot builder.
type ExPerspective = { id: string; name: string; levels: ExDim[]; split: ExSplit; gran: DtGran };
const EX_PERSP_KEY = "azsup.policy.exemption_perspectives.v1";
function loadExPerspectives(): ExPerspective[] {
  try { const a = JSON.parse(localStorage.getItem(EX_PERSP_KEY) || "[]"); return Array.isArray(a) ? a.filter((p) => p && Array.isArray(p.levels)) : []; } catch { return []; }
}
function saveExPerspectives(list: ExPerspective[]) { try { localStorage.setItem(EX_PERSP_KEY, JSON.stringify(list)); } catch { /* ignore */ } }

function ExemptionPivot({ rows }: { rows: PolicyExemption[] }) {
  const [levels, setLevels] = useState<ExDim[]>(["scope", "assignment"]);
  const [split, setSplit] = useState<ExSplit>("category");
  const [gran, setGran] = useState<DtGran>("month");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<ExPerspective[]>(() => loadExPerspectives());

  const splits = useMemo(() => exSplitColumns(rows, split), [rows, split]);
  const baseTree = useMemo(() => buildExPivot(rows, levels, splits, split, gran), [rows, levels, splits, split, gran]);
  const [sortCol, setSortCol] = useState<string>("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const tree = useMemo(() => {
    if (!sortCol) return baseTree;
    const cmp = (a: ExPivotNode, b: ExPivotNode) => {
      let d: number;
      if (sortCol === "label") d = a.label.localeCompare(b.label);
      else if (sortCol === "__total") d = a.total - b.total;
      else d = (a.counts[sortCol] || 0) - (b.counts[sortCol] || 0);
      if (d === 0) d = a.label.localeCompare(b.label);
      return sortDir === "asc" ? d : -d;
    };
    const sortNodes = (ns: ExPivotNode[]): ExPivotNode[] =>
      [...ns].sort(cmp).map((n) => (n.children.length ? { ...n, children: sortNodes(n.children) } : n));
    return sortNodes(baseTree);
  }, [baseTree, sortCol, sortDir]);
  function toggleSort(col: string) {
    if (sortCol === col) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortCol(col); setSortDir(col === "label" ? "asc" : "desc"); }
  }
  const sortArrow = (col: string) => (sortCol === col ? (sortDir === "asc" ? " ▲" : " ▼") : " ↕");
  const visible = useMemo(() => flattenEx(tree, expanded), [tree, expanded]);
  const grand = useMemo(() => {
    const c: Record<string, number> = {}; splits.forEach((s) => (c[s] = 0));
    for (const e of rows) { const sv = exSplitValue(e, split); c[sv] = (c[sv] || 0) + 1; }
    return { counts: c, total: rows.length };
  }, [rows, splits, split]);

  function toggle(k: string) { setExpanded((p) => { const n = new Set(p); n.has(k) ? n.delete(k) : n.add(k); return n; }); }
  function expandAll() { const all = new Set<string>(); const w = (ns: ExPivotNode[]) => ns.forEach((n) => { if (n.children.length) { all.add(n.key); w(n.children); } }); w(tree); setExpanded(all); }
  function addLevel(d: ExDim) { if (!levels.includes(d)) setLevels([...levels, d]); }
  function removeLevel(d: ExDim) { setLevels(levels.filter((l) => l !== d)); }
  function moveLevel(i: number, dir: -1 | 1) { const j = i + dir; if (j < 0 || j >= levels.length) return; const n = [...levels]; [n[i], n[j]] = [n[j], n[i]]; setLevels(n); }

  function savePerspective() {
    const name = window.prompt("Name this perspective:", levels.map((l) => EX_DIM_LABEL[l]).join(" ▸ "));
    if (!name) return;
    const list = [...saved.filter((p) => p.name !== name), { id: `${Date.now()}`, name, levels: [...levels], split, gran }];
    setSaved(list); saveExPerspectives(list);
  }
  function applyPerspective(p: ExPerspective) { setLevels(p.levels); setSplit(p.split); setGran(p.gran); }
  function deletePerspective(id: string) { const list = saved.filter((p) => p.id !== id); setSaved(list); saveExPerspectives(list); }

  function exportCsv() {
    const header = ["Group", ...splits, "Total"];
    const lines = [header.map(csvEscape).join(",")];
    const walk = (ns: ExPivotNode[]) => { for (const n of ns) { lines.push([`${"  ".repeat(n.depth)}${n.label}`, ...splits.map((s) => String(n.counts[s] || 0)), String(n.total)].map(csvEscape).join(",")); if (n.children.length) walk(n.children); } };
    walk(tree);
    lines.push(["Grand total", ...splits.map((s) => String(grand.counts[s] || 0)), String(grand.total)].map(csvEscape).join(","));
    download(`policy-exemptions-pivot-${new Date().toISOString().slice(0, 10)}.csv`, lines.join("\n"), "text/csv");
  }

  async function exportXlsx() {
    const dimLabels = levels.map((l) => EX_DIM_LABEL[l]).join(" ▸ ");
    const pcols = [dimLabels, ...splits, "Total"];
    const prows: (string | number)[][] = [];
    const outline: number[] = [];
    const walk = (ns: ExPivotNode[]) => { for (const n of ns) { prows.push([`${"    ".repeat(n.depth)}${n.label}`, ...splits.map((s) => n.counts[s] || 0), n.total]); outline.push(n.depth); if (n.children.length) walk(n.children); } };
    walk(tree);
    prows.push(["Grand total", ...splits.map((s) => grand.counts[s] || 0), grand.total]); outline.push(0);
    const raw = {
      name: "Raw data",
      columns: ["Exemption", "TargetAssignment", "Scope", "Category", "Status", "ExpiresOn", "Justification", "CreatedBy", "ExemptionId"],
      rows: rows.map((e) => [e.display_name, e.assignment_name || "", scopeName(e), e.category, STATUS_LABEL[e.status || "active"] || "", e.expires_on || "", e.description || "", e.created_by || "", e.id] as (string | number)[]),
    };
    const blob = await api.policyExportXlsx(`policy-exemptions-pivot-${new Date().toISOString().slice(0, 10)}`, [
      { name: "Pivot", columns: pcols, rows: prows, outline_levels: outline }, raw,
    ]);
    downloadBlob(`policy-exemptions-pivot-${new Date().toISOString().slice(0, 10)}.xlsx`, blob);
  }

  const PRESETS: { label: string; levels: ExDim[]; split: ExSplit }[] = [
    { label: "By scope", levels: ["scope", "assignment"], split: "category" },
    { label: "By assignment", levels: ["assignment", "scope"], split: "status" },
    { label: "By status", levels: ["status", "scope"], split: "none" },
    { label: "By creator", levels: ["created_by", "scope"], split: "category" },
    { label: "Timeline", levels: ["created", "scope"], split: "category" },
  ];

  return (
    <div className="space-y-2">
      {/* builder */}
      <div className="rounded-xl border bg-white p-3 shadow-sm">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium uppercase text-gray-400">Rows</span>
          {levels.map((d, i) => (
            <span key={d} className="inline-flex items-center gap-1 rounded-md border bg-gray-50 px-2 py-1 text-xs">
              <button onClick={() => moveLevel(i, -1)} className="text-gray-400 hover:text-gray-700" title="Move left">◂</button>
              {EX_DIM_LABEL[d]}
              <button onClick={() => moveLevel(i, 1)} className="text-gray-400 hover:text-gray-700" title="Move right">▸</button>
              <button onClick={() => removeLevel(d)} className="ml-1 text-gray-400 hover:text-red-600" title="Remove">✕</button>
            </span>
          ))}
          {!levels.length && <span className="text-xs italic text-gray-400">Add a dimension →</span>}
          <div className="ml-auto flex items-center gap-1">
            {ALL_EX_DIMS.filter((d) => !levels.includes(d)).map((d) => (
              <button key={d} onClick={() => addLevel(d)} className="rounded-md border border-dashed px-2 py-1 text-xs text-gray-500 hover:bg-gray-50">+ {EX_DIM_LABEL[d]}</button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t pt-2">
          <span className="text-[11px] font-medium uppercase text-gray-400">Columns</span>
          <select value={split} onChange={(e) => setSplit(e.target.value as ExSplit)} className="rounded-md border px-2 py-1 text-xs">
            <option value="category">Split by category</option>
            <option value="status">Split by status</option>
            <option value="none">Count only</option>
          </select>
          {levels.includes("created") && (
            <span className="inline-flex items-center gap-1">
              <span className="text-[11px] text-gray-500">Date</span>
              <div className="inline-flex overflow-hidden rounded-md border text-xs">
                {(["day", "month", "year"] as const).map((g) => (
                  <button key={g} onClick={() => setGran(g)} className={`px-2 py-1 capitalize ${gran === g ? "bg-gray-900 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{g}</button>
                ))}
              </div>
            </span>
          )}
          <span className="text-[11px] font-medium uppercase text-gray-400 ml-2">Presets</span>
          {PRESETS.map((p) => (
            <button key={p.label} onClick={() => { setLevels(p.levels); setSplit(p.split); }} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">{p.label}</button>
          ))}
          <button onClick={savePerspective} disabled={!levels.length} className="rounded-md border border-brand/40 bg-brand/5 px-2 py-1 text-xs text-brand hover:bg-brand/10 disabled:opacity-50">💾 Save perspective</button>
        </div>
        {saved.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-t pt-2">
            <span className="text-[11px] font-medium uppercase text-gray-400">Saved</span>
            {saved.map((p) => (
              <span key={p.id} className="inline-flex items-center gap-1 rounded-md border bg-white px-2 py-1 text-xs">
                <button onClick={() => applyPerspective(p)} className="text-gray-700 hover:text-brand" title={p.levels.map((l) => EX_DIM_LABEL[l]).join(" ▸ ")}>⭐ {p.name}</button>
                <button onClick={() => deletePerspective(p.id)} className="text-gray-300 hover:text-red-600" title="Delete">✕</button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* pivot table */}
      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <button onClick={expandAll} className="rounded border px-2 py-0.5 hover:bg-gray-50">Expand all</button>
        <button onClick={() => setExpanded(new Set())} className="rounded border px-2 py-0.5 hover:bg-gray-50">Collapse all</button>
        <span className="ml-auto">{rows.length} exemption(s)</span>
        <button onClick={exportCsv} className="rounded border px-2 py-0.5 hover:bg-gray-50">⬇ CSV</button>
        <button onClick={() => void exportXlsx()} className="rounded border border-green-300 bg-green-50 px-2 py-0.5 text-green-700 hover:bg-green-100">⬇ Excel</button>
      </div>
      {!levels.length ? (
        <div className="rounded-lg border border-dashed bg-gray-50/60 p-6 text-center text-xs text-gray-400">Add at least one row dimension to build a pivot.</div>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase text-gray-500">
              <tr>
                <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("label")}>{levels.map((l) => EX_DIM_LABEL[l]).join(" ▸ ")}{sortArrow("label")}</th>
                {splits.map((s) => <th key={s} onClick={() => toggleSort(s)} className="cursor-pointer select-none px-3 py-2 text-right font-medium hover:text-gray-900">{s}{sortArrow(s)}</th>)}
                <th className="cursor-pointer select-none px-3 py-2 text-right font-medium hover:text-gray-900" onClick={() => toggleSort("__total")}>Total{sortArrow("__total")}</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((n) => {
                const hasKids = n.children.length > 0; const open = expanded.has(n.key);
                return (
                  <tr key={n.key} className={`border-t hover:bg-gray-50 ${n.depth === 0 ? "font-medium text-gray-800" : "text-gray-700"}`}>
                    <td className="px-3 py-1.5">
                      <span style={{ paddingLeft: n.depth * 16 }} className="inline-flex items-center gap-1.5">
                        {hasKids ? <button onClick={() => toggle(n.key)} className="w-4 shrink-0 text-gray-400">{open ? "▾" : "▸"}</button> : <span className="w-4 shrink-0" />}
                        <span className={n.label === BLANK ? "italic text-gray-400" : ""}>{n.label}</span>
                      </span>
                    </td>
                    {splits.map((s) => <td key={s} className="px-3 py-1.5 text-right tabular-nums text-gray-600">{n.counts[s] || ""}</td>)}
                    <td className="px-3 py-1.5 text-right font-medium tabular-nums text-gray-800">{n.total}</td>
                  </tr>
                );
              })}
              <tr className="border-t-2 bg-gray-50 font-semibold text-gray-800">
                <td className="px-3 py-2">Grand total</td>
                {splits.map((s) => <td key={s} className="px-3 py-2 text-right tabular-nums">{grand.counts[s] || 0}</td>)}
                <td className="px-3 py-2 text-right tabular-nums">{grand.total}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================ main tab
export function ExemptionsTab({
  inv, connectionId, readOnly, focusAssignmentId, focusAssignmentName, openExemptionId, onChanged,
}: {
  inv: PolicyInventory;
  connectionId: string;
  readOnly: boolean;
  focusAssignmentId?: string;
  focusAssignmentName?: string;
  openExemptionId?: string;
  onChanged: () => void;
}) {
  const exemptions = inv.exemptions || [];
  const [view, setView] = useState<"table" | "pivot">("table");
  const [status, setStatus] = useState("all");
  const [scopeKind, setScopeKind] = useState("all");
  const [q, setQ] = useState("");
  const [groupBy, setGroupBy] = useState<"none" | "scope" | "assignment">("none");
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  // When opened from the Effective tab's "N exempt" link, filter to that exact assignment id and
  // show a banner. Cleared on demand.
  const [focusId, setFocusId] = useState<string>(focusAssignmentId || "");
  const [focusBanner, setFocusBanner] = useState<string>(focusAssignmentName || "");
  const [showColFilters, setShowColFilters] = useState(false);
  // Per-column "filter on the values" — text "contains" for free-text columns, exact for
  // category/status. Empty string = no filter on that column.
  const [colFilters, setColFilters] = useState<Record<string, string>>({
    exemption: "", assignment: "", scope: "", category: "", status: "", expires: "", justification: "",
  });
  const setCol = (k: string, v: string) => setColFilters((f) => ({ ...f, [k]: v }));
  const clearColFilters = () => setColFilters({ exemption: "", assignment: "", scope: "", category: "", status: "", expires: "", justification: "" });
  // Re-apply when the focus assignment changes (clicking a different "N exempt" link).
  useEffect(() => {
    setFocusId(focusAssignmentId || "");
    setFocusBanner(focusAssignmentName || "");
    if (focusAssignmentId) setView("table");
  }, [focusAssignmentId, focusAssignmentName]);
  const activeColFilters = Object.values(colFilters).filter(Boolean).length;
  const [sortKey, setSortKey] = useState<ExSortKey>("status");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [editing, setEditing] = useState<PolicyExemption | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmOp, setConfirmOp] = useState<{ kind: "remove"; ex: PolicyExemption } | null>(null);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const qc = useQueryClient();

  // When opened from another tab's "Open" link (e.g. the Inventory exemptions grid), auto-open the
  // edit modal for that exact exemption. Tracked so the same id re-opens after being closed.
  const lastOpenedRef = useRef<string>("");
  useEffect(() => {
    if (!openExemptionId || openExemptionId === lastOpenedRef.current) return;
    const target = exemptions.find((e) => e.id === openExemptionId);
    if (target) {
      lastOpenedRef.current = openExemptionId;
      setView("table");
      setEditing(target);
    }
  }, [openExemptionId, exemptions]);

  const buckets = useMemo(() => {
    const b = { total: exemptions.length, expired: 0, expiring_soon: 0, never: 0, unjustified: 0, active: 0 };
    for (const e of exemptions) {
      if (e.status === "expired") b.expired++;
      else if (e.status === "expiring_soon") b.expiring_soon++;
      else if (e.status === "never") b.never++;
      else b.active++;
      if (!(e.description || "").trim()) b.unjustified++;
    }
    return b;
  }, [exemptions]);

  const rows = useMemo(() => {
    const ql = q.toLowerCase();
    const cf = colFilters;
    const has = (val: string, f: string) => !f || (val || "").toLowerCase().includes(f.toLowerCase());
    const out = exemptions.filter((e) => {
      if (focusId && (e.policy_assignment_id || "").toLowerCase() !== focusId.toLowerCase()) return false;
      if (status === "unjustified") { if ((e.description || "").trim()) return false; }
      else if (status !== "all" && (e.status || "active") !== status) return false;
      if (scopeKind !== "all" && e.scope_kind !== scopeKind) return false;
      if (ql) {
        const hay = `${e.display_name} ${e.assignment_name} ${scopeName(e)} ${e.category} ${e.description}`.toLowerCase();
        if (!hay.includes(ql)) return false;
      }
      // Per-column value filters.
      if (!has(e.display_name, cf.exemption)) return false;
      if (!has(e.assignment_name || "", cf.assignment)) return false;
      if (!has(scopeName(e), cf.scope)) return false;
      if (cf.category && (e.category || "") !== cf.category) return false;
      if (cf.status && (e.status || "active") !== cf.status) return false;
      if (!has(STATUS_LABEL[e.status || "active"] === "Never expires" ? "Never" : (e.expires_on || ""), cf.expires)) return false;
      if (!has(e.description || "", cf.justification)) return false;
      return true;
    });
    out.sort((a, b) => {
      const va = exSortValue(a, sortKey), vb = exSortValue(b, sortKey);
      let cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      if (cmp === 0) cmp = (a.display_name || "").localeCompare(b.display_name || "");
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [exemptions, status, scopeKind, q, colFilters, focusId, sortKey, sortDir]);

  function toggleSort(k: ExSortKey) {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir(k === "expires" ? "asc" : "asc"); }
  }
  const arrow = (k: ExSortKey) => (sortKey === k ? (sortDir === "asc" ? " ▲" : " ▼") : " ↕");

  // Distinct values for the column-filter dropdowns (category, status).
  const categoryOptions = useMemo(() => Array.from(new Set(exemptions.map((e) => e.category || "Waiver"))).sort(), [exemptions]);
  const statusOptions = useMemo(() => {
    const present = new Set(exemptions.map((e) => e.status || "active"));
    return (["expired", "expiring_soon", "active", "never"] as const).filter((s) => present.has(s));
  }, [exemptions]);

  const grouped = useMemo(() => {
    if (groupBy === "none") return null;
    const m = new Map<string, PolicyExemption[]>();
    for (const e of rows) {
      const key = groupBy === "scope" ? scopeName(e) : (e.assignment_name || e.policy_assignment_id || "(unknown)");
      (m.get(key) || m.set(key, []).get(key)!).push(e);
    }
    return [...m.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [rows, groupBy]);

  // Default newly-formed groups to collapsed so a large set is browsable; re-seed when the
  // grouping key or the group set changes.
  useEffect(() => {
    setOpenGroups(new Set());
  }, [groupBy]);

  function toggleGroup(key: string) {
    setOpenGroups((prev) => { const n = new Set(prev); n.has(key) ? n.delete(key) : n.add(key); return n; });
  }
  function expandAllGroups() { setOpenGroups(new Set((grouped || []).map(([k]) => k))); }
  function collapseAllGroups() { setOpenGroups(new Set()); }

  function exportCsv() {
    const cols = ["DisplayName", "TargetAssignment", "Scope", "Category", "Status", "ExpiresOn", "Justification", "CreatedBy", "ExemptionId"];
    const lines = [cols.join(",")];
    for (const e of rows) {
      lines.push([
        e.display_name, e.assignment_name || "", scopeName(e), e.category, STATUS_LABEL[e.status || "active"] || "",
        e.expires_on || "", e.description || "", e.created_by || "", e.id,
      ].map(csvEscape).join(","));
    }
    download(`policy-exemptions-${new Date().toISOString().slice(0, 10)}.csv`, lines.join("\n"), "text/csv");
  }

  async function exportXlsx() {
    const cols = ["DisplayName", "TargetAssignment", "Scope", "Category", "Status", "ExpiresOn", "Justification", "CreatedBy", "ExemptionId"];
    const xrows = rows.map((e) => [
      e.display_name, e.assignment_name || "", scopeName(e), e.category, STATUS_LABEL[e.status || "active"] || "",
      e.expires_on || "", e.description || "", e.created_by || "", e.id,
    ] as (string | number)[]);
    const blob = await api.policyExportXlsx(`policy-exemptions-${new Date().toISOString().slice(0, 10)}`, [{ name: "Exemptions", columns: cols, rows: xrows }]);
    downloadBlob(`policy-exemptions-${new Date().toISOString().slice(0, 10)}.xlsx`, blob);
  }

  async function doRemove(ex: PolicyExemption) {
    try {
      await api.policyExemptionRemove(ex.id, connectionId);
      setMsg({ text: `Removed exemption “${ex.display_name}”.`, ok: true });
      setConfirmOp(null);
      onChanged();
      qc.invalidateQueries({ queryKey: ["policyInventory"] });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  const renderRow = (e: PolicyExemption) => (
    <tr key={e.id} className={`border-t hover:bg-gray-50 ${e.status === "expired" ? "bg-red-50/40" : ""}`}>
      <td className="px-3 py-1.5">
        <div className="font-medium text-gray-800">{e.display_name}</div>
        {!(e.description || "").trim() && <span className="text-[10px] text-amber-600">no justification</span>}
      </td>
      <td className="px-3 py-1.5 text-gray-700">
        {e.assignment_name}
        {e.assignment_is_initiative && <span className="ml-1 text-[10px] text-violet-600">initiative</span>}
      </td>
      <td className="px-3 py-1.5 text-gray-700"><span className="inline-flex items-center gap-1.5"><ScopeGlyph kind={e.scope_kind} /> {scopeName(e)}</span></td>
      <td className="px-3 py-1.5 text-gray-600">{e.category}</td>
      <td className="px-3 py-1.5">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_TONE[e.status || "active"]}`}>{STATUS_LABEL[e.status || "active"]}</span>
        <span className="ml-1 text-[10px] text-gray-400">{daysLabel(e)}</span>
      </td>
      <td className="px-3 py-1.5 tabular-nums text-gray-600">{e.status === "never" ? "Never" : fmtDate(e.expires_on)}</td>
      <td className="max-w-[220px] truncate px-3 py-1.5 text-gray-500" title={e.description}>{e.description || "—"}</td>
      <td className="px-3 py-1.5 text-right">
        <div className="inline-flex gap-1">
          <button onClick={() => setEditing(e)} className="rounded border px-1.5 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">Edit</button>
          <button onClick={() => setConfirmOp({ kind: "remove", ex: e })} className="rounded border border-red-200 px-1.5 py-0.5 text-[11px] text-red-600 hover:bg-red-50">Remove</button>
        </div>
      </td>
    </tr>
  );

  return (
    <div className="space-y-3">
      {/* hygiene KPI strip — clickable filters */}
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
        <Kpi label="Exemptions" value={buckets.total} active={status === "all"} onClick={() => setStatus("all")} />
        <Kpi label="Expired" value={buckets.expired} tone="text-red-700" active={status === "expired"} onClick={() => setStatus("expired")} />
        <Kpi label="Expiring 30d" value={buckets.expiring_soon} tone="text-amber-700" active={status === "expiring_soon"} onClick={() => setStatus("expiring_soon")} />
        <Kpi label="Never expires" value={buckets.never} tone="text-gray-700" active={status === "never"} onClick={() => setStatus("never")} />
        <Kpi label="Unjustified" value={buckets.unjustified} tone="text-amber-700" active={status === "unjustified"} onClick={() => setStatus("unjustified")} />
        <Kpi label="Active" value={buckets.active} tone="text-green-700" active={status === "active"} onClick={() => setStatus("active")} />
      </div>

      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-md border text-xs">
          {(["table", "pivot"] as const).map((v) => (
            <button key={v} onClick={() => setView(v)} className={`px-2.5 py-1 capitalize ${view === v ? "bg-gray-900 text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}>{v === "table" ? "Table" : "🧮 Pivot"}</button>
          ))}
        </div>
        <select value={scopeKind} onChange={(e) => setScopeKind(e.target.value)} className="rounded-md border px-2 py-1 text-xs">
          <option value="all">All scopes</option>
          <option value="managementGroup">Management group</option>
          <option value="subscription">Subscription</option>
          <option value="resourceGroup">Resource group</option>
        </select>
        {view === "table" && (
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as typeof groupBy)} className="rounded-md border px-2 py-1 text-xs">
            <option value="none">No grouping</option>
            <option value="scope">Group by scope</option>
            <option value="assignment">Group by assignment</option>
          </select>
        )}
        {view === "table" && grouped && (
          <div className="inline-flex items-center gap-1">
            <button onClick={expandAllGroups} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">Expand all</button>
            <button onClick={collapseAllGroups} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">Collapse all</button>
          </div>
        )}
        {view === "table" && (
          <button
            onClick={() => setShowColFilters((s) => !s)}
            className={`rounded-md border px-2 py-1 text-xs ${showColFilters || activeColFilters ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
            title="Filter each column by its values"
          >
            ⛃ Filters{activeColFilters ? ` (${activeColFilters})` : ""}
          </button>
        )}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search exemption / assignment…" className="min-w-[180px] flex-1 rounded-md border px-2 py-1 text-xs" />
        <span className="text-[11px] text-gray-400">{rows.length} of {exemptions.length}</span>
        {view === "table" && <button onClick={exportCsv} className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50">⬇ CSV</button>}
        {view === "table" && <button onClick={() => void exportXlsx()} className="rounded-md border border-green-300 bg-green-50 px-2 py-1 text-xs text-green-700 hover:bg-green-100">⬇ Excel</button>}
        <button onClick={() => setCreating(true)} className="rounded-md bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand-dark">+ Add exemption</button>
      </div>

      {readOnly && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800">
          This connection is <b>read-only</b>. You can preview changes and copy the generated CLI; enable writes on the connection (Settings → Connections) to apply directly.
        </div>
      )}
      {msg && <div className={`whitespace-pre-wrap rounded-md px-3 py-1.5 text-xs ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</div>}
      {focusBanner && focusId && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-brand/30 bg-brand/5 px-3 py-1.5 text-xs text-brand">
          <span>Showing exemptions for assignment: <b>{focusBanner}</b> — {rows.length} match.</span>
          <button onClick={() => { setFocusId(""); setFocusBanner(""); }} className="text-brand/70 hover:text-brand hover:underline">Clear ✕</button>
        </div>
      )}

      {/* pivot view */}
      {view === "pivot" ? (
        <ExemptionPivot rows={rows} />
      ) : (
      /* table */
      <div className="overflow-x-auto rounded-xl border bg-white shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-[11px] uppercase text-gray-500">
            <tr>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("exemption")}>Exemption{arrow("exemption")}</th>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("assignment")}>Target assignment{arrow("assignment")}</th>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("scope")}>Scope{arrow("scope")}</th>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("category")}>Category{arrow("category")}</th>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("status")}>Status{arrow("status")}</th>
              <th className="cursor-pointer select-none px-3 py-2 font-medium hover:text-gray-800" onClick={() => toggleSort("expires")}>Expires{arrow("expires")}</th>
              <th className="px-3 py-2 font-medium">Justification</th>
              <th className="px-3 py-2 text-right font-medium">Actions</th>
            </tr>
            {showColFilters && (
              <tr className="border-t bg-white">
                <th className="px-2 py-1.5 font-normal">
                  <input value={colFilters.exemption} onChange={(e) => setCol("exemption", e.target.value)} placeholder="Filter…" className="w-full rounded border px-1.5 py-0.5 text-[11px] font-normal normal-case" />
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <input value={colFilters.assignment} onChange={(e) => setCol("assignment", e.target.value)} placeholder="Filter…" className="w-full rounded border px-1.5 py-0.5 text-[11px] font-normal normal-case" />
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <input value={colFilters.scope} onChange={(e) => setCol("scope", e.target.value)} placeholder="Filter…" className="w-full rounded border px-1.5 py-0.5 text-[11px] font-normal normal-case" />
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <select value={colFilters.category} onChange={(e) => setCol("category", e.target.value)} className="w-full rounded border px-1 py-0.5 text-[11px] font-normal normal-case">
                    <option value="">All</option>
                    {categoryOptions.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <select value={colFilters.status} onChange={(e) => setCol("status", e.target.value)} className="w-full rounded border px-1 py-0.5 text-[11px] font-normal normal-case">
                    <option value="">All</option>
                    {statusOptions.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
                  </select>
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <input value={colFilters.expires} onChange={(e) => setCol("expires", e.target.value)} placeholder="Filter…" className="w-full rounded border px-1.5 py-0.5 text-[11px] font-normal normal-case" />
                </th>
                <th className="px-2 py-1.5 font-normal">
                  <input value={colFilters.justification} onChange={(e) => setCol("justification", e.target.value)} placeholder="Filter…" className="w-full rounded border px-1.5 py-0.5 text-[11px] font-normal normal-case" />
                </th>
                <th className="px-2 py-1.5 text-right">
                  {activeColFilters > 0 && <button onClick={clearColFilters} className="text-[11px] font-normal normal-case text-gray-400 hover:text-red-600">Clear</button>}
                </th>
              </tr>
            )}
          </thead>
          <tbody>
            {grouped
              ? grouped.map(([key, list]) => (
                <GroupRows key={key} title={key} list={list} open={openGroups.has(key)} onToggle={() => toggleGroup(key)} renderRow={renderRow} />
              ))
              : rows.map(renderRow)}
            {!rows.length && <tr><td colSpan={8} className="px-3 py-8 text-center text-sm text-gray-400">No exemptions match the filters.</td></tr>}
          </tbody>
        </table>
      </div>
      )}

      {(creating || editing) && (
        <ExemptionModal
          inv={inv}
          connectionId={connectionId}
          readOnly={readOnly}
          editing={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={(m) => { setCreating(false); setEditing(null); setMsg(m); onChanged(); qc.invalidateQueries({ queryKey: ["policyInventory"] }); }}
        />
      )}
      {confirmOp && (
        <RemoveConfirm
          ex={confirmOp.ex}
          readOnly={readOnly}
          onCancel={() => setConfirmOp(null)}
          onConfirm={() => doRemove(confirmOp.ex)}
        />
      )}
    </div>
  );
}

function GroupRows({ title, list, open, onToggle, renderRow }: { title: string; list: PolicyExemption[]; open: boolean; onToggle: () => void; renderRow: (e: PolicyExemption) => React.ReactNode }) {
  return (
    <>
      <tr className="cursor-pointer border-t bg-gray-50/70 font-medium text-gray-700 hover:bg-gray-100/70" onClick={onToggle}>
        <td colSpan={8} className="px-3 py-1.5">
          <span className="inline-flex items-center gap-1.5">
            <span className="text-gray-400">{open ? "▾" : "▸"}</span>{title}
            <span className="ml-1 rounded bg-gray-200 px-1.5 text-[10px] text-gray-600">{list.length}</span>
          </span>
        </td>
      </tr>
      {open && list.map(renderRow)}
    </>
  );
}

function Kpi({ label, value, tone, active, onClick }: { label: string; value: number; tone?: string; active?: boolean; onClick?: () => void }) {
  return (
    <button onClick={onClick} className={`rounded-lg border bg-white px-3 py-2 text-left shadow-sm transition ${active ? "ring-2 ring-brand/40" : "hover:bg-gray-50"}`}>
      <div className={`text-xl font-semibold ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </button>
  );
}

// ============================================================ create / edit modal
function isoFromDateInput(d: string): string {
  return d ? `${d}T00:00:00Z` : "";
}
function dateInputFromIso(s?: string): string {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

function ExemptionModal({
  inv, connectionId, readOnly, editing, onClose, onSaved,
}: {
  inv: PolicyInventory;
  connectionId: string;
  readOnly: boolean;
  editing: PolicyExemption | null;
  onClose: () => void;
  onSaved: (msg: { text: string; ok: boolean }) => void;
}) {
  const isEdit = !!editing;
  const [scope, setScope] = useState(editing?.scope || "");
  const [assignmentId, setAssignmentId] = useState(editing?.policy_assignment_id || "");
  const [category, setCategory] = useState(editing?.category || "Waiver");
  const [displayName, setDisplayName] = useState(editing?.display_name || "");
  const [description, setDescription] = useState(editing?.description || "");
  const [expires, setExpires] = useState(dateInputFromIso(editing?.expires_on));
  const [never, setNever] = useState(editing?.status === "never");
  const [guardrails, setGuardrails] = useState<PolicyExemptionGuardrails | null>(null);
  const [plan, setPlan] = useState<PolicyExemptionPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.policyExemptionGuardrails().then((r) => setGuardrails(r.guardrails)).catch(() => {});
  }, []);

  // Scope options from the assignments inventory (distinct scopes).
  const scopeOptions = useMemo(() => {
    const m = new Map<string, { scope: string; label: string; kind: string }>();
    for (const a of inv.assignments) {
      if (a.scope && !m.has(a.scope)) {
        m.set(a.scope, { scope: a.scope, label: a.subscription_name || a.management_group_display || a.scope_label || a.scope, kind: a.scope_kind });
      }
    }
    return [...m.values()].sort((x, y) => x.label.localeCompare(y.label));
  }, [inv.assignments]);

  // Assignments at/above the chosen scope are valid exemption targets.
  const assignmentOptions = useMemo(() => {
    if (!scope) return inv.assignments;
    return inv.assignments.filter((a) => scope.toLowerCase().startsWith(a.scope.toLowerCase()) || a.scope.toLowerCase() === scope.toLowerCase());
  }, [inv.assignments, scope]);

  const selectedAssignment: PolicyAssignment | undefined = inv.assignments.find((a) => a.id === assignmentId);

  function payload(): PolicyExemptionPayload {
    return {
      id: editing?.id || "",
      name: editing?.name || "",
      scope: isEdit ? (editing?.scope || "") : scope,
      policy_assignment_id: assignmentId,
      category,
      display_name: displayName,
      description,
      expires_on: never ? "" : isoFromDateInput(expires),
    };
  }

  async function doPlan() {
    setPlanning(true); setErr("");
    try {
      const p = await api.policyExemptionPlan(isEdit ? "update" : "create", payload());
      setPlan(p);
    } catch (e) { setErr(formatError(e)); } finally { setPlanning(false); }
  }

  async function doApply() {
    setApplying(true); setErr("");
    try {
      await api.policyExemptionApply(isEdit ? "update" : "create", payload(), connectionId);
      onSaved({ text: `${isEdit ? "Updated" : "Created"} exemption “${displayName}”.`, ok: true });
    } catch (e) { setErr(formatError(e)); } finally { setApplying(false); }
  }

  const valid = plan?.valid ?? false;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-xl border bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h3 className="text-sm font-semibold text-gray-900">{isEdit ? "Edit exemption" : "Add policy exemption"}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700">✕</button>
        </div>
        <div className="space-y-3 p-4">
          {!isEdit && (
            <Field label="Scope">
              <select value={scope} onChange={(e) => { setScope(e.target.value); setAssignmentId(""); setPlan(null); }} className="w-full rounded-md border px-2 py-1.5 text-sm">
                <option value="">Select a scope…</option>
                {scopeOptions.map((s) => <option key={s.scope} value={s.scope}>{s.label}</option>)}
              </select>
            </Field>
          )}
          <Field label="Target policy assignment">
            <select value={assignmentId} onChange={(e) => { setAssignmentId(e.target.value); setPlan(null); }} disabled={isEdit} className="w-full rounded-md border px-2 py-1.5 text-sm disabled:bg-gray-50">
              <option value="">Select an assignment…</option>
              {assignmentOptions.map((a) => <option key={a.id} value={a.id}>{a.display_name}{a.is_initiative ? " (initiative)" : ""}</option>)}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Display name">
              <input value={displayName} onChange={(e) => { setDisplayName(e.target.value); setPlan(null); }} className="w-full rounded-md border px-2 py-1.5 text-sm" placeholder="e.g. Waiver — legacy VM family" />
            </Field>
            <Field label="Category">
              <select value={category} onChange={(e) => { setCategory(e.target.value); setPlan(null); }} className="w-full rounded-md border px-2 py-1.5 text-sm">
                <option value="Waiver">Waiver</option>
                <option value="Mitigated">Mitigated</option>
              </select>
            </Field>
          </div>
          <Field label={`Justification${guardrails?.require_justification ? " (required)" : ""}`}>
            <textarea value={description} onChange={(e) => { setDescription(e.target.value); setPlan(null); }} rows={2} className="w-full rounded-md border px-2 py-1.5 text-sm" placeholder="Why is this exemption granted? (ticket #, owner, mitigation)" />
          </Field>
          <div className="flex items-end gap-3">
            <Field label="Expires on">
              <input type="date" value={expires} disabled={never} onChange={(e) => { setExpires(e.target.value); setPlan(null); }} className="rounded-md border px-2 py-1.5 text-sm disabled:bg-gray-50" />
            </Field>
            {/* "Never expires" can always be turned OFF (so an existing never-expiring exemption can
                be given an expiry); the guardrail only blocks turning it ON. */}
            <label className={`mb-2 flex items-center gap-1 text-xs ${guardrails?.block_never_expires && !never ? "text-gray-300" : "text-gray-600"}`}>
              <input
                type="checkbox"
                checked={never}
                disabled={!!guardrails?.block_never_expires && !never}
                onChange={(e) => { const v = e.target.checked; setNever(v); if (!v && !expires) { /* leave date for user */ } setPlan(null); }}
              />
              Never expires
            </label>
            {guardrails && guardrails.max_expiry_days > 0 && (
              <span className="mb-2 text-[11px] text-gray-400">Max {guardrails.max_expiry_days} days</span>
            )}
          </div>

          {plan && !plan.valid && (
            <ul className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">
              {plan.errors.map((e, i) => <li key={i}>• {e}</li>)}
            </ul>
          )}
          {plan && plan.valid && (
            <div className="space-y-2 rounded-md border bg-gray-50 p-2">
              <div className="text-[11px] font-medium uppercase text-gray-500">Preview — generated az CLI</div>
              <pre className="overflow-x-auto rounded bg-gray-900 p-2 text-[10px] leading-relaxed text-gray-100">{plan.cli}</pre>
              <div className="flex items-center gap-2">
                <button onClick={() => navigator.clipboard?.writeText(plan.cli)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-white">Copy CLI</button>
                <span className="text-[10px] text-gray-400">ARM PUT {plan.arm.path.split("/").slice(-2).join("/")}</span>
              </div>
            </div>
          )}
          {err && <div className="whitespace-pre-wrap rounded-md bg-red-50 px-3 py-1.5 text-xs text-red-700">{err}</div>}
        </div>
        <div className="flex items-center justify-between gap-2 border-t bg-gray-50 px-4 py-3">
          <span className="text-[11px] text-gray-400">{selectedAssignment ? `Waives: ${selectedAssignment.display_name}` : ""}</span>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-xs hover:bg-white">Cancel</button>
            {!valid ? (
              <button onClick={doPlan} disabled={planning} className="rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50">{planning ? "Validating…" : "Preview & validate"}</button>
            ) : readOnly ? (
              <button onClick={() => navigator.clipboard?.writeText(plan!.cli)} className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-dark">Copy CLI (read-only)</button>
            ) : (
              <button onClick={doApply} disabled={applying} className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-dark disabled:opacity-50">{applying ? "Applying…" : isEdit ? "Apply update" : "Create exemption"}</button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-medium text-gray-600">{label}</span>
      {children}
    </label>
  );
}

// ============================================================ remove confirm (diff preview)
function RemoveConfirm({
  ex, readOnly, onCancel, onConfirm,
}: {
  ex: PolicyExemption;
  readOnly: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cli = `az policy exemption delete --ids "${ex.id}"`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onCancel}>
      <div className="w-full max-w-lg rounded-xl border bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h3 className="text-sm font-semibold text-red-700">Remove exemption</h3>
          <button onClick={onCancel} className="text-gray-400 hover:text-gray-700">✕</button>
        </div>
        <div className="space-y-2 p-4 text-sm">
          <p className="text-gray-700">You're about to remove this exemption. The policy assignment will then apply again to the previously-exempt resources.</p>
          <div className="rounded-md border bg-gray-50 p-2 text-xs">
            <Row k="Exemption" v={ex.display_name} />
            <Row k="Waives" v={ex.assignment_name || ex.policy_assignment_id} />
            <Row k="Scope" v={scopeName(ex)} />
            <Row k="Category" v={ex.category} />
            <Row k="Expires" v={ex.status === "never" ? "Never" : fmtDate(ex.expires_on)} />
          </div>
          <div className="rounded-md bg-gray-900 p-2">
            <pre className="overflow-x-auto text-[10px] leading-relaxed text-gray-100">{cli}</pre>
          </div>
        </div>
        <div className="flex items-center justify-end gap-2 border-t bg-gray-50 px-4 py-3">
          <button onClick={onCancel} className="rounded-md border px-3 py-1.5 text-xs hover:bg-white">Cancel</button>
          {readOnly ? (
            <button onClick={() => navigator.clipboard?.writeText(cli)} className="rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800">Copy CLI (read-only)</button>
          ) : (
            <button onClick={onConfirm} className="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700">Remove exemption</button>
          )}
        </div>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3 border-b border-gray-100 py-0.5 last:border-0">
      <span className="text-gray-500">{k}</span>
      <span className="text-right font-medium text-gray-800">{v}</span>
    </div>
  );
}
