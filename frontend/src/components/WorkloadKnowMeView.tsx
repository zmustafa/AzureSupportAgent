/**
 * Workload Know-Me — a support-facing reference transformed from an architecture's Memory.
 *
 * Three modes: Read (clean doc, filled values inline), Guided fill (a focused field
 * walker), and Edit (per-section TipTap WYSIWYG + diagrams/images). Plus AI generation
 * (two-pass, live progress), per-section regenerate, revision history, validation gate and
 * export (Markdown / PDF). A workload can have MANY Know-Me documents (drafts + a published
 * reference); each is keyed by its own ``km_id`` and supports soft-delete to a Trash.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  streamRegenerateKnowMe,
  streamBuildKnowMeFromWorkload,
  streamRegenerateKnowMeSection,
  type KnowMe,
  type KnowMeResponse,
  type KnowMeSection,
  type KnowMeTodo,
  type KnowMeDocument,
  type KnowMeBuildable,
} from "../api";
import { Markdown } from "./LazyMarkdown";
import { MermaidDiagram } from "./MermaidDiagram";
import { formatError } from "../utils/format";
import { GuidedFill } from "./knowme/GuidedFill";
import { SectionEditor } from "./knowme/SectionEditor";
import { FieldInput } from "./knowme/FieldInput";
import { fieldProgress, renderSectionRead, isPlaceholderMermaid, parseFieldToken, validateField, FIELD_META } from "./knowme/fields";

const PROSE =
  "prose prose-sm max-w-none [&_h1]:text-2xl [&_h1]:font-bold [&_h2]:mb-1 [&_h2]:mt-5 [&_h2]:text-lg [&_h2]:font-bold [&_h2]:text-gray-900 [&_h3]:mt-3 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-gray-800 [&_img]:max-w-full [&_img]:rounded-lg [&_table]:text-[12px]";

const STATUS_META: Record<string, { label: string; cls: string }> = {
  draft: { label: "Draft", cls: "bg-gray-100 text-gray-600" },
  in_review: { label: "In review", cls: "bg-amber-100 text-amber-700" },
  published: { label: "Published", cls: "bg-green-100 text-green-700" },
  archived: { label: "Archived", cls: "bg-gray-200 text-gray-500" },
};

// The lifecycle transitions offered in the status dropdown.
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "draft", label: "Draft" },
  { value: "in_review", label: "In review" },
  { value: "published", label: "Published" },
  { value: "archived", label: "Archived" },
];

function fmtElapsed(s: number): string {
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

/** A compact line-level diff (LCS) of two markdown documents → rows tagged add/del/same.
 *  Used to show what a past revision changed versus the current version. */
type DiffRow = { type: "same" | "add" | "del"; text: string };
function lineDiff(oldText: string, newText: string): DiffRow[] {
  const a = (oldText || "").split("\n");
  const b = (newText || "").split("\n");
  const n = a.length, m = b.length;
  // LCS length table.
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const rows: DiffRow[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ type: "same", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ type: "del", text: a[i] }); i++; }
    else { rows.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < n) rows.push({ type: "del", text: a[i++] });
  while (j < m) rows.push({ type: "add", text: b[j++] });
  return rows;
}

/** Inline pick-or-type popover anchored to a clicked field chip. Shows the field's choices /
 *  suggestions (segmented buttons, a combobox, or suggestion pills via FieldInput) plus a
 *  free-text box — the user can pick an option OR type their own value, then Save. No
 *  right-hand guided-fill panel involved. Closes on outside-click / Esc / scroll. */
function FieldPopover({
  todo,
  anchor,
  saving,
  onSave,
  onClose,
  onSuggest,
  suggesting,
}: {
  todo: KnowMeTodo;
  anchor: DOMRect;
  saving: boolean;
  onSave: (value: string) => void;
  onClose: () => void;
  onSuggest: () => void;
  suggesting: boolean;
}) {
  const [draft, setDraft] = useState(todo.value || "");
  const ref = useRef<HTMLDivElement>(null);
  // Position: prefer below the chip; flip above if it would overflow the viewport bottom.
  const WIDTH = 340;
  const vw = typeof window !== "undefined" ? window.innerWidth : 1280;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  const left = Math.max(8, Math.min(anchor.left, vw - WIDTH - 8));
  const below = anchor.bottom + 8;
  const placeAbove = below + 220 > vh && anchor.top > vh - anchor.bottom;
  const style: React.CSSProperties = placeAbove
    ? { position: "fixed", left, bottom: vh - anchor.top + 8, width: WIDTH, zIndex: 50 }
    : { position: "fixed", left, top: below, width: WIDTH, zIndex: 50 };

  useEffect(() => {
    // Outside-click close. Runs in the CAPTURE phase: a combobox option unmounts itself
    // synchronously on mousedown (React 18 flushes discrete events), so by the bubble phase
    // the clicked node is detached and ``contains`` would wrongly report "outside" — closing
    // the popover and discarding the pick. Capturing fires before that unmount, while the
    // option is still in the DOM and correctly inside the popover.
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current && t && ref.current.contains(t)) return; // click inside → keep open
      onClose();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    // A scroll in the document moves the anchor out from under us → just close.
    const onScroll = () => onClose();
    window.addEventListener("mousedown", onDown, true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose]);

  const error = validateField(todo, draft);
  const meta = FIELD_META[todo.type];

  return (
    <div ref={ref} style={style} className="rounded-xl border border-gray-200 bg-white p-3 shadow-2xl">
      <div className="mb-1.5 flex items-start gap-2">
        <span className="min-w-0 flex-1 text-[13px] font-semibold text-gray-800">{todo.label}</span>
        {todo.required && <span className="shrink-0 rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">required</span>}
        <button onClick={onClose} aria-label="Close" className="shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100">✕</button>
      </div>
      {meta?.label && <div className="mb-1.5 text-[11px] text-gray-400">{meta.label}{meta.placeholder ? ` · e.g. ${meta.placeholder}` : ""}</div>}
      <FieldInput
        todo={todo}
        value={draft}
        onChange={setDraft}
        onEnter={() => { if (!error) onSave(draft); }}
        autoFocus
        onSuggest={onSuggest}
        suggesting={suggesting}
      />
      <div className="mt-2.5 flex items-center gap-2">
        <button
          onClick={() => onSave(draft)}
          disabled={saving || !!error}
          className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-dark disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {todo.value && (
          <button
            onClick={() => onSave("")}
            disabled={saving}
            className="rounded-lg border px-2.5 py-1.5 text-xs text-gray-500 hover:bg-gray-50 disabled:opacity-50"
            title="Clear this field"
          >
            Clear
          </button>
        )}
        <button onClick={onClose} className="ml-auto rounded-lg border px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50">Cancel</button>
      </div>
    </div>
  );
}

/** Rewrite ``asset:<id>`` image refs to the live asset API URL so images render in the doc.
 *  Assets are keyed by the Know-Me's own id. */
function withAssetUrls(kmId: string, md: string): string {
  return (md || "").replace(/asset:([0-9a-fA-F-]{36})/g, (_m, id) => api.knowMeAssetUrl(kmId, id));
}

