// A typed Know-Me field input: the control varies by field type AND by whether the field
// carries a *choice set* — a segmented button row (small strict sets like Yes/No or
// Critical/High/Medium/Low), a combobox (typeahead + free text for pickers like
// subscriptions / regions), or the plain typed input. Also offers inline validation,
// one-click suggestions, and an on-demand "✨ Suggest options" (AI) action.
import { useEffect, useMemo, useRef, useState } from "react";
import type { KnowMeTodo } from "../../api";
import { FIELD_META, fieldControl, htmlInputType, optionAccent, validateField } from "./fields";

export function FieldInput({
  todo,
  value,
  onChange,
  onEnter,
  autoFocus,
  onSuggest,
  suggesting,
}: {
  todo: KnowMeTodo;
  value: string;
  onChange: (v: string) => void;
  onEnter?: () => void;
  autoFocus?: boolean;
  // Optional AI "suggest options" action (P3). When provided, a button is shown for fields
  // that don't already have a useful choice set.
  onSuggest?: () => void;
  suggesting?: boolean;
}) {
  const ref = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const meta = FIELD_META[todo.type] ?? FIELD_META.text;
  const error = validateField(todo, value);
  const control = fieldControl(todo);
  useEffect(() => {
    if (autoFocus && ref.current && control !== "combobox") {
      ref.current.focus();
      ref.current.select?.();
    }
  }, [autoFocus, todo.id, control]);

  const common = {
    value,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(e.target.value),
    placeholder: meta.placeholder,
    className:
      "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none " +
      (error ? "border-red-300 focus:border-red-400" : "border-gray-300 focus:border-brand"),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey && onEnter) {
        e.preventDefault();
        onEnter();
      }
    },
  };

  // Show the AI-suggest button when a field has no choice set yet and isn't already
  // platform/AI-sourced (those are already useful).
  const canSuggest =
    !!onSuggest && todo.choice_source !== "platform" && todo.choice_source !== "ai" && (todo.choices?.length ?? 0) === 0;

  return (
    <div>
      {control === "segmented" ? (
        <SegmentedChoice todo={todo} value={value} onChange={onChange} />
      ) : control === "combobox" ? (
        <Combobox todo={todo} value={value} onChange={onChange} onEnter={onEnter} error={!!error} autoFocus={autoFocus} inputRef={ref as React.RefObject<HTMLInputElement>} />
      ) : todo.type === "group" || todo.type === "person" ? (
        <textarea ref={ref as React.RefObject<HTMLTextAreaElement>} rows={todo.type === "group" ? 2 : 1} {...common} />
      ) : (
        <input ref={ref as React.RefObject<HTMLInputElement>} type={htmlInputType(todo.type)} {...common} />
      )}

      {/* Legacy one-click suggestions (kept for plain inputs that only carry suggestions). */}
      {control === "input" && (todo.suggestions?.length ?? 0) > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-gray-400">Suggestions</span>
          {todo.suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onChange(s)}
              className="rounded-full border border-brand/30 bg-brand/5 px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="mt-1.5 flex items-center gap-2">
        {todo.choices?.length ? (
          <span className="text-[10px] uppercase tracking-wide text-gray-300">
            {todo.choice_source === "platform" ? "from your Azure scope" : todo.choice_source === "ai" ? "AI-suggested · editable" : todo.allow_custom === false ? "pick one" : "pick or type"}
          </span>
        ) : null}
        {canSuggest && (
          <button
            type="button"
            onClick={onSuggest}
            disabled={suggesting}
            className="ml-auto inline-flex items-center gap-1 rounded-md border border-violet-200 bg-violet-50 px-2 py-0.5 text-[11px] font-medium text-violet-700 hover:bg-violet-100 disabled:opacity-50"
            title="Let AI suggest realistic options for this field"
          >
            {suggesting ? <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-violet-500 border-t-transparent" /> : "✨"}
            {suggesting ? "Suggesting…" : "Suggest options"}
          </button>
        )}
      </div>

      {error && value.trim() && <div className="mt-1 text-[11px] text-red-600">{error}</div>}
    </div>
  );
}

/** A small strict set rendered as a row of segmented buttons (Yes/No, criticality…). */
function SegmentedChoice({ todo, value, onChange }: { todo: KnowMeTodo; value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {(todo.choices ?? []).map((opt) => {
        const active = value.trim().toLowerCase() === opt.trim().toLowerCase();
        return (
          <button
            key={opt}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(active ? "" : opt)}
            className={
              "rounded-lg border px-3 py-1.5 text-sm font-medium transition " +
              (active ? optionAccent(opt) + " ring-1 ring-inset" : "border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50")
            }
          >
            {opt}
          </button>
        );
      })}
    </div>
  );
}

/** A combobox: a text input that filters the choice list; clicking an option fills it.
 *  Free text is allowed when ``allow_custom`` (the default for platform/AI picker sets). */
function Combobox({
  todo,
  value,
  onChange,
  onEnter,
  error,
  autoFocus,
  inputRef,
}: {
  todo: KnowMeTodo;
  value: string;
  onChange: (v: string) => void;
  onEnter?: () => void;
  error: boolean;
  autoFocus?: boolean;
  inputRef: React.RefObject<HTMLInputElement>;
}) {
  const [open, setOpen] = useState(false);
  const choices = todo.choices ?? [];
  const allowCustom = todo.allow_custom !== false;
  const filtered = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q) return choices;
    return choices.filter((c) => c.toLowerCase().includes(q));
  }, [choices, value]);
  useEffect(() => {
    if (autoFocus && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [autoFocus, todo.id, inputRef]);

  const exactMatch = choices.some((c) => c.toLowerCase() === value.trim().toLowerCase());

  return (
    <div className="relative">
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        placeholder={allowCustom ? "Pick an option or type your own…" : "Pick an option…"}
        aria-label={todo.label}
        className={
          "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none " +
          (error ? "border-red-300 focus:border-red-400" : "border-gray-300 focus:border-brand")
        }
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && onEnter) { e.preventDefault(); setOpen(false); onEnter(); }
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && (filtered.length > 0 || (allowCustom && value.trim() && !exactMatch)) && (
        <div className="absolute z-30 mt-1 max-h-56 w-full overflow-auto rounded-lg border bg-white py-1 shadow-lg">
          {filtered.map((c) => (
            <button
              key={c}
              type="button"
              onMouseDown={(e) => { e.preventDefault(); onChange(c); setOpen(false); }}
              className={"block w-full px-3 py-1.5 text-left text-sm hover:bg-brand/5 " + (c.toLowerCase() === value.trim().toLowerCase() ? "bg-brand/5 font-medium text-brand" : "text-gray-700")}
            >
              {c}
            </button>
          ))}
          {allowCustom && value.trim() && !exactMatch && (
            <button
              type="button"
              onMouseDown={(e) => { e.preventDefault(); setOpen(false); }}
              className="block w-full border-t px-3 py-1.5 text-left text-[12px] text-gray-500 hover:bg-gray-50"
            >
              ＋ Use “{value.trim()}”
            </button>
          )}
        </div>
      )}
    </div>
  );
}
