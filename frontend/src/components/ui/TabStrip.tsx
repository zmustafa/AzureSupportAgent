import { useEffect, useRef, useState, type ReactNode } from "react";

export type TabItem<T extends string = string> = { id: T; label: ReactNode; title?: string };

/**
 * Overflow-safe horizontal tab strip. When the tabs are wider than the container it hides the
 * native scrollbar and shows subtle chevron buttons instead of clipping the last tab under a
 * scrollbar (fixes the Policy "…Exemptions" → "Exe…" clipping). Tabs never get truncated.
 */
export function TabStrip<T extends string>({
  tabs,
  active,
  onChange,
  className = "",
}: {
  tabs: TabItem<T>[];
  active: T;
  onChange: (id: T) => void;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [ov, setOv] = useState({ left: false, right: false });

  function measure() {
    const el = ref.current;
    if (!el) return;
    setOv({
      left: el.scrollLeft > 2,
      right: el.scrollLeft + el.clientWidth < el.scrollWidth - 2,
    });
  }

  useEffect(() => {
    measure();
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.length]);

  const nudge = (dx: number) => ref.current?.scrollBy({ left: dx, behavior: "smooth" });

  return (
    <div className={`relative ${className}`}>
      {ov.left && (
        <button
          type="button"
          aria-label="Scroll tabs left"
          onClick={() => nudge(-220)}
          className="absolute left-0 top-1/2 z-10 -translate-y-1/2 rounded-full border bg-white/95 px-1.5 py-0.5 text-gray-500 shadow-sm hover:text-gray-800"
        >
          ‹
        </button>
      )}
      <div
        ref={ref}
        role="tablist"
        onScroll={measure}
        className="no-scrollbar flex gap-1 overflow-x-auto scroll-smooth whitespace-nowrap"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={active === t.id}
            title={t.title}
            onClick={() => onChange(t.id)}
            className={`shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
              active === t.id ? "bg-brand text-white" : "text-gray-600 hover:bg-gray-100"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {ov.right && (
        <button
          type="button"
          aria-label="Scroll tabs right"
          onClick={() => nudge(220)}
          className="absolute right-0 top-1/2 z-10 -translate-y-1/2 rounded-full border bg-white/95 px-1.5 py-0.5 text-gray-500 shadow-sm hover:text-gray-800"
        >
          ›
        </button>
      )}
    </div>
  );
}