export function KnowMeView({ kmId }: { kmId: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const kmQ = useQuery({ queryKey: ["knowMe", kmId], queryFn: () => api.knowMe(kmId) });

  const km: KnowMe | null = kmQ.data?.know_me ?? null;
  const hasMemory = kmQ.data?.has_memory ?? false;
  const architectureId = km?.architecture_id ?? kmQ.data?.architecture?.id ?? "";
  const workloadName = kmQ.data?.architecture?.workload_name ?? km?.workload_name ?? "";
  const workloadId = kmQ.data?.architecture?.workload_id ?? km?.workload_id ?? "";

  const [mode, setMode] = useState<"read" | "fill" | "edit">("read");
  // When set, guided fill is scoped to a single section's fields (per-section ✍️ button).
  const [fillScope, setFillScope] = useState<string>("");
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  // --- generation (SSE) ---
  const [genState, setGenState] = useState<"idle" | "running">("idle");
  const [genSteps, setGenSteps] = useState<{ message: string; at: number }[]>([]);
  const [genElapsed, setGenElapsed] = useState(0);
  const genStart = useRef(0);
  const genTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const genAbort = useRef<AbortController | null>(null);
  const stepsRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = stepsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [genSteps.length]);
  useEffect(() => () => { if (genTimer.current) clearInterval(genTimer.current); genAbort.current?.abort(); }, []);

  function stopTimer() {
    if (genTimer.current) { clearInterval(genTimer.current); genTimer.current = null; }
  }
  function startGen() {
    setErr("");
    setGenState("running");
    setGenSteps([{ message: "Starting…", at: 0 }]);
    genStart.current = Date.now();
    setGenElapsed(0);
    stopTimer();
    genTimer.current = setInterval(() => setGenElapsed(Math.floor((Date.now() - genStart.current) / 1000)), 1000);
    const ctrl = new AbortController();
    genAbort.current = ctrl;
    return ctrl;
  }
  const onGenStatus = (s: { message: string }) =>
    setGenSteps((prev) => [...prev, { message: s.message, at: Math.floor((Date.now() - genStart.current) / 1000) }]);
  const onGenDone = (r: KnowMeResponse) => {
    qc.setQueryData(["knowMe", kmId], r);
    void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
    void qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
  };

  async function generate() {
    if (genState === "running") return;
    const ctrl = startGen();
    await streamRegenerateKnowMe(kmId, { onStatus: onGenStatus, onDone: onGenDone, onError: setErr }, ctrl.signal);
    stopTimer();
    setGenState("idle");
  }
  async function buildFromWorkload() {
    if (genState === "running" || !workloadId) return;
    const ctrl = startGen();
    await streamBuildKnowMeFromWorkload(
      { workload_id: workloadId, architecture_id: architectureId },
      { onStatus: onGenStatus, onDone: (r) => { if (r.id && r.id !== kmId) navigate(`/knowme/${r.id}`); else onGenDone(r); }, onError: setErr },
      ctrl.signal,
    );
    stopTimer();
    setGenState("idle");
  }
  function cancelGenerate() {
    genAbort.current?.abort();
    stopTimer();
    setGenState("idle");
  }

  // --- todos / fields ---
  const todos: KnowMeTodo[] = km?.todos ?? [];
  const sections: KnowMeSection[] = km?.sections ?? [];
  const sectionKeys = useMemo(() => sections.map((s) => s.key), [sections]);
  const prog = useMemo(() => fieldProgress(todos), [todos]);

  async function saveTodos(next: KnowMeTodo[], nextStatus?: string) {
    setSaving(true);
    setErr("");
    try {
      const r = await api.saveKnowMe(kmId, { todos: next, status: nextStatus });
      qc.setQueryData(["knowMe", kmId], r);
      void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
      void qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } catch (e) { setErr(formatError(e)); } finally { setSaving(false); }
  }

  // Save ONE field's value inline (from the pick-or-type popover). A non-empty value marks it
  // done; clearing it reopens the field.
  async function saveField(fieldId: string, value: string) {
    const v = value.trim();
    const next = todos.map((t) =>
      t.id === fieldId
        ? { ...t, value: v, status: (v ? "done" : "open") as "open" | "done", source: (v && t.source === "human" ? "human" : t.source) }
        : t,
    );
    await saveTodos(next);
  }

  // --- document metadata (title / description / status / reference) ---
  async function saveMeta(patch: { title?: string; description?: string; status?: string }) {
    setSaving(true);
    setErr("");
    try {
      const r = await api.saveKnowMe(kmId, patch);
      qc.setQueryData(["knowMe", kmId], r);
      void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
      void qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } catch (e) { setErr(formatError(e)); } finally { setSaving(false); }
  }
  async function toggleReference() {
    setErr("");
    try {
      const r = await api.setKnowMeReference(kmId, !(km?.is_reference));
      qc.setQueryData(["knowMe", kmId], r);
      void qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } catch (e) { setErr(formatError(e)); }
  }
  // On-demand AI "suggest options" for a single field (P3).
  const [suggestingId, setSuggestingId] = useState("");
  async function suggestField(fieldId: string) {
    if (suggestingId) return;
    setSuggestingId(fieldId);
    setErr("");
    try {
      const r = await api.suggestKnowMeField(kmId, fieldId);
      // Refetch the doc so the field's new choices flow into the walker.
      if (r.choices?.length) await qc.invalidateQueries({ queryKey: ["knowMe", kmId] });
    } catch (e) { setErr(formatError(e)); } finally { setSuggestingId(""); }
  }
  // Inline title / description editing.
  const [editingMeta, setEditingMeta] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftDesc, setDraftDesc] = useState("");
  function beginEditMeta() {
    setDraftTitle(km?.title || "");
    setDraftDesc(km?.description || "");
    setEditingMeta(true);
  }
  async function commitMeta() {
    setEditingMeta(false);
    const title = draftTitle.trim();
    const description = draftDesc.trim();
    if (title === (km?.title || "") && description === (km?.description || "")) return;
    await saveMeta({ title, description });
  }

  // --- per-section edit + regenerate ---
  const [editSection, setEditSection] = useState<KnowMeSection | null>(null);
  const [regenKey, setRegenKey] = useState<string>("");
  // Live status messages for an in-progress section regenerate, shown in a popup anchored
  // to the section's ✨ button.
  const [regenSteps, setRegenSteps] = useState<{ message: string; at: number }[]>([]);
  const regenStart = useRef(0);
  const regenAbort = useRef<AbortController | null>(null);
  useEffect(() => () => regenAbort.current?.abort(), []);
  async function saveSection(sectionKey: string, content: string) {
    const next = sections.map((s) => (s.key === sectionKey ? { ...s, content } : s));
    const r = await api.saveKnowMe(kmId, { sections: next });
    qc.setQueryData(["knowMe", kmId], r);
    void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
  }
  async function regenSection(sectionKey: string) {
    if (regenKey) return;
    setRegenKey(sectionKey);
    setErr("");
    regenStart.current = Date.now();
    setRegenSteps([{ message: "Starting…", at: 0 }]);
    const ctrl = new AbortController();
    regenAbort.current = ctrl;
    await streamRegenerateKnowMeSection(
      kmId,
      sectionKey,
      {
        onStatus: (s) => setRegenSteps((prev) => [...prev, { message: s.message, at: Math.floor((Date.now() - regenStart.current) / 1000) }]),
        onDone: (r) => {
          qc.setQueryData(["knowMe", kmId], r);
          void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
        },
        onError: (m) => setErr(m),
      },
      ctrl.signal,
    );
    setRegenKey("");
    setRegenSteps([]);
  }
  function cancelRegen() {
    regenAbort.current?.abort();
    setRegenKey("");
    setRegenSteps([]);
  }

  // --- scroll-to-field for guided fill ---
  const docRef = useRef<HTMLDivElement>(null);
  const [highlightId, setHighlightId] = useState<string>("");
  // A click on an inline field chip opens a pick-or-type popover anchored to it.
  // Clicking a field chip opens an inline pick-or-type popover anchored to it (NOT the
  // right-hand guided-fill panel). ``anchor`` is the chip's viewport rect for positioning.
  const [picker, setPicker] = useState<{ id: string; anchor: DOMRect } | null>(null);
  const openPicker = useCallback((tid: string, anchor: DOMRect) => {
    setHighlightId(tid);
    setPicker({ id: tid, anchor });
  }, []);
  const closePicker = useCallback(() => { setPicker(null); setHighlightId(""); }, []);
  function scrollToSection(sectionKey: string, todoId: string) {
    setHighlightId(todoId);
    // After the re-render paints the red marker, scroll the marker itself into view
    // (falls back to the section). Two frames so the Markdown re-render has committed.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        const marker = docRef.current?.querySelector(".km-active-field") as HTMLElement | null;
        const section = docRef.current?.querySelector(`[data-kmsec="${sectionKey}"]`) as HTMLElement | null;
        (marker ?? section)?.scrollIntoView({ behavior: "smooth", block: "center" });
      }),
    );
  }

  // Custom Markdown ``code`` renderer for the document: (1) renders ```mermaid fenced
  // blocks as live diagrams, and (2) turns each OPEN ⟦TODO⟧ field into a clickable, bordered
  // field chip — dashed/amber when empty, red while its pick-or-type popover is open.
  const docMarkdownComponents = useMemo(() => {
    return {
      code({ className, children, ...props }: { className?: string; children?: React.ReactNode }) {
        const text = String(Array.isArray(children) ? children.join("") : (children ?? ""));
        // Fenced ```mermaid → live diagram (skip trivial all-generic placeholder diagrams).
        if (/\blanguage-mermaid\b/.test(className || "") && text.trim()) {
          if (isPlaceholderMermaid(text)) return null;
          return <MermaidDiagram code={text.replace(/\n$/, "")} />;
        }
        // Fillable field chip: bordered control. Click to pick or type inline. A filled
        // field stays a chip (shows its value + a red ● marker, still editable); an open one
        // shows the label with a dashed/amber style; the active (popover-open) chip is red.
        const field = parseFieldToken(text);
        if (field) {
          const active = field.tid === highlightId;
          const filled = !!field.value;
          return (
            <button
              type="button"
              data-kmfield={field.tid}
              onClick={(e) => { e.preventDefault(); openPicker(field.tid, (e.currentTarget as HTMLElement).getBoundingClientRect()); }}
              title={active ? `Editing: ${field.label}` : filled ? `${field.label}: ${field.value} — click to edit` : `Click to pick or type: ${field.label}`}
              aria-label={filled ? `Edit field ${field.label}, current value ${field.value}` : `Fill field: ${field.label}`}
              className={
                "km-field mx-0.5 inline-flex items-center gap-1 rounded-md border-2 px-1.5 py-0.5 align-baseline text-[12px] font-medium transition focus:outline-none focus:border-red-400 focus:bg-red-50 focus:text-red-700 focus:ring-2 focus:ring-red-300 " +
                (active
                  ? "km-active-field border-red-400 bg-red-50 text-red-700 ring-2 ring-red-300"
                  : filled
                    ? "border-gray-300 bg-white text-gray-800 hover:border-red-300 hover:bg-red-50"
                    : "border-dashed border-amber-400 bg-amber-50 text-amber-700 hover:border-amber-500 hover:bg-amber-100")
              }
            >
              {/* A red ● marks an editable field (always shown so a filled value stays a field). */}
              <span aria-hidden="true" className={active ? "text-red-500" : filled ? "text-red-400" : "text-amber-500"} style={{ fontSize: "8px", lineHeight: 1 }}>●</span>
              {filled ? field.value : field.label}
            </button>
          );
        }
        return <code className={className} {...props}>{children}</code>;
      },
      // Honor the ``?w=<px>`` width hint a resized image carries in its src (display from the
      // width-less URL so the browser caches one image; apply the width via style).
      img({ src, alt, ...props }: { src?: string; alt?: string }) {
        const m = /[?&]w=(\d+)/.exec(src || "");
        const width = m ? `${m[1]}px` : undefined;
        const cleanSrc = (src || "").replace(/[?&]w=\d+/g, "").replace(/\?$/, "");
        return <img src={cleanSrc} alt={alt || ""} style={width ? { width, maxWidth: "100%" } : undefined} {...props} />;
      },
    };
  }, [highlightId, openPicker]);

  // --- copy / export ---
  const [copied, setCopied] = useState(false);
  function copyMarkdown() {
    void navigator.clipboard.writeText(kmQ.data?.markdown ?? "");
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  // --- history / revision viewing ---
  // Revisions open in the MAIN document area (read-only) with a banner — not a small pane.
  // viewingRevId === null means the live/current version. The current version is also shown
  // as the first entry in the list so the user can navigate back to it.
  const [showHistory, setShowHistory] = useState(false);
  // Only one toolbar popup (How built / Source) may be open at a time so they never overlap.
  const [openMenu, setOpenMenu] = useState<"" | "evidence" | "links">("");
  const showEvidence = openMenu === "evidence";
  const showLinks = openMenu === "links";
  const toolbarRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!openMenu) return;
    const onDown = (e: MouseEvent) => {
      if (toolbarRef.current && !toolbarRef.current.contains(e.target as Node)) setOpenMenu("");
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [openMenu]);
  const [viewingRevId, setViewingRevId] = useState<string | null>(null);
  const revQ = useQuery({
    queryKey: ["knowMeRevisions", kmId],
    queryFn: () => api.knowMeRevisions(kmId),
    enabled: showHistory && !!km,
  });
  const revContentQ = useQuery({
    queryKey: ["knowMeRevision", kmId, viewingRevId],
    queryFn: () => api.knowMeRevision(kmId, viewingRevId!),
    enabled: !!viewingRevId,
  });
  const viewingRev = useMemo(
    () => (revQ.data?.revisions ?? []).find((r) => r.id === viewingRevId) ?? null,
    [revQ.data, viewingRevId],
  );
  function closeHistory() {
    setShowHistory(false);
    setViewingRevId(null);
  }
  function viewRevision(id: string | null) {
    setViewingRevId(id);
    if (id) setMode("read"); // a revision is read-only — drop out of fill/edit
  }
  async function restoreRev(id: string) {
    if (!window.confirm("Restore this version? It becomes the new current version (saved as a new revision), so nothing is lost — the version you're replacing stays in history.")) return;
    try {
      const r = await api.restoreKnowMeRevision(kmId, id);
      qc.setQueryData(["knowMe", kmId], r);
      setViewingRevId(null); // back to current (now the restored content)
      void qc.invalidateQueries({ queryKey: ["knowMeRevisions", kmId] });
    } catch (e) { setErr(formatError(e)); }
  }

  const statusMeta = STATUS_META[km?.status ?? "draft"] ?? STATUS_META.draft;
  const confidence = km?.ai?.confidence ?? km?.confidence;
  const viewingRevMd = useMemo(() => revContentQ.data?.markdown ?? "", [revContentQ.data]);
  const ev = km?.ai?.evidence_used;

  // --- staleness: the source Memory changed after this Know-Me was generated ---
  const memoryUpdatedAt = kmQ.data?.memory_updated_at ?? "";
  const generatedAt = km?.ai?.generated_at ?? "";
  const isStale = useMemo(() => {
    if (!memoryUpdatedAt || !generatedAt) return false;
    const m = Date.parse(memoryUpdatedAt);
    const g = Date.parse(generatedAt);
    return Number.isFinite(m) && Number.isFinite(g) && m > g + 1000;
  }, [memoryUpdatedAt, generatedAt]);

  // --- in-doc table of contents (sections with content) ---
  const [showToc, setShowToc] = useState(false);
  const tocSections = useMemo(
    () => sections.filter((s) => (s.content || "").trim()).map((s) => ({ key: s.key, label: s.label })),
    [sections],
  );
  function jumpToSection(key: string) {
    const el = docRef.current?.querySelector(`[data-kmsec="${key}"]`) as HTMLElement | null;
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // --- revision diff (line-level) vs the current document ---
  const [diffMode, setDiffMode] = useState(false);
  const diffRows = useMemo(
    () => (viewingRevId && diffMode ? lineDiff(viewingRevMd, kmQ.data?.markdown ?? "") : []),
    [viewingRevId, diffMode, viewingRevMd, kmQ.data?.markdown],
  );
  useEffect(() => { setDiffMode(false); }, [viewingRevId]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-white">
      {editSection && (
        <SectionEditor
          architectureId={kmId}
          section={editSection}
          onClose={() => setEditSection(null)}
          onSaved={saveSection}
        />
      )}
      {/* Inline pick-or-type popover for a clicked field chip. */}
      {picker && (() => {
        const todo = todos.find((t) => t.id === picker.id);
        if (!todo) return null;
        return (
          <FieldPopover
            todo={todo}
            anchor={picker.anchor}
            saving={saving}
            onSave={async (v) => { await saveField(picker.id, v); closePicker(); }}
            onClose={closePicker}
            onSuggest={() => void suggestField(picker.id)}
            suggesting={suggestingId === picker.id}
          />
        );
      })()}
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <button onClick={() => navigate("/knowme")} aria-label="Back to Know-Me index" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">← Know-Me</button>
        <span className="text-gray-300">/</span>
        <span className="text-sm font-semibold text-gray-700">📄 {workloadName || "Workload Know-Me"}</span>
        {km && (
          <select
            value={km.status}
            onChange={(e) => void saveMeta({ status: e.target.value })}
            disabled={saving || !!viewingRevId}
            aria-label="Document status"
            title="Change this Know-Me's lifecycle status"
            className={`rounded-full border-0 px-2 py-0.5 text-[10px] font-medium focus:ring-1 focus:ring-brand ${statusMeta.cls}`}
          >
            {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        )}
        {km && (
          <button
            onClick={() => void toggleReference()}
            aria-label={km.is_reference ? "Unset as reference" : "Mark as the reference document"}
            title={km.is_reference ? "This is the workload's reference Know-Me — click to unset" : "Mark as the canonical reference for this workload"}
            className={`rounded-lg border px-1.5 py-0.5 text-[11px] ${km.is_reference ? "border-amber-300 bg-amber-50 text-amber-700" : "text-gray-400 hover:bg-gray-50"}`}
          >
            {km.is_reference ? "★ Reference" : "☆ Reference"}
          </button>
        )}
        {km && (
          <div ref={toolbarRef} className="flex items-center gap-2">
            <div className="relative">
              <button onClick={() => setOpenMenu((v) => (v === "evidence" ? "" : "evidence"))} aria-haspopup="true" aria-expanded={showEvidence} aria-label="How this Know-Me was built" className={`rounded-lg border px-2 py-1 text-[11px] hover:bg-gray-50 ${showEvidence ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-500"}`} title="How this Know-Me was built">ℹ︎ How built</button>
              {showEvidence && (
                <div className="absolute left-0 top-full z-30 mt-1 w-72 rounded-xl border bg-white p-3 text-[11px] shadow-lg">
                  <div className="mb-1 font-semibold text-gray-700">How this was built</div>
                  <ul className="space-y-1 text-gray-600">
                    <li>🔁 AI passes: <b>{km.ai?.passes ?? 1}</b> (draft + verify)</li>
                    <li>📈 Confidence: <b>{typeof confidence === "number" ? `${Math.round(confidence * 100)}%` : "—"}</b></li>
                    <li>✅ Auto-filled from platform data: <b>{km.ai?.autofilled ?? 0}</b> field(s)</li>
                    <li>🛡 Assessment findings used: <b>{ev?.assessment_findings ?? 0}</b></li>
                    <li>📡 Coverage signals: <b>{(ev?.coverage ?? []).join(", ") || "none"}</b></li>
                    <li>🚀 Performance evidence: <b>{ev?.performance ? "yes" : "no"}</b></li>
                    <li>🧹 Idle/orphaned flagged: <b>{ev?.idle_resources ?? 0}</b></li>
                    {km.ai?.generated_at && <li>🕒 Generated: <b>{new Date(km.ai.generated_at).toLocaleString()}</b></li>}
                  </ul>
                </div>
              )}
            </div>
            <div className="relative">
              <button onClick={() => setOpenMenu((v) => (v === "links" ? "" : "links"))} aria-haspopup="true" aria-expanded={showLinks} aria-label="Open related source pages" className={`rounded-lg border px-2 py-1 text-[11px] hover:bg-gray-50 ${showLinks ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-500"}`} title="Jump to the source workload / memory / architecture">🔗 Source ▾</button>
              {showLinks && (
                <div className="absolute left-0 top-full z-30 mt-1 w-56 rounded-xl border bg-white p-1 text-[12px] shadow-lg">
                  <button onClick={() => { setOpenMenu(""); navigate(`/architectures/${architectureId}/memory`); }} className="block w-full rounded-md px-2 py-1.5 text-left text-gray-700 hover:bg-gray-50">🧠 Architecture Memory</button>
                  <button onClick={() => { setOpenMenu(""); navigate(`/architectures/${architectureId}`); }} className="block w-full rounded-md px-2 py-1.5 text-left text-gray-700 hover:bg-gray-50">🏗️ Architecture diagram</button>
                  {workloadId && <button onClick={() => { setOpenMenu(""); navigate(`/mission-control/${workloadId}`); }} className="block w-full rounded-md px-2 py-1.5 text-left text-gray-700 hover:bg-gray-50">🚀 Mission Control</button>}
                  {workloadId && <button onClick={() => { setOpenMenu(""); navigate(`/assessments?workload_id=${encodeURIComponent(workloadId)}`); }} className="block w-full rounded-md px-2 py-1.5 text-left text-gray-700 hover:bg-gray-50">🛡 Assessments</button>}
                  {workloadId && <button onClick={() => { setOpenMenu(""); navigate("/workloads"); }} className="block w-full rounded-md px-2 py-1.5 text-left text-gray-700 hover:bg-gray-50">🧩 Azure Workloads</button>}
                </div>
              )}
            </div>
          </div>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {km && !viewingRevId && tocSections.length > 1 && (
            <button onClick={() => setShowToc((v) => !v)} aria-label="Toggle table of contents" title="Jump to a section" className={`rounded-lg border px-2 py-1 text-xs ${showToc ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>☰ Contents</button>
          )}
          {km && !viewingRevId && (
            <div className="mr-1 flex rounded-lg border p-0.5 text-xs">
              {(["read", "fill", "edit"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => { setMode(m); if (m === "fill") setFillScope(""); }}
                  aria-label={m === "fill" ? "Guided fill mode" : `${m} mode`}
                  className={`rounded px-2 py-0.5 ${mode === m ? "bg-brand/10 font-medium text-brand" : "text-gray-500 hover:bg-gray-100"}`}
                >
                  {m === "read" ? "Read" : m === "fill" ? `✍️ Fill${prog.open ? ` (${prog.open})` : ""}` : "Edit"}
                </button>
              ))}
            </div>
          )}
          {!viewingRevId && (
          <button
            onClick={() => void generate()}
            disabled={genState === "running" || !hasMemory}
            title={hasMemory ? "Re-transform the architecture memory into a Know-Me" : "Generate the architecture Memory first"}
            className="flex items-center gap-1 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
          >
            {genState === "running" ? <><span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" /> Generating… <span className="font-mono tabular-nums">{fmtElapsed(genElapsed)}</span></> : km ? "✨ Regenerate" : "✨ Generate from memory"}
          </button>
          )}
          {km && <>
            <button onClick={copyMarkdown} aria-label="Copy document as Markdown" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">{copied ? "✓ Copied" : "Copy"}</button>
            <a href={api.knowMeExportUrl(kmId, "md")} aria-label="Download as Markdown" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">⬇ .md</a>
            <a href={api.knowMeExportUrl(kmId, "pdf")} aria-label="Download as PDF" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">📄 PDF</a>
            <button onClick={() => (showHistory ? closeHistory() : setShowHistory(true))} aria-label="Toggle revision history" className={`rounded-lg border px-2 py-1 text-xs ${showHistory ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>🕘 History</button>
          </>}
        </div>
      </div>

      {/* Generation progress */}
      {genState === "running" && (
        <div className="border-b border-brand/20 bg-brand/5 px-3 py-2 text-[12px] text-brand">
          <div className="flex items-center gap-2">
            <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-brand border-t-transparent" />
            <span className="font-medium">Generating Know-Me…</span>
            <span className="font-mono tabular-nums text-brand/80">{fmtElapsed(genElapsed)}</span>
            <span className="text-[11px] text-brand/60">two-pass · large drafts can take a few minutes</span>
            <button onClick={cancelGenerate} className="ml-auto rounded-md border border-brand/30 bg-white px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10">Cancel</button>
          </div>
          {genSteps.length > 0 && (
            <ol ref={stepsRef} className="mt-1.5 max-h-44 space-y-0.5 overflow-y-auto border-t border-brand/10 pt-1.5">
              {genSteps.map((s, i) => {
                const isLast = i === genSteps.length - 1;
                return (
                  <li key={i} className="flex items-center gap-2 text-[11px]">
                    {isLast ? <span className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" /> : <span className="shrink-0 text-emerald-600">✓</span>}
                    <span className={isLast ? "text-brand" : "text-gray-500"}>{s.message}</span>
                    <span className="ml-auto font-mono tabular-nums text-gray-400">{fmtElapsed(s.at)}</span>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
      {err && <div className="border-b border-red-200 bg-red-50 px-3 py-1.5 text-[12px] text-red-700">{err}</div>}

      {/* Body */}
      {kmQ.isLoading ? (
        <div className="p-6 text-sm text-gray-500">Loading…</div>
      ) : !km ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center">
          <div className="text-4xl">📄</div>
          <div className="text-sm font-medium text-gray-700">No Know-Me yet for {workloadName || "this workload"}</div>
          {hasMemory ? (
            <>
              <p className="max-w-md text-xs text-gray-500">Transform this workload's Architecture Memory into a support-facing Know-Me — triage runbook, known issues, thresholds, and a guided human-completion flow for contacts &amp; escalation.</p>
              <button onClick={() => void generate()} disabled={genState === "running"} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-50">✨ Generate from memory</button>
            </>
          ) : workloadId ? (
            <>
              <p className="max-w-md text-xs text-gray-500">No Memory yet. Build it automatically from the linked workload <b>{workloadName || ""}</b> — we'll draft the Architecture Memory, then transform it into the Know-Me, in one go.</p>
              <button onClick={() => void buildFromWorkload()} disabled={genState === "running"} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-50">✨ Build memory &amp; Know-Me</button>
              <button onClick={() => navigate(`/architectures/${architectureId}/memory`)} className="text-xs text-gray-400 underline hover:text-gray-600">or open the Memory editor</button>
            </>
          ) : (
            <>
              <p className="max-w-md text-xs text-gray-500">This architecture has no Memory and no linked workload. The Know-Me is transformed from the Memory, so generate that first.</p>
              <button onClick={() => navigate(`/architectures/${architectureId}/memory`)} className="rounded-lg border border-brand/30 bg-brand/5 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/10">🧠 Open Memory</button>
            </>
          )}
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          {/* In-document table of contents (jump nav). */}
          {showToc && !viewingRevId && mode !== "fill" && tocSections.length > 1 && (
            <div className="hidden w-56 shrink-0 flex-col border-r bg-gray-50/40 lg:flex">
              <div className="flex items-center gap-2 border-b px-3 py-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Contents</span>
                <button onClick={() => setShowToc(false)} aria-label="Hide contents" className="ml-auto rounded-md px-1.5 py-0.5 text-[11px] text-gray-400 hover:bg-gray-100">✕</button>
              </div>
              <nav className="min-h-0 flex-1 overflow-auto p-1.5">
                {tocSections.map((s, idx) => (
                  <button
                    key={s.key}
                    onClick={() => jumpToSection(s.key)}
                    className="flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-gray-600 hover:bg-white hover:text-brand"
                  >
                    <span className="shrink-0 font-mono text-[10px] text-gray-300">{String(idx + 1).padStart(2, "0")}</span>
                    <span className="truncate">{s.label}</span>
                  </button>
                ))}
              </nav>
            </div>
          )}
          {/* Document — live sections, OR a read-only past revision (with a warning banner). */}
          <div ref={docRef} className="min-w-0 flex-1 overflow-auto p-5">
            {viewingRevId ? (
              <div className="mx-auto max-w-3xl space-y-3 lg:max-w-4xl xl:max-w-6xl 2xl:max-w-[100rem]">
                <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
                  <span className="text-base">🕘</span>
                  <span>
                    You are viewing a <b>past revision</b>{viewingRev ? <> — “{viewingRev.reason}”, {new Date(viewingRev.created_at).toLocaleString()}{viewingRev.by ? ` by ${viewingRev.by}` : ""}</> : ""}. It is <b>read-only</b>.
                  </span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => setDiffMode((v) => !v)} aria-pressed={diffMode} className={`rounded-lg border px-2.5 py-1 text-xs font-medium ${diffMode ? "border-brand/40 bg-brand/10 text-brand" : "border-amber-400 bg-white text-amber-800 hover:bg-amber-100"}`}>{diffMode ? "📄 Preview" : "± Diff vs current"}</button>
                    <button onClick={() => void restoreRev(viewingRevId)} className="rounded-lg border border-amber-400 bg-white px-2.5 py-1 text-xs font-semibold text-amber-800 hover:bg-amber-100">↩ Restore to edit</button>
                    <button onClick={() => viewRevision(null)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-white">Back to current</button>
                  </div>
                </div>
                {revContentQ.isLoading ? (
                  <div className="py-10 text-center text-sm text-gray-400">Loading revision…</div>
                ) : diffMode ? (
                  <div className="overflow-hidden rounded-xl border">
                    <div className="flex items-center gap-3 border-b bg-gray-50 px-3 py-1.5 text-[11px] text-gray-500">
                      <span><span className="rounded bg-red-100 px-1 text-red-700">− this revision</span></span>
                      <span><span className="rounded bg-green-100 px-1 text-green-700">+ current version</span></span>
                      <span className="ml-auto">{diffRows.filter((r) => r.type !== "same").length} changed line(s)</span>
                    </div>
                    <pre className="max-h-[70vh] overflow-auto bg-white p-0 text-[12px] leading-relaxed">
                      {diffRows.map((r, idx) => (
                        <div
                          key={idx}
                          className={`whitespace-pre-wrap break-words px-3 ${r.type === "add" ? "bg-green-50 text-green-800" : r.type === "del" ? "bg-red-50 text-red-800" : "text-gray-600"}`}
                        >
                          <span className="mr-2 select-none text-gray-300">{r.type === "add" ? "+" : r.type === "del" ? "−" : " "}</span>{r.text || " "}
                        </div>
                      ))}
                    </pre>
                  </div>
                ) : (
                  <div className={`${PROSE} rounded-xl border border-amber-200 bg-amber-50/20 p-5`}>
                    <Markdown components={docMarkdownComponents}>{withAssetUrls(kmId, viewingRevMd)}</Markdown>
                  </div>
                )}
              </div>
            ) : (<>
            {mode === "fill" && prog.requiredOpen > 0 && (
              <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-800">
                ⚠ {prog.requiredOpen} required field{prog.requiredOpen === 1 ? "" : "s"} must be completed before publishing.
              </div>
            )}
            {isStale && mode !== "fill" && (
              <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-orange-300 bg-orange-50 px-3 py-2 text-[12px] text-orange-800">
                <span className="text-base">🔄</span>
                <span>The source <b>Architecture Memory</b> changed after this Know-Me was generated{generatedAt ? <> ({new Date(generatedAt).toLocaleDateString()})</> : ""} — it may be out of date.</span>
                <div className="ml-auto flex items-center gap-1.5">
                  <button onClick={() => void generate()} disabled={genState === "running" || !hasMemory} className="rounded-lg border border-orange-400 bg-white px-2.5 py-1 text-xs font-semibold text-orange-800 hover:bg-orange-100 disabled:opacity-50">✨ Re-sync from Memory</button>
                  <button onClick={() => navigate(`/architectures/${architectureId}/memory`)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-white">Open Memory</button>
                </div>
              </div>
            )}
            <div className="mx-auto max-w-3xl space-y-1 lg:max-w-4xl xl:max-w-6xl 2xl:max-w-[100rem]">
              {editingMeta ? (
                <div className="space-y-2 rounded-xl border border-brand/30 bg-brand/5 p-3">
                  <input
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    placeholder={`Know-Me — ${workloadName}`}
                    aria-label="Document title"
                    className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-lg font-bold text-gray-900 focus:border-brand focus:outline-none"
                    autoFocus
                  />
                  <input
                    value={draftDesc}
                    onChange={(e) => setDraftDesc(e.target.value)}
                    placeholder="Optional one-line description (helps tell multiple drafts apart)"
                    aria-label="Document description"
                    className="w-full rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 focus:border-brand focus:outline-none"
                  />
                  <div className="flex items-center gap-2">
                    <button onClick={() => void commitMeta()} disabled={saving} className="rounded-lg bg-brand px-3 py-1 text-xs font-semibold text-white hover:bg-brand-dark disabled:opacity-50">Save</button>
                    <button onClick={() => setEditingMeta(false)} className="rounded-lg border px-3 py-1 text-xs text-gray-600 hover:bg-gray-50">Cancel</button>
                  </div>
                </div>
              ) : (
                <div className="group/title">
                  <h1 className="flex items-center gap-2 text-2xl font-bold text-gray-900">
                    {km.title || `Know-Me — ${workloadName}`}
                    {km.is_reference && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700" title="Reference document for this workload">★ Reference</span>}
                    {!viewingRevId && (
                      <button onClick={beginEditMeta} aria-label="Rename document" title="Rename / edit description" className="rounded-md border px-1.5 py-0.5 text-[11px] text-gray-400 opacity-0 transition hover:bg-gray-50 group-hover/title:opacity-100">✏️ Rename</button>
                    )}
                  </h1>
                  {km.description && <p className="mt-0.5 text-sm text-gray-600">{km.description}</p>}
                  {workloadName && <p className="text-sm text-gray-500">Workload: {workloadName}</p>}
                </div>
              )}
              {sections.filter((s) => (s.content || "").trim()).map((s) => {
                const body = withAssetUrls(kmId, renderSectionRead(s.content, s.key, todos, highlightId, s.label));
                const sectionOpen = todos.filter((t) => t.section_key === s.key && t.status !== "done").length;
                return (
                  <section key={s.key} data-kmsec={s.key} className="group scroll-mt-20 rounded-xl border border-transparent px-3 py-2 transition hover:border-gray-100 hover:bg-gray-50/40">
                    <div className="flex items-center gap-2">
                      <h2 className="flex-1 text-lg font-bold text-gray-900">{s.label}</h2>
                      <div className={`relative flex items-center gap-1 transition ${regenKey === s.key ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}>
                        {sectionOpen > 0 && (
                          <button
                            onClick={() => { setFillScope(s.key); setMode("fill"); }}
                            disabled={!!regenKey}
                            title={`Fill this section's ${sectionOpen} open field${sectionOpen === 1 ? "" : "s"} with AI assistance`}
                            className="flex items-center gap-1 rounded border border-brand/30 bg-brand/5 px-1.5 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
                          >
                            ✍️ Fill <span className="rounded-full bg-brand/15 px-1 text-[9px] tabular-nums">{sectionOpen}</span>
                          </button>
                        )}
                        <button onClick={() => void regenSection(s.key)} disabled={!!regenKey} aria-label={`Regenerate section ${s.label} with AI`} title="Regenerate this section with AI" className="rounded border px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-white disabled:opacity-50">
                          {regenKey === s.key ? <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent align-middle" /> : "✨"}
                        </button>
                        <button onClick={() => setEditSection(s)} disabled={!!regenKey} aria-label={`Edit section ${s.label}`} title="Edit this section" className="rounded border px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-white disabled:opacity-50">✏️</button>
                        {/* Live regenerate status popup, anchored to this section's ✨ button. */}
                        {regenKey === s.key && (
                          <div className="absolute right-0 top-full z-30 mt-1.5 w-80 rounded-xl border border-brand/20 bg-white p-2.5 text-left shadow-xl">
                            <div className="flex items-center gap-2 border-b border-brand/10 pb-1.5 text-[12px] font-medium text-brand">
                              <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                              <span>Regenerating “{s.label}”…</span>
                              <button onClick={cancelRegen} className="ml-auto rounded border border-brand/30 px-1.5 py-0.5 text-[10px] font-medium text-brand hover:bg-brand/5">Cancel</button>
                            </div>
                            <ol className="mt-1.5 max-h-48 space-y-0.5 overflow-y-auto">
                              {regenSteps.map((st, i) => {
                                const isLast = i === regenSteps.length - 1;
                                return (
                                  <li key={i} className="flex items-center gap-2 text-[11px]">
                                    {isLast ? <span className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" /> : <span className="shrink-0 text-emerald-600">✓</span>}
                                    <span className={isLast ? "text-brand" : "text-gray-500"}>{st.message}</span>
                                    <span className="ml-auto font-mono tabular-nums text-gray-400">{st.at}s</span>
                                  </li>
                                );
                              })}
                            </ol>
                          </div>
                        )}
                      </div>
                    </div>
                    <div className={PROSE}><Markdown components={docMarkdownComponents}>{body}</Markdown></div>
                  </section>
                );
              })}
            </div>
            {/* Small-screen completion + publish (the full rail shows on lg+). */}
            {mode !== "fill" && (
              <div className="mx-auto mt-4 max-w-3xl rounded-xl border bg-gray-50/60 p-3 lg:hidden">
                <div className="flex items-center gap-2">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
                    <div className="h-full rounded-full bg-brand" style={{ width: `${prog.total ? Math.round((prog.done / prog.total) * 100) : 100}%` }} />
                  </div>
                  <span className="font-mono text-[11px] tabular-nums text-gray-600">{prog.done}/{prog.total}</span>
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button onClick={() => setMode("fill")} className="flex-1 rounded-lg border border-brand/30 bg-brand/5 px-2 py-1.5 text-xs font-medium text-brand hover:bg-brand/10">✍️ Guided fill{prog.open ? ` (${prog.open})` : ""}</button>
                  <button
                    onClick={() => void saveTodos(todos, "published")}
                    disabled={saving || prog.requiredOpen > 0}
                    className="flex-1 rounded-lg bg-green-600 px-2 py-1.5 text-xs font-semibold text-white hover:bg-green-700 disabled:opacity-40"
                  >
                    {km.status === "published" ? "✓ Published" : prog.requiredOpen > 0 ? `Publish (${prog.requiredOpen})` : "Publish"}
                  </button>
                </div>
              </div>
            )}
            </>)}
          </div>

          {/* Guided fill panel */}
          {mode === "fill" && (
            <GuidedFill
              todos={todos}
              sections={sections}
              sectionKeys={sectionKeys}
              saving={saving}
              onSave={(next) => void saveTodos(next)}
              onExit={() => { setMode("read"); setHighlightId(""); setFillScope(""); }}
              onScrollToSection={scrollToSection}
              onPublish={() => void saveTodos(todos, "published")}
              scopeSection={fillScope || undefined}
              onClearScope={() => setFillScope("")}
              onSuggestField={(fid) => void suggestField(fid)}
              suggestingId={suggestingId}
            />
          )}

          {/* History — list only; selecting an entry opens it in the main document area. */}
          {showHistory && mode !== "fill" && (
            <div className="hidden w-80 shrink-0 flex-col border-l lg:flex">
              <div className="flex items-center gap-2 border-b px-3 py-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Revision history</span>
                <button onClick={closeHistory} className="ml-auto rounded-md px-2 py-0.5 text-[11px] text-gray-400 hover:bg-gray-100">✕ Close</button>
              </div>
              <div className="min-h-0 flex-1 overflow-auto">
                {/* Current / live version — always first, so the user can return to it. */}
                <div
                  className={`cursor-pointer border-b px-3 py-2 text-[11px] hover:bg-gray-50 ${!viewingRevId ? "bg-brand/5" : ""}`}
                  onClick={() => viewRevision(null)}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="font-semibold text-gray-800">● Current version</span>
                    {!viewingRevId && <span className="rounded-full bg-brand/10 px-1.5 py-0.5 text-[9px] font-medium text-brand">viewing</span>}
                    <span className="ml-auto text-gray-400">{km.updated_at ? new Date(km.updated_at).toLocaleString() : ""}</span>
                  </div>
                  <div className="text-gray-400">Live, editable · {prog.done}/{prog.total} fields{km.updated_by ? ` · ${km.updated_by}` : ""}</div>
                </div>
                {revQ.isLoading ? (
                  <div className="p-3 text-[11px] text-gray-400">Loading…</div>
                ) : (revQ.data?.revisions ?? []).length === 0 ? (
                  <div className="p-3 text-[11px] text-gray-400">No earlier versions yet.</div>
                ) : (revQ.data?.revisions ?? []).map((r) => (
                  <div key={r.id} className={`group cursor-pointer border-b px-3 py-2 text-[11px] hover:bg-gray-50 ${viewingRevId === r.id ? "bg-amber-50" : ""}`} onClick={() => viewRevision(r.id)}>
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium text-gray-700">{r.reason}</span>
                      {viewingRevId === r.id && <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-medium text-amber-700">viewing</span>}
                      <span className="ml-auto text-gray-400">{new Date(r.created_at).toLocaleString()}</span>
                    </div>
                    <div className="flex items-center gap-2 text-gray-400">
                      <span>{r.filled_count}/{r.section_count} sections · {r.open_todos} open TODO{r.open_todos === 1 ? "" : "s"}{r.by ? ` · ${r.by}` : ""}</span>
                      <button
                        onClick={(e) => { e.stopPropagation(); void restoreRev(r.id); }}
                        title="Restore this version as the new current version"
                        className="ml-auto rounded border border-amber-300 bg-white px-1.5 py-0.5 font-medium text-amber-700 opacity-0 transition hover:bg-amber-100 group-hover:opacity-100"
                      >↩ Restore</button>
                    </div>
                  </div>
                ))}
              </div>
              <div className="border-t px-3 py-2 text-[10px] text-gray-400">
                Viewing a past version is read-only. <b>Restore</b> it to make edits — that saves a new current revision.
              </div>
            </div>
          )}

          {/* Publish bar (read/edit modes, current version only) */}
          {mode !== "fill" && !showHistory && !viewingRevId && (
            <div className="hidden w-64 shrink-0 flex-col border-l lg:flex">
              <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Completion</div>
              <div className="space-y-2 p-3 text-[12px] text-gray-600">
                <div className="flex items-center gap-2">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
                    <div className="h-full rounded-full bg-brand" style={{ width: `${prog.total ? Math.round((prog.done / prog.total) * 100) : 100}%` }} />
                  </div>
                  <span className="font-mono text-[11px] tabular-nums">{prog.done}/{prog.total}</span>
                </div>
                {prog.requiredOpen > 0 ? (
                  <div className="text-[11px] text-amber-600">{prog.requiredOpen} required field{prog.requiredOpen === 1 ? "" : "s"} remaining</div>
                ) : (
                  <div className="text-[11px] text-emerald-600">All required fields complete ✓</div>
                )}
                <button onClick={() => setMode("fill")} className="w-full rounded-lg border border-brand/30 bg-brand/5 px-2 py-1.5 text-xs font-medium text-brand hover:bg-brand/10">✍️ Guided fill</button>
                <button
                  onClick={() => void saveTodos(todos, "published")}
                  disabled={saving || prog.requiredOpen > 0}
                  title={prog.requiredOpen > 0 ? "Complete required fields before publishing" : "Mark this Know-Me as published"}
                  className="w-full rounded-lg bg-green-600 px-2 py-1.5 text-xs font-semibold text-white hover:bg-green-700 disabled:opacity-40"
                >
                  {km.status === "published" ? "✓ Published" : prog.requiredOpen > 0 ? `Publish (${prog.requiredOpen} required)` : "Publish"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


const SOURCE_BADGE: Record<string, string> = {
  ai: "bg-violet-100 text-violet-700",
  hybrid: "bg-sky-100 text-sky-700",
  edited: "bg-gray-100 text-gray-600",
};

const PHASE_META: Record<string, { label: string; icon: string }> = {
  architecture: { label: "Architecture", icon: "🏗️" },
  memory: { label: "Memory", icon: "🧠" },
  knowme: { label: "Know-Me", icon: "📄" },
  save: { label: "Saving", icon: "💾" },
};

/** Modal: pick an Azure workload → the backend ensures it has an architecture + memory
 *  (building them with AI if missing), then transforms the memory into a Know-Me, streaming
 *  live progress across all three phases. On success the caller navigates to the document. */
function BuildFromWorkloadModal({ onClose, onBuilt }: { onClose: () => void; onBuilt: (kmId: string) => void }) {
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = useMemo(
    () => [...(wlQ.data?.workloads ?? [])].sort((a, b) => (a.name || "").localeCompare(b.name || "")),
    [wlQ.data],
  );
  const [wlId, setWlId] = useState("");
  const [state, setState] = useState<"idle" | "running">("idle");
  const [steps, setSteps] = useState<{ phase: string; message: string; at: number }[]>([]);
  const [phase, setPhase] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [err, setErr] = useState("");
  const start = useRef(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const abort = useRef<AbortController | null>(null);
  const stepsRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    const el = stepsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [steps.length]);
  useEffect(() => () => { if (timer.current) clearInterval(timer.current); abort.current?.abort(); }, []);

  function stopTimer() {
    if (timer.current) { clearInterval(timer.current); timer.current = null; }
  }

  async function build() {
    if (!wlId || state === "running") return;
    setErr("");
    setState("running");
    setSteps([{ phase: "architecture", message: "Starting…", at: 0 }]);
    setPhase("architecture");
    start.current = Date.now();
    setElapsed(0);
    stopTimer();
    timer.current = setInterval(() => setElapsed(Math.floor((Date.now() - start.current) / 1000)), 1000);
    const ctrl = new AbortController();
    abort.current = ctrl;
    let builtId = "";
    await streamBuildKnowMeFromWorkload(
      { workload_id: wlId },
      {
        onStatus: (s) => {
          if (s.phase) setPhase(s.phase);
          setSteps((prev) => [...prev, { phase: s.phase, message: s.message, at: Math.floor((Date.now() - start.current) / 1000) }]);
        },
        onDone: (r: KnowMeResponse) => { builtId = r.id ?? r.know_me?.id ?? ""; },
        onError: (m) => setErr(m),
      },
      ctrl.signal,
    );
    stopTimer();
    setState("idle");
    if (builtId) onBuilt(builtId);
  }

  function cancel() {
    abort.current?.abort();
    stopTimer();
    setState("idle");
  }

  const selected = workloads.find((w) => w.id === wlId);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => state !== "running" && onClose()}>
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <span className="text-lg">✨</span>
          <span className="text-sm font-semibold text-gray-800">Build a Know-Me from a workload</span>
          <button onClick={() => state !== "running" && onClose()} disabled={state === "running"} className="ml-auto rounded-md px-2 py-1 text-gray-400 hover:bg-gray-100 disabled:opacity-40">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <p className="mb-3 text-xs leading-relaxed text-gray-500">
            Pick an Azure workload. Behind the scenes we use (or reverse-engineer) its <b>architecture</b>, draft its <b>memory</b> if needed, then transform that into a support-facing <b>Know-Me</b> — all in one go.
          </p>

          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-gray-500">Workload</label>
          {wlQ.isLoading ? (
            <div className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-400">Loading workloads…</div>
          ) : workloads.length === 0 ? (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">No workloads found. Create one under Azure Workloads first.</div>
          ) : (
            <select
              value={wlId}
              onChange={(e) => setWlId(e.target.value)}
              disabled={state === "running"}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none disabled:bg-gray-50"
            >
              <option value="">Select a workload…</option>
              {workloads.map((w) => (
                <option key={w.id} value={w.id}>
                  🧩 {w.name}{w.environment && w.environment !== "unknown" ? ` · ${w.environment}` : ""}
                </option>
              ))}
            </select>
          )}
          {selected?.description && state === "idle" && (
            <p className="mt-2 line-clamp-3 text-[11px] leading-relaxed text-gray-400">{selected.description}</p>
          )}

          {state === "running" && (
            <div className="mt-4 rounded-xl border border-brand/20 bg-brand/5 p-3">
              <div className="flex items-center gap-2 text-[12px] text-brand">
                <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                <span className="font-medium">{PHASE_META[phase]?.icon ?? "⏳"} {PHASE_META[phase]?.label ?? "Working"}…</span>
                <span className="font-mono tabular-nums text-brand/80">{fmtElapsed(elapsed)}</span>
                <span className="ml-auto text-[10px] text-brand/60">this can take a few minutes</span>
              </div>
              <ol ref={stepsRef} className="mt-2 max-h-52 space-y-0.5 overflow-y-auto border-t border-brand/10 pt-2">
                {steps.map((s, i) => {
                  const isLast = i === steps.length - 1;
                  return (
                    <li key={i} className="flex items-center gap-2 text-[11px]">
                      {isLast ? <span className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" /> : <span className="shrink-0 text-emerald-600">✓</span>}
                      <span className="shrink-0 text-gray-400">{PHASE_META[s.phase]?.icon ?? "•"}</span>
                      <span className={isLast ? "text-brand" : "text-gray-500"}>{s.message}</span>
                      <span className="ml-auto font-mono tabular-nums text-gray-400">{fmtElapsed(s.at)}</span>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
          {err && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[12px] text-red-700">{err}</div>}
        </div>

        <div className="flex items-center gap-2 border-t px-4 py-3">
          <span className="text-[11px] text-gray-400">{PHASE_META.architecture.icon} Architecture → {PHASE_META.memory.icon} Memory → {PHASE_META.knowme.icon} Know-Me</span>
          <div className="ml-auto flex items-center gap-2">
            {state === "running" ? (
              <button onClick={cancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
            ) : (
              <>
                <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Close</button>
                <button
                  onClick={() => void build()}
                  disabled={!wlId}
                  className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark disabled:opacity-40"
                >
                  ✨ Build Know-Me
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** The Trash drawer — soft-deleted Know-Me documents with restore / purge / empty. */
function KnowMeTrashPanel({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const trashQ = useQuery({ queryKey: ["knowMeTrash"], queryFn: api.knowMeTrash });
  const items = trashQ.data?.items ?? [];
  const [busy, setBusy] = useState("");

  async function restore(id: string) {
    setBusy(id);
    try {
      await api.restoreKnowMe(id);
      await qc.invalidateQueries({ queryKey: ["knowMeTrash"] });
      await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } finally { setBusy(""); }
  }
  async function purge(id: string) {
    if (!window.confirm("Permanently delete this Know-Me? This cannot be undone — its revisions and images are removed too.")) return;
    setBusy(id);
    try {
      await api.purgeKnowMe(id);
      await qc.invalidateQueries({ queryKey: ["knowMeTrash"] });
      await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } finally { setBusy(""); }
  }
  async function emptyAll() {
    if (!items.length) return;
    if (!window.confirm(`Permanently delete all ${items.length} Know-Me document(s) in the Trash? This cannot be undone.`)) return;
    setBusy("__all__");
    try {
      await api.emptyKnowMeTrash();
      await qc.invalidateQueries({ queryKey: ["knowMeTrash"] });
      await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
    } finally { setBusy(""); }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <div className="flex h-full w-full max-w-md flex-col bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <span className="text-lg">🗑️</span>
          <span className="text-sm font-semibold text-gray-800">Know-Me Trash</span>
          {items.length > 0 && <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">{items.length}</span>}
          <div className="ml-auto flex items-center gap-2">
            {items.length > 0 && (
              <button onClick={() => void emptyAll()} disabled={busy === "__all__"} className="rounded-lg border border-red-200 px-2 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Empty trash</button>
            )}
            <button onClick={onClose} className="rounded-md px-2 py-1 text-gray-400 hover:bg-gray-100">✕</button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          {trashQ.isLoading ? (
            <div className="py-10 text-center text-sm text-gray-400">Loading…</div>
          ) : items.length === 0 ? (
            <div className="py-10 text-center text-sm text-gray-400">The Trash is empty.</div>
          ) : (
            <div className="space-y-2">
              {items.map((t) => (
                <div key={t.id} className="rounded-xl border border-gray-200 bg-white px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-700">{t.title || t.workload_name || "Untitled Know-Me"}</span>
                    <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${(STATUS_META[t.status] ?? STATUS_META.draft).cls}`}>{(STATUS_META[t.status] ?? STATUS_META.draft).label}</span>
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-gray-400">
                    {t.workload_name ? `🧩 ${t.workload_name} · ` : ""}deleted {t.deleted_at ? new Date(t.deleted_at).toLocaleString() : "—"}{t.deleted_by ? ` by ${t.deleted_by}` : ""}
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <button onClick={() => void restore(t.id)} disabled={busy === t.id} className="rounded-lg border border-brand/30 bg-brand/5 px-2 py-1 text-[11px] font-medium text-brand hover:bg-brand/10 disabled:opacity-50">↩ Restore</button>
                    <button onClick={() => void purge(t.id)} disabled={busy === t.id} className="rounded-lg border border-red-200 px-2 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Standalone index of all Workload Know-Me documents (top-level /knowme page). A workload
 *  can have MANY Know-Me documents (drafts + a published reference); they are grouped by
 *  workload/architecture. Each architecture with a Memory can spawn a new Know-Me. */
export function KnowMeIndex() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const term = q.trim().toLowerCase();
  const query = useQuery({ queryKey: ["knowMeIndex"], queryFn: api.knowMeIndex });
  const [building, setBuilding] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  const [creating, setCreating] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "draft" | "in_review" | "published" | "archived">("all");

  const documents: KnowMeDocument[] = query.data?.documents ?? [];
  const buildable: KnowMeBuildable[] = query.data?.buildable ?? [];
  const trashCount = query.data?.trash_count ?? 0;

  // Group documents under their source architecture; include buildable architectures that
  // have no document yet so the user can create the first one.
  const groups = useMemo(() => {
    const byArch = new Map<string, { architecture_id: string; architecture_name: string; workload_name: string; architecture_exists: boolean; docs: KnowMeDocument[] }>();
    for (const b of buildable) {
      byArch.set(b.architecture_id, {
        architecture_id: b.architecture_id,
        architecture_name: b.architecture_name,
        workload_name: b.workload_name,
        architecture_exists: b.architecture_exists,
        docs: [],
      });
    }
    for (const d of documents) {
      if (statusFilter !== "all" && d.status !== statusFilter) continue;
      let g = byArch.get(d.architecture_id);
      if (!g) {
        g = { architecture_id: d.architecture_id, architecture_name: d.architecture_name, workload_name: d.workload_name, architecture_exists: d.architecture_exists, docs: [] };
        byArch.set(d.architecture_id, g);
      }
      g.docs.push(d);
    }
    let list = [...byArch.values()];
    // Within a workload, surface the reference doc first, then published, then by recency.
    const rank = (d: KnowMeDocument) => (d.is_reference ? 0 : d.status === "published" ? 1 : d.status === "in_review" ? 2 : d.status === "draft" ? 3 : 4);
    for (const g of list) g.docs.sort((a, b) => rank(a) - rank(b) || (b.updated_at || "").localeCompare(a.updated_at || ""));
    if (statusFilter !== "all") list = list.filter((g) => g.docs.length > 0);
    if (term) {
      list = list.filter(
        (g) => g.workload_name.toLowerCase().includes(term) || g.architecture_name.toLowerCase().includes(term) || g.docs.some((d) => (d.title || "").toLowerCase().includes(term)),
      );
    }
    list.sort((a, b) => (a.workload_name || a.architecture_name).localeCompare(b.workload_name || b.architecture_name));
    return list;
  }, [documents, buildable, term, statusFilter]);

  async function createNew(architectureId: string) {
    setCreating(architectureId);
    try {
      const r = await api.createKnowMe(architectureId);
      await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
      if (r.id) navigate(`/knowme/${r.id}`);
    } finally { setCreating(""); }
  }
  async function softDelete(id: string) {
    if (!window.confirm("Move this Know-Me to the Trash? You can restore it later.")) return;
    await api.deleteKnowMe(id);
    await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
  }
  // Permanently remove an ORPHANED group — the architecture is gone, so there's nothing to
  // restore; purge its leftover Memory + any Know-Me docs in one go.
  async function purgeOrphan(architectureId: string) {
    if (!window.confirm("Permanently remove this orphaned Know-Me and its leftover Memory? The source architecture no longer exists, so this can't be undone.")) return;
    await api.purgeKnowMeOrphan(architectureId);
    await qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
  }

  const totalDocs = documents.length;

  return (
    <div className="h-full overflow-y-auto bg-gray-50/40">
      {building && (
        <BuildFromWorkloadModal
          onClose={() => setBuilding(false)}
          onBuilt={(kmId) => {
            setBuilding(false);
            void qc.invalidateQueries({ queryKey: ["knowMeIndex"] });
            navigate(`/knowme/${kmId}`);
          }}
        />
      )}
      {showTrash && <KnowMeTrashPanel onClose={() => setShowTrash(false)} />}
      <div className="space-y-4 p-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold text-gray-900">📄 Workload Know-Me</h1>
            <p className="mt-0.5 text-sm text-gray-500">
              Support-facing references transformed from each workload's Architecture Memory. A workload can keep multiple — drafts and a published reference — for the read a responder needs to triage a case.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search workloads…"
              aria-label="Search Know-Me documents"
              className="w-56 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
            />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
              aria-label="Filter by status"
              title="Filter documents by lifecycle status"
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
              aria-label="View deleted Know-Me documents"
              title="View deleted Know-Me documents"
              className="relative shrink-0 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50"
            >
              🗑️ Trash{trashCount > 0 ? ` (${trashCount})` : ""}
            </button>
            <button
              onClick={() => setBuilding(true)}
              title="Pick an Azure workload — its architecture & memory are built automatically, then transformed into a Know-Me"
              className="shrink-0 rounded-lg bg-brand px-3 py-2 text-sm font-semibold text-white hover:bg-brand-dark"
            >
              ✨ Build from workload
            </button>
          </div>
        </div>

        {query.isLoading && <div className="py-10 text-center text-sm text-gray-400">Loading…</div>}
        {!query.isLoading && groups.length === 0 && !term && (
          <div className="rounded-xl border bg-white p-8 text-center">
            <div className="text-sm font-medium text-gray-700">No Know-Me documents yet</div>
            <p className="mx-auto mt-1 max-w-md text-xs text-gray-500">
              Build one straight from an Azure workload — its architecture and memory are created automatically behind the scenes, then transformed into a support-facing Know-Me.
            </p>
            <button onClick={() => setBuilding(true)} className="mt-3 rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-dark">✨ Build from workload</button>
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
                      </span>
                      <span className="block truncate text-[11px] text-gray-400">{g.architecture_name}</span>
                    </span>
                    {g.architecture_exists ? (
                      <button
                        onClick={() => void createNew(g.architecture_id)}
                        disabled={creating === g.architecture_id}
                        title="Create a new (empty draft) Know-Me for this workload"
                        className="shrink-0 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
                      >
                        {creating === g.architecture_id ? "Creating…" : "+ New"}
                      </button>
                    ) : (
                      <button
                        onClick={() => void purgeOrphan(g.architecture_id)}
                        title="Permanently remove this orphaned Know-Me and its leftover Memory (the architecture no longer exists)"
                        className="shrink-0 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-100"
                      >
                        🗑️ Remove
                      </button>
                    )}
                  </div>
                  {g.docs.length === 0 ? (
                    <div className="px-4 py-3 text-[12px] text-gray-400">No Know-Me yet — click <b>+ New</b> to start one, or use ✨ Build from workload.</div>
                  ) : (
                    <div className="divide-y">
                      {g.docs.map((d) => {
                        const sm = STATUS_META[d.status] ?? STATUS_META.draft;
                        return (
                          <div key={d.id} className="group flex items-center gap-3 px-4 py-2.5 transition hover:bg-gray-50/60">
                            <button onClick={() => navigate(`/knowme/${d.id}`)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
                              <span className="text-lg leading-none">📄</span>
                              <span className="min-w-0 flex-1">
                                <span className="flex items-center gap-2">
                                  {d.is_reference && <span className="shrink-0 text-amber-500" title="Reference document">★</span>}
                                  <span className="truncate text-sm font-medium text-gray-800">{d.title || `Know-Me — ${d.workload_name || g.workload_name || "draft"}`}</span>
                                  <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${sm.cls}`}>{sm.label}</span>
                                  {d.source && <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${SOURCE_BADGE[d.source] ?? "bg-gray-100 text-gray-600"}`}>{d.source}</span>}
                                </span>
                                {d.description && <span className="mt-0.5 block truncate text-[11px] text-gray-500">{d.description}</span>}
                                <span className="mt-0.5 block truncate text-[11px] text-gray-400">
                                  {d.filled_count}/{d.section_count} sections{d.open_todos ? ` · ${d.open_todos} open TODO${d.open_todos === 1 ? "" : "s"}` : ""} · updated {d.updated_at ? new Date(d.updated_at).toLocaleDateString() : "—"}
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

/** Route dispatcher for the top-level /knowme feature: /knowme → index, /knowme/:id → view
 *  (where :id is the Know-Me document id, km_id). */
export function KnowMePanel() {
  const location = useLocation();
  const segs = location.pathname.split("/").filter(Boolean); // ["knowme", :id?]
  const id = segs[1];
  if (id) return <KnowMeView kmId={id} />;
  return <KnowMeIndex />;
}

