// Guided fill: a focused single-field walker that advances top-to-bottom
// through every human-completion field, scrolling the document to the field's section and
// flashing it as you go. Enter = save & advance, Tab/Shift-Tab = next/prev, S = skip,
// Esc = exit. Reuses the typed FieldInput (suggestions + validation).
import { useCallback, useEffect, useMemo, useState } from "react";
import type { KnowMeSection, KnowMeTodo } from "../../api";
import { FieldInput } from "./FieldInput";
import { effectiveGroup, fieldContext, fieldProgress, groupLabel, orderTodos, validateField } from "./fields";

export function GuidedFill({
  todos,
  sections,
  sectionKeys,
  saving,
  onSave,
  onExit,
  onScrollToSection,
  onPublish,
  scopeSection,
  onClearScope,
  focusFieldId,
  focusNonce,
  onSuggestField,
  suggestingId,
}: {
  todos: KnowMeTodo[];
  sections: KnowMeSection[];
  sectionKeys: string[];
  saving: boolean;
  onSave: (next: KnowMeTodo[]) => void;
  onExit: () => void;
  onScrollToSection: (sectionKey: string, todoId: string) => void;
  onPublish?: () => void;
  // When set, the walker is restricted to this section's fields only (a per-section fill).
  scopeSection?: string;
  onClearScope?: () => void;
  // When a field chip in the document is clicked, jump the walker straight to that field.
  // ``focusNonce`` bumps on every click so re-clicking the same field re-focuses it.
  focusFieldId?: string;
  focusNonce?: number;
  // On-demand AI "suggest options" for a field (P3).
  onSuggestField?: (fieldId: string) => void;
  suggestingId?: string;
}) {
  const ordered = useMemo(() => {
    const all = orderTodos(todos, sectionKeys);
    return scopeSection ? all.filter((t) => t.section_key === scopeSection) : all;
  }, [todos, sectionKeys, scopeSection]);
  const [working, setWorking] = useState<KnowMeTodo[]>(ordered);
  useEffect(() => {
    const all = orderTodos(todos, sectionKeys);
    setWorking(scopeSection ? all.filter((t) => t.section_key === scopeSection) : all);
  }, [todos, sectionKeys, scopeSection]);
  const scopeLabel = useMemo(
    () => sections.find((s) => s.key === scopeSection)?.label ?? "",
    [sections, scopeSection],
  );

  // Start at the first OPEN field.
  const firstOpen = Math.max(0, ordered.findIndex((t) => t.status !== "done"));
  const [i, setI] = useState(firstOpen);
  const [draft, setDraft] = useState("");
  const [draftAssignee, setDraftAssignee] = useState("");
  const [draftNote, setDraftNote] = useState("");
  const [showMeta, setShowMeta] = useState(false);
  // When the scope (per-section vs whole-doc) changes, jump to that scope's first open field.
  useEffect(() => {
    const all = orderTodos(todos, sectionKeys);
    const list = scopeSection ? all.filter((t) => t.section_key === scopeSection) : all;
    const idx = list.findIndex((t) => t.status !== "done");
    setI(idx >= 0 ? idx : 0);
  }, [scopeSection]); // eslint-disable-line react-hooks/exhaustive-deps
  // When a document field chip is clicked, jump straight to that field (by id).
  useEffect(() => {
    if (!focusFieldId) return;
    const idx = working.findIndex((t) => t.id === focusFieldId);
    if (idx >= 0) setI(idx);
  }, [focusNonce]); // eslint-disable-line react-hooks/exhaustive-deps
  const contentByKey = useMemo(
    () => Object.fromEntries(sections.map((s) => [s.key, s.content || ""])),
    [sections],
  );

  const cur: KnowMeTodo | undefined = working[i];
  const finished = i >= working.length;

  // Load the draft + scroll to the field whenever the index changes.
  useEffect(() => {
    if (!cur) return;
    setDraft(cur.value || "");
    setDraftAssignee(cur.assignee || "");
    setDraftNote(cur.note || "");
    setShowMeta(!!(cur.assignee || cur.note));
    onScrollToSection(cur.section_key, cur.id);
  }, [i]); // eslint-disable-line react-hooks/exhaustive-deps

  const commit = useCallback(
    (idx: number, value: string, markDone: boolean): KnowMeTodo[] => {
      const next = working.map((t, j) =>
        j === idx
          ? {
              ...t,
              value,
              assignee: draftAssignee.trim(),
              note: draftNote.trim(),
              status: (markDone && value.trim() ? "done" : value.trim() ? t.status : "open") as "open" | "done",
              source: value.trim() && t.source === "human" ? "human" : t.source,
            }
          : t,
      );
      setWorking(next);
      return next;
    },
    [working, draftAssignee, draftNote],
  );

  // Unsaved-edit guard: the draft value/assignee/note differs from what's stored.
  const dirty = !!cur && (draft !== (cur.value || "") || draftAssignee !== (cur.assignee || "") || draftNote !== (cur.note || ""));
  const requestExit = useCallback(() => {
    if (dirty && !window.confirm("Discard the value you're editing? It hasn't been saved yet.")) return;
    onExit();
  }, [dirty, onExit]);

  const advance = useCallback(
    (dir: 1 | -1, markDone: boolean) => {
      if (cur) {
        if (markDone && validateField(cur, draft)) return; // block invalid save
        const next = commit(i, draft, markDone);
        onSave(next);
      }
      setI((p) => Math.min(working.length, Math.max(0, p + dir)));
    },
    [cur, draft, i, working.length, commit, onSave],
  );

  const skip = useCallback(() => setI((p) => Math.min(working.length, p + 1)), [working.length]);

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        requestExit();
      } else if (e.key === "Tab") {
        e.preventDefault();
        advance(e.shiftKey ? -1 : 1, false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, requestExit]);

  const prog = fieldProgress(working);
  const pct = prog.total ? Math.round((prog.done / prog.total) * 100) : 0;
  const ctx = cur ? fieldContext(contentByKey[cur.section_key] || "", cur) : "";
  const error = cur ? validateField(cur, draft) : "";

  return (
    <div className="flex h-full w-[24rem] shrink-0 flex-col border-l bg-white">
      {/* Header / progress */}
      <div className="border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-gray-800">✍️ {scopeSection ? "Fill section" : "Guided fill"}</span>
          <button onClick={requestExit} className="ml-auto rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100">Done</button>
        </div>
        {scopeSection && (
          <div className="mt-1 flex items-center gap-1.5 text-[11px] text-gray-500">
            <span className="truncate font-medium text-gray-700">{scopeLabel}</span>
            {onClearScope && (
              <button onClick={onClearScope} className="ml-auto shrink-0 rounded border border-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500 hover:bg-gray-50">Fill whole document</button>
            )}
          </div>
        )}
        <div className="mt-2 flex items-center gap-2">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-brand transition-all" style={{ width: `${pct}%` }} />
          </div>
          <span className="font-mono text-[11px] tabular-nums text-gray-500">{prog.done}/{prog.total}</span>
        </div>
        {prog.requiredOpen > 0 && (
          <div className="mt-1 text-[11px] text-amber-600">{prog.requiredOpen} required field{prog.requiredOpen === 1 ? "" : "s"} remaining{scopeSection ? " in this section" : ""}</div>
        )}
      </div>

      {/* Body */}
      {finished ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
          <div className="text-4xl">{prog.requiredOpen === 0 ? "🎉" : "📋"}</div>
          <div className="text-sm font-medium text-gray-700">
            {scopeSection
              ? (prog.requiredOpen === 0 ? "Section complete!" : `${prog.requiredOpen} required field(s) still open in this section`)
              : (prog.requiredOpen === 0 ? "All required fields complete!" : `${prog.requiredOpen} required field(s) still open`)}
          </div>
          <div className="text-xs text-gray-500">{prog.done}/{prog.total} fields filled · {prog.open} optional left</div>
          <div className="flex flex-wrap justify-center gap-2">
            <button onClick={() => setI(0)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">↩ Review from top</button>
            {scopeSection && onClearScope && (
              <button onClick={onClearScope} className="rounded-lg border border-brand/30 bg-brand/5 px-3 py-1.5 text-xs font-medium text-brand hover:bg-brand/10">Fill the rest →</button>
            )}
            {!scopeSection && onPublish && (
              <button
                onClick={onPublish}
                disabled={prog.requiredOpen > 0}
                className="rounded-lg bg-green-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-green-700 disabled:opacity-40"
              >
                Publish
              </button>
            )}
          </div>
        </div>
      ) : cur ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
            <div className="flex items-center gap-2">
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                {groupLabel(effectiveGroup(cur))}
              </span>
              <span className="text-[11px] text-gray-400">Field {i + 1} of {working.length}</span>
              {cur.required && <span className="rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">required</span>}
              {cur.status === "done" && <span className="text-[11px] text-emerald-600">✓ filled</span>}
            </div>
            <div className="text-sm font-semibold text-gray-800">{cur.label}</div>
            {ctx && (
              <div className="rounded-lg border border-gray-100 bg-gray-50 px-3 py-2 text-[11px] leading-relaxed text-gray-500">
                <span className="font-medium text-gray-400">In context: </span>{ctx}
              </div>
            )}
            <FieldInput
              todo={cur}
              value={draft}
              onChange={setDraft}
              onEnter={() => advance(1, true)}
              autoFocus
              onSuggest={onSuggestField ? () => onSuggestField(cur.id) : undefined}
              suggesting={suggestingId === cur.id}
            />
            {/* Optional ownership of this field: who's responsible + a note for the next person. */}
            <div>
              <button
                onClick={() => setShowMeta((v) => !v)}
                className="flex items-center gap-1 text-[11px] font-medium text-gray-500 hover:text-brand"
              >
                {showMeta ? "▾" : "▸"} Assign / add a note{(draftAssignee || draftNote) ? " • set" : ""}
              </button>
              {showMeta && (
                <div className="mt-1.5 space-y-1.5">
                  <input
                    value={draftAssignee}
                    onChange={(e) => setDraftAssignee(e.target.value)}
                    placeholder="Assignee (who should fill this in?)"
                    aria-label="Field assignee"
                    className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-[12px] focus:border-brand focus:outline-none"
                  />
                  <textarea
                    value={draftNote}
                    onChange={(e) => setDraftNote(e.target.value)}
                    placeholder="Note / request for information…"
                    aria-label="Field note"
                    rows={2}
                    className="w-full resize-y rounded-lg border border-gray-200 px-2.5 py-1.5 text-[12px] focus:border-brand focus:outline-none"
                  />
                </div>
              )}
            </div>
          </div>
          {/* Controls */}
          <div className="border-t px-4 py-3">
            <div className="flex items-center gap-2">
              <button
                onClick={() => advance(-1, false)}
                disabled={i === 0}
                className="rounded-lg border px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
              >
                ← Back
              </button>
              <button onClick={skip} className="rounded-lg border px-2.5 py-1.5 text-xs text-gray-500 hover:bg-gray-50">Skip</button>
              <button
                onClick={() => advance(1, true)}
                disabled={!!error || saving}
                className="ml-auto rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-dark disabled:opacity-40"
              >
                {saving ? "Saving…" : i === working.length - 1 ? "Save & finish →" : "Save & next →"}
              </button>
            </div>
            <div className="mt-1.5 text-center text-[10px] text-gray-400">Enter to save · Tab to move · Esc to exit</div>
          </div>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-gray-400">
          No human-completion fields — nothing to fill. 🎉
        </div>
      )}
    </div>
  );
}
