import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { DESTINATIONS, type Destination } from "../help/content";

/**
 * Command Palette — press Ctrl/⌘+K anywhere to fuzzily jump to any page or action. The single
 * biggest "feels enterprise" upgrade for an app with this many routes, and it compensates for
 * the deep navigation. Mounted once at the app root; registers the global hotkey itself.
 */
export function CommandPalette() {
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Global hotkey: Ctrl/⌘+K toggles; "/" opens when nothing is focused.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      // Focus after the modal paints.
      const t = setTimeout(() => inputRef.current?.focus(), 20);
      return () => clearTimeout(t);
    }
  }, [open]);

  const items = useMemo(() => {
    const all = DESTINATIONS.filter((d) => isAdmin || !d.adminOnly);
    const q = query.trim().toLowerCase();
    if (!q) return all;
    const tokens = q.split(/\s+/);
    return all.filter((d) => {
      const hay = `${d.label} ${d.group} ${d.keywords ?? ""}`.toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
  }, [query, isAdmin]);

  // Group while preserving order.
  const grouped = useMemo(() => {
    const out: { group: string; items: { d: Destination; index: number }[] }[] = [];
    items.forEach((d, index) => {
      let bucket = out.find((g) => g.group === d.group);
      if (!bucket) {
        bucket = { group: d.group, items: [] };
        out.push(bucket);
      }
      bucket.items.push({ d, index });
    });
    return out;
  }, [items]);

  function choose(d: Destination) {
    setOpen(false);
    navigate(d.path);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const d = items[active];
      if (d) choose(d);
    }
  }

  // Keep the active row in view.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-black/40 px-4 pt-[12vh] backdrop-blur-[1px]"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-xl overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <span className="text-gray-400">⌕</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
            placeholder="Search pages and actions…"
            className="w-full bg-transparent text-sm outline-none placeholder:text-gray-400"
            aria-label="Command palette search"
          />
          <kbd className="rounded border bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-400">Esc</kbd>
        </div>
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto py-1">
          {items.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-gray-400">No matches.</div>
          ) : (
            grouped.map((g) => (
              <div key={g.group}>
                <div className="px-4 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">{g.group}</div>
                {g.items.map(({ d, index }) => (
                  <button
                    key={d.path + d.label}
                    data-idx={index}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => choose(d)}
                    className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm ${
                      index === active ? "bg-brand/10 text-brand" : "text-gray-700 hover:bg-gray-50"
                    }`}
                  >
                    <span className="text-base" aria-hidden>{d.icon}</span>
                    <span className="min-w-0 flex-1 truncate">{d.label}</span>
                    {d.adminOnly && <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[9px] uppercase text-gray-400">admin</span>}
                  </button>
                ))}
              </div>
            ))
          )}
        </div>
        <div className="flex items-center justify-between border-t bg-gray-50 px-4 py-2 text-[11px] text-gray-400">
          <span><kbd className="rounded border bg-white px-1">↑</kbd> <kbd className="rounded border bg-white px-1">↓</kbd> to navigate · <kbd className="rounded border bg-white px-1">↵</kbd> to open</span>
          <span>Command Palette</span>
        </div>
      </div>
    </div>
  );
}
