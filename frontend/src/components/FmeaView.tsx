/**
 * FMEA — Failure Mode and Effects Analysis.
 *
 * A workload (via its architecture) can keep MANY FMEA documents (drafts + a published
 * baseline). Each document holds MULTIPLE tables of scored rows; the Risk Priority Number
 * (RPN = Severity x Occurrence x Detection) is always derived server-side. AI generation
 * transforms the architecture's Memory + posture evidence into grounded failure modes and
 * defensible 1-10 scores.
 *
 * Three exports: FmeaPanel (route dispatcher), FmeaIndex (the list), FmeaView (the editor
 * grid that mirrors the classic FMEA worksheet, with colour-coded factor cells and RPN).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fmea as fmeaApi,
  streamRegenerateFmea,
  streamRegenerateFmeaTable,
  type FmeaDoc,
  type FmeaTable,
  type FmeaRow,
  type FmeaResponse,
  type FmeaSummary,
  type FmeaDocumentSummary,
  type FmeaBuildable,
  type FmeaRiskBand,
} from "../api";
import { formatError } from "../utils/format";
import { Skeleton, useDebounced } from "../utils/perf";
import { usePersistedState } from "../utils/persistedState";

const STATUS_META: Record<string, { label: string; cls: string }> = {
  draft: { label: "Draft", cls: "bg-gray-100 text-gray-600" },
  in_review: { label: "In review", cls: "bg-amber-100 text-amber-700" },
  published: { label: "Published", cls: "bg-green-100 text-green-700" },
  archived: { label: "Archived", cls: "bg-gray-200 text-gray-500" },
};
const STATUS_OPTIONS = ["draft", "in_review", "published", "archived"];
const SOURCE_BADGE: Record<string, string> = {
  ai: "bg-violet-100 text-violet-700",
  edited: "bg-sky-100 text-sky-700",
  hybrid: "bg-indigo-100 text-indigo-700",
};

const BAND_META: Record<FmeaRiskBand, { label: string; chip: string; cell: string; dot: string }> = {
  critical: { label: "Critical", chip: "bg-red-100 text-red-700", cell: "bg-red-100 text-red-800", dot: "bg-red-500" },
  high: { label: "High", chip: "bg-orange-100 text-orange-700", cell: "bg-orange-100 text-orange-800", dot: "bg-orange-500" },
  medium: { label: "Medium", chip: "bg-amber-100 text-amber-700", cell: "bg-amber-100 text-amber-800", dot: "bg-amber-400" },
  low: { label: "Low", chip: "bg-green-100 text-green-700", cell: "bg-green-100 text-green-800", dot: "bg-green-500" },
  none: { label: "Unscored", chip: "bg-gray-100 text-gray-500", cell: "bg-gray-50 text-gray-400", dot: "bg-gray-300" },
};

// ---- client-side derivations (the server is authoritative; these are for live feedback) --
function normFactor(v: unknown): number {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n > 10 ? 10 : n;
}
function rpnOf(s: unknown, o: unknown, d: unknown): number | null {
  const a = normFactor(s), b = normFactor(o), c = normFactor(d);
  if (a === 0 || b === 0 || c === 0) return null;
  return a * b * c;
}
function bandOf(rpn: number | null): FmeaRiskBand {
  if (rpn === null) return "none";
  if (rpn >= 200) return "critical";
  if (rpn >= 120) return "high";
  if (rpn >= 40) return "medium";
  return "low";
}
function factorCellClass(n: number): string {
  const v = normFactor(n);
  if (v === 0) return "bg-white text-gray-400";
  if (v >= 8) return "bg-red-100 text-red-800 font-semibold";
  if (v >= 4) return "bg-amber-100 text-amber-800 font-medium";
  return "bg-green-100 text-green-800 font-medium";
}

function emptyRow(): FmeaRow {
  return {
    id: `row-${Math.random().toString(36).slice(2, 10)}`,
    item: "", function: "", failure_mode: "", effects: "", causes: "",
    control_prevention: "", control_detection: "", recommended_actions: "",
    owner: "", date_due: "", action_results: "", date_completed: "",
    severity: 0, occurrence: 0, detection: 0,
    severity_post: 0, occurrence_post: 0, detection_post: 0,
    rpn: null, rpn_post: null, risk_band: "none", risk_band_post: "none",
  };
}

function fmtElapsed(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

// ⟦TODO: <label> | key=<field>⟧ placeholder tokens come from AI generation for human-only
// fields (owner, due date). We never surface the raw token — a blank, fillable cell is the
// signal "needs a human". This strips any TODO token (and stray bracket variants) to "".
const TODO_RE = /⟦\s*TODO[\s\S]*?⟧/gi;
function stripTodo(value: string): string {
  if (!value) return "";
  const cleaned = value.replace(TODO_RE, "").trim();
  return cleaned;
}

// Coerce an arbitrary stored value to an ISO date (YYYY-MM-DD) for <input type="date">,
// or "" if it's empty / a TODO token / unparseable.
function toISODate(value: string): string {
  const v = stripTodo(value);
  if (!v) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
  const d = new Date(v);
  if (!Number.isNaN(d.getTime())) return d.toISOString().slice(0, 10);
  return "";
}

// Strip AI ⟦TODO⟧ placeholder tokens out of the human-only fields (owner / due / completed)
// so they never surface in the grid, the CSV export, or a saved document. Run once on load.
function sanitizeDoc(doc: FmeaDoc): FmeaDoc {
  return {
    ...doc,
    tables: (doc.tables || []).map((t) => ({
      ...t,
      rows: (t.rows || []).map((r) => ({
        ...r,
        owner: stripTodo(r.owner),
        date_due: toISODate(r.date_due),
        date_completed: toISODate(r.date_completed),
      })),
    })),
  };
}

// ============================================================================ summary chips
function SummaryBar({ summary }: { summary: FmeaSummary }) {
  const order: FmeaRiskBand[] = ["critical", "high", "medium", "low"];
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {order.map((b) => (
        <span key={b} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${BAND_META[b].chip}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${BAND_META[b].dot}`} />
          {BAND_META[b].label}: {summary.counts[b] ?? 0}
        </span>
      ))}
      <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">
        {summary.total_rows} row{summary.total_rows === 1 ? "" : "s"}
      </span>
      {summary.top_rpn > 0 && (
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">Top RPN {summary.top_rpn}</span>
      )}
      {summary.mitigated_rows > 0 && (
        <span className="rounded-full bg-green-50 px-2 py-0.5 text-[11px] text-green-600">{summary.mitigated_rows} mitigated</span>
      )}
    </div>
  );
}

// ============================================================================ the grid
function FactorInput({ value, onChange, label }: { value: number; onChange: (n: number) => void; label: string }) {
  return (
    <input
      type="number"
      min={0}
      max={10}
      value={value || ""}
      aria-label={label}
      onChange={(e) => onChange(normFactor(e.target.value))}
      className={`h-full w-full border-0 bg-transparent px-1 py-0.5 text-center text-[11px] outline-none focus:ring-2 focus:ring-brand/40 ${factorCellClass(value)}`}
      placeholder="–"
    />
  );
}

function TextCell({ value, onChange, label, mono }: { value: string; onChange: (v: string) => void; label: string; mono?: boolean }) {
  return (
    <textarea
      value={value}
      aria-label={label}
      rows={3}
      onChange={(e) => onChange(e.target.value)}
      className={`block h-full w-full resize-none border-0 bg-transparent px-1.5 py-0.5 text-[10px] leading-snug text-gray-800 outline-none focus:ring-2 focus:ring-brand/40 ${mono ? "font-mono" : ""}`}
    />
  );
}

// A free-text cell that hides any AI ⟦TODO⟧ placeholder (shows a blank, fillable input with a
// hint instead). Used for human-only fields like Owner.
function HumanTextCell({ value, onChange, label, placeholder }: { value: string; onChange: (v: string) => void; label: string; placeholder?: string }) {
  return (
    <textarea
      value={stripTodo(value)}
      aria-label={label}
      rows={3}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="block h-full w-full resize-none border-0 bg-transparent px-1.5 py-0.5 text-[10px] leading-snug text-gray-800 outline-none placeholder:text-gray-300 focus:ring-2 focus:ring-brand/40"
    />
  );
}

// A calendar date picker cell (native <input type="date">). Ignores TODO tokens / junk.
function DateCell({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <input
      type="date"
      value={toISODate(value)}
      aria-label={label}
      onChange={(e) => onChange(e.target.value)}
      className="block h-full w-full border-0 bg-transparent px-1.5 py-0.5 text-[10px] text-gray-800 outline-none focus:ring-2 focus:ring-brand/40"
    />
  );
}

function FmeaTableGrid({
  table, onChange, onRemove, onRegen, regenning, readOnly,
}: {
  table: FmeaTable;
  onChange: (t: FmeaTable) => void;
  onRemove: () => void;
  onRegen: () => void;
  regenning: boolean;
  readOnly: boolean;
}) {
  const setRow = (rowId: string, patch: Partial<FmeaRow>) =>
    onChange({ ...table, rows: table.rows.map((r) => (r.id === rowId ? { ...r, ...patch } : r)) });
  const addRow = () => onChange({ ...table, rows: [...table.rows, emptyRow()] });
  const removeRow = (rowId: string) => onChange({ ...table, rows: table.rows.filter((r) => r.id !== rowId) });

  // Sorting by a numeric column is a VIEW concern only — edits/removes act by row id, so the
  // underlying order is preserved. Clicking a numeric header cycles desc → asc → off.
  type SortKey = "severity" | "occurrence" | "detection" | "rpn" | "severity_post" | "occurrence_post" | "detection_post" | "rpn_post";
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);
  const toggleSort = (key: SortKey) =>
    setSort((s) => (s?.key !== key ? { key, dir: "desc" } : s.dir === "desc" ? { key, dir: "asc" } : null));
  const sortVal = (r: FmeaRow, key: SortKey): number | null => {
    if (key === "rpn") return rpnOf(r.severity, r.occurrence, r.detection);
    if (key === "rpn_post") return rpnOf(r.severity_post, r.occurrence_post, r.detection_post);
    return normFactor(r[key]) || null;
  };
  const displayRows = useMemo(() => {
    const indexed = table.rows.map((r, i) => ({ r, i }));
    if (!sort) return indexed;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...indexed].sort((a, b) => {
      const av = sortVal(a.r, sort.key);
      const bv = sortVal(b.r, sort.key);
      // Unscored (null) rows always sink to the bottom regardless of direction.
      if (av === null && bv === null) return a.i - b.i;
      if (av === null) return 1;
      if (bv === null) return -1;
      return (av - bv) * dir || a.i - b.i;
    });
  }, [table.rows, sort]);

  const th = "border border-gray-200 bg-gray-100 px-1.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-600";
  const td = "border border-gray-200 align-top";
  const sortArrow = (key: SortKey) => (sort?.key === key ? (sort.dir === "desc" ? " ↓" : " ↑") : "");
  // A clickable numeric column header (sortable). Keeps the compact width classes.
  const numTh = (key: SortKey, label: string, extra = "") => (
    <th
      className={`${th} ${extra} cursor-pointer select-none hover:bg-gray-200`}
      rowSpan={2}
      title={`Sort by ${label}`}
      onClick={() => toggleSort(key)}
    >
      {label}{sortArrow(key)}
    </th>
  );

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50/70 px-3 py-2">
        <span className="text-sm font-semibold text-gray-800">
          <input
            value={table.name}
            aria-label="Table name"
            onChange={(e) => onChange({ ...table, name: e.target.value })}
            className="rounded border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-gray-800 hover:border-gray-200 focus:border-brand/40 focus:outline-none"
          />
        </span>
        <input
          value={table.scope_ref}
          aria-label="Scope reference"
          placeholder="scope / resource group…"
          onChange={(e) => onChange({ ...table, scope_ref: e.target.value })}
          className="rounded border border-transparent bg-transparent px-1 py-0.5 text-[11px] text-gray-500 hover:border-gray-200 focus:border-brand/40 focus:outline-none"
        />
        <span className="ml-auto flex items-center gap-1.5">
          <button
            onClick={onRegen}
            disabled={regenning || readOnly}
            title="Regenerate this table's rows from the architecture Memory"
            className="rounded-lg border border-violet-200 bg-violet-50 px-2 py-1 text-[11px] font-medium text-violet-700 hover:bg-violet-100 disabled:opacity-50"
          >
            {regenning ? "Regenerating…" : "✨ Regenerate"}
          </button>
          <button
            onClick={onRemove}
            disabled={readOnly}
            title="Remove this table"
            className="rounded-lg border px-2 py-1 text-[11px] text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50"
          >
            🗑️
          </button>
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[12px]" style={{ minWidth: 1700 }}>
          <thead>
            <tr>
              <th className={`${th} w-8`} rowSpan={2}>ID</th>
              <th className={th} rowSpan={2} style={{ minWidth: 130 }}>System / Item / Process Step</th>
              <th className={th} rowSpan={2} style={{ minWidth: 100 }}>Function</th>
              <th className={th} rowSpan={2} style={{ minWidth: 130 }}>Potential Failure Mode</th>
              <th className={th} rowSpan={2} style={{ minWidth: 130 }}>Effects of Failure</th>
              {numTh("severity", "Sev", "w-12")}
              <th className={th} rowSpan={2} style={{ minWidth: 120 }}>Causes</th>
              {numTh("occurrence", "Occ", "w-12")}
              <th className={th} colSpan={2}>Current Controls</th>
              {numTh("detection", "Det", "w-12")}
              {numTh("rpn", "RPN", "w-14")}
              <th className={th} rowSpan={2} style={{ minWidth: 130 }}>Recommended Actions</th>
              <th className={th} rowSpan={2} style={{ minWidth: 90 }}>Owner</th>
              <th className={th} rowSpan={2} style={{ minWidth: 140 }}>Date Due</th>
              <th className={`${th} bg-sky-50`} colSpan={6}>FMEA Results</th>
              <th className={`${th} w-8`} rowSpan={2} />
            </tr>
            <tr>
              <th className={th} style={{ minWidth: 100 }}>Prevention</th>
              <th className={th} style={{ minWidth: 100 }}>Detection</th>
              <th className={`${th} bg-sky-50`} style={{ minWidth: 120 }}>Action Results</th>
              <th className={`${th} bg-sky-50`} style={{ minWidth: 140 }}>Date Completed</th>
              {numTh("severity_post", "Sev", "w-12 bg-sky-50")}
              {numTh("occurrence_post", "Occ", "w-12 bg-sky-50")}
              {numTh("detection_post", "Det", "w-12 bg-sky-50")}
              {numTh("rpn_post", "RPN", "w-14 bg-sky-50")}
            </tr>
          </thead>
          <tbody>
            {displayRows.map(({ r, i: ri }) => {
              const rpn = rpnOf(r.severity, r.occurrence, r.detection);
              const rpnPost = rpnOf(r.severity_post, r.occurrence_post, r.detection_post);
              const band = BAND_META[bandOf(rpn)];
              const bandPost = BAND_META[bandOf(rpnPost)];
              return (
                <tr key={r.id} className="hover:bg-gray-50/40">
                  <td className={`${td} px-1 text-center text-[10px] text-gray-400`}>{ri + 1}</td>
                  <td className={td}><TextCell value={r.item} onChange={(v) => setRow(r.id, { item: v })} label="Item" /></td>
                  <td className={td}><TextCell value={r.function} onChange={(v) => setRow(r.id, { function: v })} label="Function" /></td>
                  <td className={td}><TextCell value={r.failure_mode} onChange={(v) => setRow(r.id, { failure_mode: v })} label="Failure mode" /></td>
                  <td className={td}><TextCell value={r.effects} onChange={(v) => setRow(r.id, { effects: v })} label="Effects" /></td>
                  <td className={`${td} p-0`}><FactorInput value={r.severity} onChange={(n) => setRow(r.id, { severity: n })} label="Severity" /></td>
                  <td className={td}><TextCell value={r.causes} onChange={(v) => setRow(r.id, { causes: v })} label="Causes" /></td>
                  <td className={`${td} p-0`}><FactorInput value={r.occurrence} onChange={(n) => setRow(r.id, { occurrence: n })} label="Occurrence" /></td>
                  <td className={td}><TextCell value={r.control_prevention} onChange={(v) => setRow(r.id, { control_prevention: v })} label="Prevention control" /></td>
                  <td className={td}><TextCell value={r.control_detection} onChange={(v) => setRow(r.id, { control_detection: v })} label="Detection control" /></td>
                  <td className={`${td} p-0`}><FactorInput value={r.detection} onChange={(n) => setRow(r.id, { detection: n })} label="Detection" /></td>
                  <td className={`${td} px-1 text-center text-[11px] font-semibold ${band.cell}`} title={band.label}>{rpn ?? "–"}</td>
                  <td className={td}><TextCell value={r.recommended_actions} onChange={(v) => setRow(r.id, { recommended_actions: v })} label="Recommended actions" /></td>
                  <td className={td}><HumanTextCell value={r.owner} onChange={(v) => setRow(r.id, { owner: v })} label="Owner" placeholder="Owner…" /></td>
                  <td className={td}><DateCell value={r.date_due} onChange={(v) => setRow(r.id, { date_due: v })} label="Date due" /></td>
                  <td className={`${td} bg-sky-50/40`}><TextCell value={r.action_results} onChange={(v) => setRow(r.id, { action_results: v })} label="Action results" /></td>
                  <td className={`${td} bg-sky-50/40`}><DateCell value={r.date_completed} onChange={(v) => setRow(r.id, { date_completed: v })} label="Date completed" /></td>
                  <td className={`${td} bg-sky-50/40 p-0`}><FactorInput value={r.severity_post} onChange={(n) => setRow(r.id, { severity_post: n })} label="Severity (post)" /></td>
                  <td className={`${td} bg-sky-50/40 p-0`}><FactorInput value={r.occurrence_post} onChange={(n) => setRow(r.id, { occurrence_post: n })} label="Occurrence (post)" /></td>
                  <td className={`${td} bg-sky-50/40 p-0`}><FactorInput value={r.detection_post} onChange={(n) => setRow(r.id, { detection_post: n })} label="Detection (post)" /></td>
                  <td className={`${td} px-1 text-center text-[11px] font-semibold ${bandPost.cell}`} title={bandPost.label}>{rpnPost ?? "–"}</td>
                  <td className={`${td} p-0 text-center`}>
                    <button
                      onClick={() => removeRow(r.id)}
                      title="Delete row"
                      className="px-1 py-1 text-gray-300 hover:text-red-500"
                    >
                      ×
                    </button>
                  </td>
                </tr>
              );
            })}
            {table.rows.length === 0 && (
              <tr>
                <td colSpan={22} className="border border-gray-200 px-3 py-4 text-center text-[12px] text-gray-400">
                  No rows yet — add a failure mode or regenerate with AI.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="border-t bg-gray-50/50 px-3 py-1.5">
        <button onClick={addRow} className="rounded-lg border border-gray-300 bg-white px-2.5 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-50">+ Add row</button>
      </div>
    </div>
  );
}

// ============================================================================ detail view
export function FmeaView({ fmeaId }: { fmeaId: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ["fmea", fmeaId], queryFn: () => fmeaApi.get(fmeaId), staleTime: 5 * 60 * 1000 });

  const [doc, setDoc] = useState<FmeaDoc | null>(null);
  const [summary, setSummary] = useState<FmeaSummary | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genStatus, setGenStatus] = useState("");
  const [genLog, setGenLog] = useState<{ t: string; phase: string; msg: string }[]>([]);
  const [genStart, setGenStart] = useState(0);
  const [genElapsed, setGenElapsed] = useState(0);
  const [regenTable, setRegenTable] = useState<string>("");
  const [hasMemory, setHasMemory] = useState(true);
  const [exportNote, setExportNote] = useState(""); // export-feedback toast
  const abortRef = useRef<AbortController | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  // Tick an elapsed-time counter while a generation is in flight.
  useEffect(() => {
    if (!generating && !regenTable) return;
    const id = setInterval(() => setGenElapsed(Math.floor((Date.now() - genStart) / 1000)), 1000);
    return () => clearInterval(id);
  }, [generating, regenTable, genStart]);

  // Auto-scroll the activity log to the newest entry.
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [genLog]);

  useEffect(() => {
    if (query.data) {
      setDoc(sanitizeDoc(query.data.fmea));
      setSummary(query.data.summary);
      setHasMemory(query.data.has_memory ?? true);
      setDirty(false);
    }
  }, [query.data]);

  const architectureId = doc?.architecture_id || query.data?.architecture.id || "";
  const readOnly = doc?.status === "archived";

  const applyResponse = useCallback((r: FmeaResponse) => {
    setDoc(sanitizeDoc(r.fmea));
    setSummary(r.summary);
    setDirty(false);
    void qc.invalidateQueries({ queryKey: ["fmeaIndex"] });
  }, [qc]);

  const save = useCallback(async () => {
    if (!doc) return;
    setSaving(true);
    setError("");
    try {
      const r = await fmeaApi.save(fmeaId, {
        title: doc.title, scope_note: doc.scope_note, tables: doc.tables, status: doc.status,
      });
      applyResponse(r);
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }, [doc, fmeaId, applyResponse]);

  // Debounced autosave when the document is dirty (1.2s idle).
  useEffect(() => {
    if (!dirty || generating || saving) return;
    const t = setTimeout(() => void save(), 1200);
    return () => clearTimeout(t);
  }, [dirty, doc, generating, saving, save]);

  function patchDoc(patch: Partial<FmeaDoc>) {
    setDoc((d) => (d ? { ...d, ...patch } : d));
    setDirty(true);
  }
  function setTable(t: FmeaTable) {
    setDoc((d) => (d ? { ...d, tables: d.tables.map((x) => (x.id === t.id ? t : x)) } : d));
    setDirty(true);
  }
  function addTable() {
    const t: FmeaTable = { id: `tbl-${Math.random().toString(36).slice(2, 10)}`, name: "New table", scope_ref: "", rows: [emptyRow()] };
    setDoc((d) => (d ? { ...d, tables: [...d.tables, t] } : d));
    setDirty(true);
  }
  function removeTable(id: string) {
    if (!window.confirm("Remove this table?")) return;
    setDoc((d) => (d ? { ...d, tables: d.tables.filter((t) => t.id !== id) } : d));
    setDirty(true);
  }

  // Append one line to the live activity log (kept to a sane length) + update the headline.
  const pushLog = useCallback((phase: string, msg: string) => {
    const t = new Date().toLocaleTimeString([], { hour12: false });
    setGenStatus(msg);
    setGenLog((prev) => [...prev.slice(-199), { t, phase, msg }]);
  }, []);

  async function generate(full: boolean) {
    if (!architectureId && full) return;
    setGenerating(true);
    setGenStatus("Starting…");
    setGenLog([]);
    setGenStart(Date.now());
    setGenElapsed(0);
    setError("");
    abortRef.current = new AbortController();
    pushLog("start", "🚀 Starting FMEA generation…");
    const handlers = {
      onStatus: (s: { phase: string; message: string }) => pushLog(s.phase, s.message),
      onDone: (r: FmeaResponse) => {
        const n = (r.fmea.tables || []).reduce((a, t) => a + (t.rows?.length || 0), 0);
        pushLog("done", `✅ Done — ${r.fmea.tables.length} table(s) · ${n} failure mode(s).`);
        applyResponse(r);
        setGenerating(false);
      },
      onError: (msg: string) => { pushLog("error", `❌ ${msg}`); setError(msg); setGenerating(false); },
    };
    try {
      await streamRegenerateFmea(fmeaId, handlers, abortRef.current.signal);
    } catch (e) {
      setError(formatError(e));
      setGenerating(false);
    }
  }

  // Reconnect to a generation still running server-side (started before navigating here, or
  // continuing after navigating away and back). The backend job is idempotent — re-POSTing the
  // stream just FOLLOWS the in-flight job; the saved doc lands via onDone regardless.
  useEffect(() => {
    if (!fmeaId) return;
    let cancelled = false;
    void fmeaApi.generateJob(fmeaId).then((r) => {
      if (cancelled || !r.job || r.job.status !== "running" || generating) return;
      setGenerating(true);
      setGenStatus("Reconnecting…");
      setGenStart(Date.now());
      setGenElapsed(0);
      abortRef.current = new AbortController();
      pushLog("start", "🔄 Reconnecting to a generation in progress…");
      const handlers = {
        onStatus: (s: { phase: string; message: string }) => pushLog(s.phase, s.message),
        onDone: (rr: FmeaResponse) => {
          const n = (rr.fmea.tables || []).reduce((a, t) => a + (t.rows?.length || 0), 0);
          pushLog("done", `✅ Done — ${rr.fmea.tables.length} table(s) · ${n} failure mode(s).`);
          applyResponse(rr);
          setGenerating(false);
        },
        onError: (msg: string) => { pushLog("error", `❌ ${msg}`); setError(msg); setGenerating(false); },
      };
      void streamRegenerateFmea(fmeaId, handlers, abortRef.current.signal).catch(() => setGenerating(false));
    }).catch(() => { /* no job — fine */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fmeaId]);

  async function regenerateTable(tableId: string) {
    setRegenTable(tableId);
    setGenLog([]);
    setGenStart(Date.now());
    setGenElapsed(0);
    setError("");
    abortRef.current = new AbortController();
    pushLog("start", "🚀 Regenerating table…");
    try {
      await streamRegenerateFmeaTable(fmeaId, tableId, {
        onStatus: (s) => pushLog(s.phase, s.message),
        onDone: (r) => { pushLog("done", "✅ Table regenerated."); applyResponse(r); setRegenTable(""); },
        onError: (msg) => { pushLog("error", `❌ ${msg}`); setError(msg); setRegenTable(""); },
      }, abortRef.current.signal);
    } catch (e) {
      setError(formatError(e));
      setRegenTable("");
    }
  }

  function cancelGen() {
    abortRef.current?.abort();
    pushLog("cancel", "⏹️ Cancelled.");
    setGenerating(false);
    setRegenTable("");
  }

  if (query.isLoading || !doc) {
    return <div className="p-6"><Skeleton rows={8} /></div>;
  }

  const sm = STATUS_META[doc.status] ?? STATUS_META.draft;
  const isEmpty = doc.tables.length === 0;

  return (
    <div className="flex h-full flex-col bg-gray-50/40">
      {/* header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex items-center gap-2 text-[12px] text-gray-400">
          <button onClick={() => navigate("/fmea")} className="hover:text-gray-600">FMEA</button>
          <span>/</span>
          <span className="text-gray-500">{doc.workload_name || "document"}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <input
            value={doc.title}
            aria-label="FMEA title"
            onChange={(e) => patchDoc({ title: e.target.value })}
            className="min-w-0 flex-1 rounded border border-transparent px-1 py-0.5 text-lg font-bold text-gray-900 hover:border-gray-200 focus:border-brand/40 focus:outline-none"
          />
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${sm.cls}`}>{sm.label}</span>
          {doc.source && <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${SOURCE_BADGE[doc.source] ?? "bg-gray-100 text-gray-600"}`}>{doc.source}</span>}
          <span className="shrink-0 text-[11px] text-gray-400">{saving ? "Saving…" : dirty ? "Unsaved" : "Saved"}</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          {summary && <SummaryBar summary={summary} />}
          <div className="flex items-center gap-1.5">
            <select
              value={doc.status}
              aria-label="Status"
              onChange={(e) => patchDoc({ status: e.target.value as FmeaDoc["status"] })}
              className="rounded-lg border border-gray-300 px-2 py-1 text-[12px] text-gray-600 focus:border-brand-dark focus:outline-none"
            >
              {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{STATUS_META[s].label}</option>)}
            </select>
            <a
              href={fmeaApi.exportXlsxUrl(fmeaId)}
              onClick={() => { setExportNote("Excel workbook downloading…"); setTimeout(() => setExportNote(""), 2800); }}
              className="rounded-lg border border-emerald-300 bg-emerald-50 px-2.5 py-1 text-[12px] font-semibold text-emerald-700 hover:bg-emerald-100"
              title="Download a richly-formatted Excel workbook (one sheet per table, colour-scaled scores, live RPN formulas)"
            >
              ⬇ Excel
            </a>
            <a
              href={fmeaApi.exportUrl(fmeaId)}
              onClick={() => { setExportNote("CSV downloading…"); setTimeout(() => setExportNote(""), 2800); }}
              className="rounded-lg border border-gray-300 px-2.5 py-1 text-[12px] font-medium text-gray-600 hover:bg-gray-50"
              title="Export as CSV"
            >
              ⬇ CSV
            </a>
            {generating ? (
              <button onClick={cancelGen} className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-1 text-[12px] font-medium text-red-600 hover:bg-red-100">Cancel</button>
            ) : (
              <button
                onClick={() => void generate(true)}
                disabled={!hasMemory}
                title={hasMemory ? "Regenerate the whole FMEA from the architecture Memory" : "The source architecture has no Memory to transform"}
                className="rounded-lg bg-brand px-3 py-1 text-[12px] font-semibold text-white hover:bg-brand-dark disabled:opacity-50"
              >
                {isEmpty ? "✨ Generate with AI" : "✨ Regenerate all"}
              </button>
            )}
            <button onClick={() => void save()} disabled={saving || readOnly} className="rounded-lg border border-gray-300 px-2.5 py-1 text-[12px] font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">Save</button>
          </div>
        </div>
        {(generating || regenTable) && (
          <div className="mt-2 overflow-hidden rounded-lg border border-violet-200 bg-violet-50/70">
            <div className="flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium text-violet-800">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-violet-300 border-t-violet-600" />
              <span className="flex-1 truncate">{genStatus || "Working…"}</span>
              <span className="shrink-0 tabular-nums text-[11px] text-violet-500">{fmtElapsed(genElapsed)}</span>
            </div>
            {genLog.length > 0 && (
              <div className="max-h-40 overflow-y-auto border-t border-violet-100 bg-white/60 px-3 py-1.5 font-mono text-[11px] leading-relaxed text-gray-600">
                {genLog.map((l, i) => (
                  <div key={i} className="flex gap-2">
                    <span className="shrink-0 text-gray-400">{l.t}</span>
                    <span className="min-w-0 flex-1 break-words">{l.msg}</span>
                  </div>
                ))}
                <div ref={logEndRef} />
              </div>
            )}
          </div>
        )}
        {!hasMemory && (
          <div className="mt-2 rounded-lg bg-amber-50 px-3 py-1.5 text-[12px] text-amber-700">
            The source architecture has no Memory yet. Generate its Memory first to enable AI generation.
          </div>
        )}
        {error && <div className="mt-2 rounded-lg bg-red-50 px-3 py-1.5 text-[12px] text-red-600">{error}</div>}
        {exportNote && <div className="mt-2 rounded-lg bg-emerald-50 px-3 py-1.5 text-[12px] text-emerald-700">✓ {exportNote}</div>}
      </div>

      {/* tables */}
      <div className="flex-1 space-y-4 overflow-y-auto p-5">
        {isEmpty && !generating && (
          <div className="rounded-xl border border-dashed bg-white p-10 text-center">
            <div className="text-sm font-medium text-gray-700">No tables yet</div>
            <p className="mx-auto mt-1 max-w-md text-xs text-gray-500">
              Generate the analysis with AI from the architecture's Memory, or add a table manually.
            </p>
            <div className="mt-3 flex items-center justify-center gap-2">
              <button onClick={() => void generate(true)} disabled={!hasMemory} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-50">✨ Generate with AI</button>
              <button onClick={addTable} className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">+ Add table</button>
            </div>
          </div>
        )}
        {doc.tables.map((t) => (
          <FmeaTableGrid
            key={t.id}
            table={t}
            onChange={setTable}
            onRemove={() => removeTable(t.id)}
            onRegen={() => void regenerateTable(t.id)}
            regenning={regenTable === t.id}
            readOnly={readOnly}
          />
        ))}
        {!isEmpty && (
          <button onClick={addTable} className="rounded-lg border border-dashed border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-500 hover:bg-gray-50">+ Add table</button>
        )}
      </div>
    </div>
  );
}

