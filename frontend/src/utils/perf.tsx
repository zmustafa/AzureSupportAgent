// Shared performance + UX helpers (virtualized lists, debounce, skeletons) used by the heavy
// data screens (Change Explorer, Tag Intelligence). Keeping them here avoids duplicating the
// pattern per screen.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

/** Virtualized vertical list: renders only the rows in view. ``estimateSize`` is the row height. */
export function VirtualList<T>({ items, estimateSize = 40, max = "60vh", render, className = "" }: {
  items: T[]; estimateSize?: number; max?: string; render: (item: T, index: number) => ReactNode; className?: string;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virt = useVirtualizer({
    count: items.length, getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize, overscan: 12,
  });
  return (
    <div ref={parentRef} className={`overflow-auto ${className}`} style={{ maxHeight: max }}>
      <div style={{ height: virt.getTotalSize(), position: "relative" }}>
        {virt.getVirtualItems().map((vi) => (
          <div key={vi.key} ref={virt.measureElement} data-index={vi.index}
            style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}>
            {render(items[vi.index], vi.index)}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Debounce a fast-changing value (e.g. a search input) so downstream filtering/queries don't run
 *  on every keystroke. */
export function useDebounced<T>(value: T, ms = 150): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/** Lightweight skeleton block shown while a tab renders / a query is in flight. */
export function Skeleton({ rows = 6, className = "" }: { rows?: number; className?: string }) {
  return (
    <div className={`space-y-2 ${className}`}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-9 animate-pulse rounded-lg bg-gray-100" />
      ))}
    </div>
  );
}

/** A compact inline search box with a "shown / total" counter + clear button. */
export function InlineSearch({ q, setQ, shown, total, placeholder, width = "w-64" }: {
  q: string; setQ: (v: string) => void; shown: number; total: number; placeholder: string; width?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} className={`${width} rounded border px-2 py-1 text-sm`} />
      {q && <button onClick={() => setQ("")} className="text-[11px] text-gray-400 hover:text-gray-600">✕ clear</button>}
      <span className="text-[11px] text-gray-400">{shown} / {total}</span>
    </div>
  );
}
