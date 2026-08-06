/** Group + collapse state for a list screen, extracted at its third use.
 *
 * The Findings tab worked this out first and paid for it in bugs; the Disabled Access tab needs
 * exactly the same behaviour, and copying it a third time would mean the two subtle rules below
 * live in three places and get fixed in one.
 *
 * The HOOK is shared, not the rendering. Three screens disagree about how a group header should
 * look, and a shared component that all three must agree on is a component none of them can
 * safely change — the problem already flagged on `SankeyExplorer`.
 *
 * Two rules that are not obvious and are both regressions if lost:
 *
 * 1. **Auto-fold runs once per grouping, not once per render.** Without the guard ref, every
 *    background refetch (a filter change, a poll, a window focus) re-folds the section the
 *    reader just opened, which feels like the page fighting them.
 * 2. **A sub-group of one is not a group.** If every item in a section lands in a single
 *    sub-group, the sub-level is dropped: nesting a section inside itself gives the reader one
 *    more click and no more information.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePersistedState } from "./persistedState";

export type GroupedSection<T> = {
  key: string;
  label: string;
  items: T[];
  /** Server-published count for this section, when one exists. */
  total: number;
  /** True when `total` came from the loaded page rather than the server. */
  countIsFromPage: boolean;
  subGroups: { key: string; label: string; items: T[] }[] | null;
};

export type UseGroupedCollapse<T> = {
  groupBy: string;
  setGroupBy: (v: string) => void;
  subGroupBy: string;
  setSubGroupBy: (v: string) => void;
  /** `subGroupBy`, forced to "none" when it would nest a dimension inside itself. */
  effectiveSub: string;
  sections: GroupedSection<T>[] | null;
  isCollapsed: (key: string) => boolean;
  toggle: (key: string) => void;
  collapseAll: () => void;
  expandAll: () => void;
  allGroupKeys: string[];
};

export type GroupDimension<T> = {
  key: string;
  label: string;
  /** The bucket(s) this item belongs to. Returning several puts the item in each. */
  of: (item: T) => string | string[];
  /** Display label for a bucket key. Defaults to the key. */
  labelOf?: (bucket: string) => string;
  /** Server-side count map for this dimension, keyed by bucket. */
  counts?: Record<string, number>;
};

function bucketsOf<T>(dim: GroupDimension<T>, item: T): string[] {
  const raw = dim.of(item);
  const list = Array.isArray(raw) ? raw : [raw];
  const cleaned = list.filter((b) => b !== undefined && b !== null).map(String);
  // An item that belongs to no bucket still has to appear somewhere, or grouping silently
  // deletes rows — a filter disguised as a view.
  return cleaned.length > 0 ? cleaned : ["—"];
}

export function useGroupedCollapse<T>(
  items: T[],
  dimensions: GroupDimension<T>[],
  opts: { storagePrefix: string; defaultGroupBy?: string; defaultSubGroupBy?: string },
): UseGroupedCollapse<T> {
  const [groupBy, setGroupBy] = usePersistedState<string>(
    `${opts.storagePrefix}.groupBy`,
    opts.defaultGroupBy ?? "none",
  );
  const [subGroupBy, setSubGroupBy] = usePersistedState<string>(
    `${opts.storagePrefix}.subGroupBy`,
    opts.defaultSubGroupBy ?? "none",
  );
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Nesting a dimension inside itself produces one child holding everything.
  const effectiveSub = subGroupBy === groupBy ? "none" : subGroupBy;

  const byKey = useMemo(() => {
    const m = new Map<string, GroupDimension<T>>();
    dimensions.forEach((d) => m.set(d.key, d));
    return m;
  }, [dimensions]);

  const sections = useMemo<GroupedSection<T>[] | null>(() => {
    const dim = byKey.get(groupBy);
    if (!dim) return null;
    const buckets = new Map<string, T[]>();
    for (const item of items) {
      for (const b of bucketsOf(dim, item)) {
        const list = buckets.get(b);
        if (list) list.push(item);
        else buckets.set(b, [item]);
      }
    }
    const sub = effectiveSub === "none" ? null : byKey.get(effectiveSub);
    const out: GroupedSection<T>[] = [];
    for (const [key, list] of buckets) {
      let subGroups: { key: string; label: string; items: T[] }[] | null = null;
      if (sub) {
        const inner = new Map<string, T[]>();
        for (const item of list) {
          for (const b of bucketsOf(sub, item)) {
            const l = inner.get(b);
            if (l) l.push(item);
            else inner.set(b, [item]);
          }
        }
        // A sub-group of one IS the section. Drop the level rather than adding a click.
        subGroups =
          inner.size <= 1
            ? null
            : [...inner.entries()].map(([k, v]) => ({
                key: `${key}::${k}`,
                label: sub.labelOf ? sub.labelOf(k) : k,
                items: v,
              }));
        subGroups?.sort((a, b) => b.items.length - a.items.length || a.label.localeCompare(b.label));
      }
      const serverCount = dim.counts?.[key];
      out.push({
        key,
        label: dim.labelOf ? dim.labelOf(key) : key,
        items: list,
        total: serverCount ?? list.length,
        countIsFromPage: serverCount === undefined,
        subGroups,
      });
    }
    out.sort((a, b) => b.total - a.total || a.label.localeCompare(b.label));
    return out;
  }, [items, groupBy, effectiveSub, byKey]);

  const allGroupKeys = useMemo(
    () =>
      (sections ?? []).flatMap((s) => [s.key, ...(s.subGroups ?? []).map((g) => g.key)]),
    [sections],
  );

  // Which grouping the automatic collapse has already run for.
  const autoCollapsedFor = useRef<string>("");

  useEffect(() => {
    setCollapsed(new Set());
    autoCollapsedFor.current = "";
  }, [groupBy, subGroupBy]);

  useEffect(() => {
    if (groupBy === "none" || allGroupKeys.length === 0) return;
    const signature = `${groupBy}|${effectiveSub}`;
    if (autoCollapsedFor.current === signature) return;
    autoCollapsedFor.current = signature;
    setCollapsed(new Set(allGroupKeys));
  }, [allGroupKeys, groupBy, effectiveSub]);

  const toggle = useCallback((key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  return {
    groupBy,
    setGroupBy,
    subGroupBy,
    setSubGroupBy,
    effectiveSub,
    sections,
    isCollapsed: useCallback((key: string) => collapsed.has(key), [collapsed]),
    toggle,
    // Both levels, or the buttons cannot reach half the tree they appear to control.
    collapseAll: useCallback(() => setCollapsed(new Set(allGroupKeys)), [allGroupKeys]),
    expandAll: useCallback(() => setCollapsed(new Set()), []),
    allGroupKeys,
  };
}