// ============================================================================ index
function FmeaTrashPanel({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ["fmeaTrash"], queryFn: fmeaApi.trash });
  const items = query.data?.items ?? [];
  async function restore(id: string) {
    await fmeaApi.restore(id);
    await Promise.all([qc.invalidateQueries({ queryKey: ["fmeaTrash"] }), qc.invalidateQueries({ queryKey: ["fmeaIndex"] })]);
  }
  async function purge(id: string) {
    if (!window.confirm("Permanently delete this FMEA? This cannot be undone.")) return;
    await fmeaApi.purge(id);
    await qc.invalidateQueries({ queryKey: ["fmeaTrash"] });
  }
  async function emptyAll() {
    if (!window.confirm("Permanently delete ALL items in the Trash?")) return;
    await fmeaApi.emptyTrash();
    await Promise.all([qc.invalidateQueries({ queryKey: ["fmeaTrash"] }), qc.invalidateQueries({ queryKey: ["fmeaIndex"] })]);
  }
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div className="max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-xl bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-800">🗑️ FMEA Trash</h2>
          <div className="flex items-center gap-2">
            {items.length > 0 && <button onClick={() => void emptyAll()} className="rounded-lg border border-red-200 px-2.5 py-1 text-[12px] text-red-600 hover:bg-red-50">Empty trash</button>}
            <button onClick={onClose} className="rounded-lg border px-2.5 py-1 text-[12px] text-gray-500 hover:bg-gray-50">Close</button>
          </div>
        </div>
        <div className="max-h-[60vh] divide-y overflow-y-auto">
          {items.length === 0 && <div className="px-4 py-8 text-center text-sm text-gray-400">Trash is empty.</div>}
          {items.map((d) => (
            <div key={d.id} className="flex items-center gap-3 px-4 py-2.5">
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-gray-700">{d.title || "FMEA"}</span>
                <span className="block truncate text-[11px] text-gray-400">{d.workload_name} · deleted {d.deleted_at ? new Date(d.deleted_at).toLocaleString() : "—"}</span>
              </span>
              <button onClick={() => void restore(d.id)} className="rounded-lg border px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50">Restore</button>
              <button onClick={() => void purge(d.id)} className="rounded-lg border border-red-200 px-2 py-1 text-[11px] text-red-600 hover:bg-red-50">Delete</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ============================================================================ new-FMEA picker
function NewFmeaModal({
  buildable, onClose, onPick, creating,
}: {
  buildable: FmeaBuildable[];
  onClose: () => void;
  onPick: (architectureId: string) => void;
  creating: string;
}) {
  const [q, setQ] = useState("");
  const term = q.trim().toLowerCase();
  const list = useMemo(() => {
    const rows = [...buildable].sort((a, b) =>
      (a.workload_name || a.architecture_name).localeCompare(b.workload_name || b.architecture_name));
    if (!term) return rows;
    return rows.filter((b) =>
      (b.workload_name || "").toLowerCase().includes(term) || (b.architecture_name || "").toLowerCase().includes(term));
  }, [buildable, term]);

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden rounded-xl bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="border-b px-4 py-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-800">✨ New FMEA</h2>
            <button onClick={onClose} className="rounded-lg border px-2.5 py-1 text-[12px] text-gray-500 hover:bg-gray-50">Close</button>
          </div>
          <p className="mt-1 text-[12px] text-gray-500">Pick a workload to analyse. Its FMEA is built from the workload's Architecture Memory.</p>
        </div>
        {buildable.length === 0 ? (
          <div className="px-4 py-8 text-center text-[12px] text-gray-500">
            None of your architectures have a Memory yet. Open <b>Architectures</b>, generate a Memory for a workload, then return here.
          </div>
        ) : (
          <>
            <div className="border-b px-4 py-2">
              <input
                autoFocus
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search workloads…"
                aria-label="Search workloads"
                className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
              />
            </div>
            <div className="min-h-0 flex-1 divide-y overflow-y-auto">
              {list.length === 0 && <div className="px-4 py-8 text-center text-[12px] text-gray-400">No workloads match "{q}".</div>}
              {list.map((b) => (
                <button
                  key={b.architecture_id}
                  onClick={() => onPick(b.architecture_id)}
                  disabled={!!creating}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition hover:bg-gray-50 disabled:opacity-50"
                >
                  <span className="text-lg leading-none">🧩</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-gray-800">{b.workload_name || b.architecture_name}</span>
                    <span className="block truncate text-[11px] text-gray-400">
                      {b.architecture_name}{b.fmea_count > 0 ? ` · ${b.fmea_count} existing FMEA${b.fmea_count === 1 ? "" : "s"}` : ""}
                    </span>
                  </span>
                  <span className="shrink-0 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand">
                    {creating === b.architecture_id ? "Creating…" : "Start →"}
                  </span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function FmeaIndex() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [q, setQ] = usePersistedState<string>("azsup.fmea.search", "");
  const term = useDebounced(q, 150).trim().toLowerCase();
  const query = useQuery({ queryKey: ["fmeaIndex"], queryFn: fmeaApi.index, staleTime: 5 * 60 * 1000 });
  const [showTrash, setShowTrash] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [creating, setCreating] = useState("");
  // Persist the index status filter so the chosen view survives navigation/reload.
  const [statusFilter, setStatusFilter] = usePersistedState<"all" | "draft" | "in_review" | "published" | "archived">("azsup.fmea.statusFilter", "all");

  const documents: FmeaDocumentSummary[] = query.data?.documents ?? [];
  const buildable: FmeaBuildable[] = query.data?.buildable ?? [];
  const trashCount = query.data?.trash_count ?? 0;

  const groups = useMemo(() => {
    const byArch = new Map<string, { architecture_id: string; architecture_name: string; workload_name: string; architecture_exists: boolean; workload_exists: boolean; docs: FmeaDocumentSummary[] }>();
    for (const b of buildable) {
      byArch.set(b.architecture_id, {
        architecture_id: b.architecture_id, architecture_name: b.architecture_name,
        workload_name: b.workload_name, architecture_exists: b.architecture_exists, workload_exists: b.workload_exists !== false, docs: [],
      });
    }
    for (const d of documents) {
      if (statusFilter !== "all" && d.status !== statusFilter) continue;
      let g = byArch.get(d.architecture_id);
      if (!g) {
        g = { architecture_id: d.architecture_id, architecture_name: d.architecture_name, workload_name: d.workload_name, architecture_exists: d.architecture_exists, workload_exists: d.workload_exists !== false, docs: [] };
        byArch.set(d.architecture_id, g);
      }
      g.docs.push(d);
    }
    let list = [...byArch.values()];
    const rank = (d: FmeaDocumentSummary) => (d.status === "published" ? 0 : d.status === "in_review" ? 1 : d.status === "draft" ? 2 : 3);
    for (const g of list) g.docs.sort((a, b) => rank(a) - rank(b) || (b.updated_at || "").localeCompare(a.updated_at || ""));
    if (statusFilter !== "all") list = list.filter((g) => g.docs.length > 0);
    if (term) {
      list = list.filter((g) => g.workload_name.toLowerCase().includes(term) || g.architecture_name.toLowerCase().includes(term) || g.docs.some((d) => (d.title || "").toLowerCase().includes(term)));
    }
    list.sort((a, b) => (a.workload_name || a.architecture_name).localeCompare(b.workload_name || b.architecture_name));
    return list;
  }, [documents, buildable, term, statusFilter]);

  async function createNew(architectureId: string) {
    setCreating(architectureId);
    try {
      const r = await fmeaApi.create(architectureId);
      await qc.invalidateQueries({ queryKey: ["fmeaIndex"] });
      if (r.id) navigate(`/fmea/${r.id}`);
    } finally { setCreating(""); }
  }
  async function softDelete(id: string) {
    if (!window.confirm("Move this FMEA to the Trash? You can restore it later.")) return;
    await fmeaApi.remove(id);
    await qc.invalidateQueries({ queryKey: ["fmeaIndex"] });
  }

  const totalDocs = documents.length;

  return (
    <div className="h-full overflow-y-auto bg-gray-50/40">
      {showTrash && <FmeaTrashPanel onClose={() => setShowTrash(false)} />}
      {showNew && (
        <NewFmeaModal
          buildable={buildable}
          onClose={() => setShowNew(false)}
          onPick={(architectureId) => { setShowNew(false); void createNew(architectureId); }}
          creating={creating}
        />
      )}
      <div className="space-y-4 p-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold text-gray-900">🧪 FMEA</h1>
            <p className="mt-0.5 max-w-3xl text-sm text-gray-500">
              Failure Mode and Effects Analysis — multiple scored tables per workload, generated by AI from the Architecture Memory. Each failure mode is rated on Severity, Occurrence and Detection; the Risk Priority Number is computed automatically.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search workloads…"
              aria-label="Search FMEA documents"
              className="w-56 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
            />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
              aria-label="Filter by status"
              className="shrink-0 rounded-lg border border-gray-300 px-2.5 py-2 text-sm text-gray-600 focus:border-brand-dark focus:outline-none"
            >
              <option value="all">All statuses</option>
              <option value="published">Published</option>
              <option value="in_review">In review</option>
              <option value="draft">Draft</option>
              <option value="archived">Archived</option>
            </select>
            <button
              onClick={() => setShowTrash(true)}
              aria-label="View deleted FMEA documents"
              title="View deleted FMEA documents"
              className="relative shrink-0 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50"
            >
              🗑️ Trash{trashCount > 0 ? ` (${trashCount})` : ""}
            </button>
            <button
              onClick={() => setShowNew(true)}
              title="Start a new FMEA for any workload that has an Architecture Memory"
              className="shrink-0 rounded-lg bg-brand px-3 py-2 text-sm font-semibold text-white hover:bg-brand-dark"
            >
              ✨ New FMEA
            </button>
          </div>
        </div>

        {query.isLoading && <div className="py-10 text-center text-sm text-gray-400">Loading…</div>}
        {!query.isLoading && groups.length === 0 && !term && (
          <div className="rounded-xl border bg-white p-8 text-center">
            <div className="text-2xl">🧪</div>
            <div className="mt-1 text-sm font-medium text-gray-700">No FMEA documents yet</div>
            {buildable.length > 0 ? (
              <>
                <p className="mx-auto mt-1 max-w-md text-xs text-gray-500">
                  Start a Failure Mode and Effects Analysis for one of your {buildable.length} workload{buildable.length === 1 ? "" : "s"} that has an Architecture Memory, then generate the scored risk tables with AI.
                </p>
                <button onClick={() => setShowNew(true)} className="mt-3 rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark">✨ New FMEA</button>
              </>
            ) : (
              <p className="mx-auto mt-1 max-w-md text-xs text-gray-500">
                FMEA is generated from a workload's <b>Architecture Memory</b>. None of your architectures have a Memory yet — open <b>Architectures</b>, generate a Memory for a workload, then come back here to run its FMEA.
              </p>
            )}
          </div>
        )}
        {!query.isLoading && groups.length === 0 && term && (
          <div className="rounded-xl border border-dashed bg-white p-10 text-center text-sm text-gray-400">No workloads match "{q}".</div>
        )}

        {groups.length > 0 && (
          <>
            <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
              {totalDocs} document{totalDocs === 1 ? "" : "s"} · {groups.length} workload{groups.length === 1 ? "" : "s"}
            </div>
            <div className="space-y-4">
              {groups.map((g) => (
                <div key={g.architecture_id} className="rounded-xl border border-gray-200 bg-white">
                  <div className="flex items-center gap-2 border-b px-4 py-2.5">
                    <span className="text-base">🧩</span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-sm font-semibold text-gray-800">{g.workload_name || g.architecture_name}</span>
                        {!g.architecture_exists && <span className="shrink-0 rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] text-red-600">orphaned</span>}
                        {g.architecture_exists && !g.workload_exists && <span title="The workload this architecture was built from has been deleted" className="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">workload deleted</span>}
                      </span>
                      <span className="block truncate text-[11px] text-gray-400">{g.architecture_name}</span>
                    </span>
                    <button
                      onClick={() => void createNew(g.architecture_id)}
                      disabled={creating === g.architecture_id}
                      title="Create a new FMEA for this workload"
                      className="shrink-0 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
                    >
                      {creating === g.architecture_id ? "Creating…" : "+ New"}
                    </button>
                  </div>
                  {g.docs.length === 0 ? (
                    <div className="px-4 py-3 text-[12px] text-gray-400">No FMEA yet — click <b>+ New</b> to start one.</div>
                  ) : (
                    <div className="divide-y">
                      {g.docs.map((d) => {
                        const dsm = STATUS_META[d.status] ?? STATUS_META.draft;
                        const crit = d.counts?.critical ?? 0;
                        const high = d.counts?.high ?? 0;
                        return (
                          <div key={d.id} className="group flex items-center gap-3 px-4 py-2.5 transition hover:bg-gray-50/60">
                            <button onClick={() => navigate(`/fmea/${d.id}`)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
                              <span className="text-lg leading-none">🧪</span>
                              <span className="min-w-0 flex-1">
                                <span className="flex items-center gap-2">
                                  <span className="truncate text-sm font-medium text-gray-800">{d.title || `FMEA — ${d.workload_name || g.workload_name || "draft"}`}</span>
                                  <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${dsm.cls}`}>{dsm.label}</span>
                                  {d.source && <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${SOURCE_BADGE[d.source] ?? "bg-gray-100 text-gray-600"}`}>{d.source}</span>}
                                </span>
                                <span className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
                                  <span>{d.table_count} table{d.table_count === 1 ? "" : "s"} · {d.row_count} row{d.row_count === 1 ? "" : "s"}</span>
                                  {crit > 0 && <span className="rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">{crit} critical</span>}
                                  {high > 0 && <span className="rounded-full bg-orange-100 px-1.5 py-0.5 text-[10px] font-medium text-orange-700">{high} high</span>}
                                  {d.top_rpn > 0 && <span>top RPN {d.top_rpn}</span>}
                                  <span>· updated {d.updated_at ? new Date(d.updated_at).toLocaleDateString() : "—"}</span>
                                </span>
                              </span>
                            </button>
                            <button
                              onClick={() => void softDelete(d.id)}
                              aria-label="Move to Trash"
                              title="Move to Trash"
                              className="shrink-0 rounded-lg border px-2 py-1 text-[11px] text-gray-400 opacity-0 transition hover:bg-red-50 hover:text-red-600 group-hover:opacity-100"
                            >
                              🗑️
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** Route dispatcher for the top-level /fmea feature: /fmea → index, /fmea/:id → view. */
export function FmeaPanel() {
  const location = useLocation();
  const segs = location.pathname.split("/").filter(Boolean); // ["fmea", :id?]
  const id = segs[1];
  if (id) return <FmeaView fmeaId={id} />;
  return <FmeaIndex />;
}
