import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import type { EntraBlocker, EntraDomainMeta, EntraMeta } from "../../api";
import { usePersistedState } from "../../utils/persistedState";

/**
 * Shared chrome for the Entra ID Support Agent.
 *
 * The governing rule for every screen in this area: render `meta` BEFORE the data. A tenant
 * where half the model could not be measured must say so at the top of the page, otherwise a
 * blind pillar reads as a clean one — the single worst failure mode this product can have.
 */

/**
 * The active sub-tab, taken from and written to the URL.
 *
 * Sub-tabs used to be component state, which meant a reload, a shared link or the browser
 * back button all dumped the reader on the first sub-tab of the section — losing the very
 * screen they were reading. They are now the third path segment: `/entra/privileged/pim`.
 *
 * A `?sub=` query parameter is still honoured once and rewritten to the path form, because
 * the legacy /identity redirects were built against it.
 */
export function useSubTabRoute<T extends string>(
  valid: readonly T[], fallback: T,
): [T, (next: T) => void] {
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();

  const parts = location.pathname.split("/").filter(Boolean);
  const tab = parts[1] ?? "";
  const fromPath = parts[2] as T | undefined;
  const fromQuery = params.get("sub") as T | null;
  const active = fromPath && valid.includes(fromPath)
    ? fromPath
    : fromQuery && valid.includes(fromQuery)
      ? fromQuery
      : fallback;

  useEffect(() => {
    if (!fromQuery) return;
    // Consume the legacy parameter by replacing it with the equivalent path, so the URL
    // stops carrying a hint it no longer needs and the address bar matches the screen.
    const rest = new URLSearchParams(params);
    rest.delete("sub");
    const search = rest.toString();
    navigate({ pathname: `/entra/${tab}/${active}`, search: search ? `?${search}` : "" },
             { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromQuery]);

  const setTab = useCallback(
    (next: T) => navigate(`/entra/${tab}/${next}`),
    [navigate, tab],
  );
  return [active, setTab];
}

export const SEV_STYLE: Record<string, { label: string; chip: string; dot: string; rank: number }> = {
  critical: { label: "Critical", chip: "bg-red-100 text-red-700", dot: "bg-red-500", rank: 4 },
  high: { label: "High", chip: "bg-orange-100 text-orange-700", dot: "bg-orange-500", rank: 3 },
  medium: { label: "Medium", chip: "bg-amber-100 text-amber-700", dot: "bg-amber-500", rank: 2 },
  low: { label: "Low", chip: "bg-sky-100 text-sky-700", dot: "bg-sky-500", rank: 1 },
  info: { label: "Info", chip: "bg-gray-100 text-gray-600", dot: "bg-gray-400", rank: 0 },
};

export function SevBadge({ sev }: { sev: string }) {
  const m = SEV_STYLE[sev] ?? SEV_STYLE.info;
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${m.chip}`}>{m.label}</span>;
}

// ============================================================================ sorting
/**
 * Column sorting for every grid in this feature.
 *
 * Written once and shared because the rules are the interesting part, not the arrows.
 * Twenty-three grids each inventing their own comparator is twenty-three chances to sort
 * `high` between `low` and `none`, or to file an unknown date under 1970.
 *
 * The rules, applied everywhere:
 *
 * 1. **Nulls last, in both directions.** "Not recorded" is not "oldest" and not "zero". A
 *    row with no value sinks to the bottom whichever way the arrow points, so flipping the
 *    sort never promotes an absence to the top of the screen.
 * 2. **Enums sort by rank, never by alphabet.** Severity, tier and workflow state all have
 *    a meaningful order that has nothing to do with spelling.
 * 3. **A new column starts descending.** The interesting end of risk, age, count and
 *    severity is the top of a descending sort; only text columns open ascending.
 * 4. **Stable.** Equal rows keep the order the server sent, so re-clicking a column never
 *    shuffles rows that the comparator says nothing about.
 */
export type SortDir = 1 | -1;
export type SortState<K extends string = string> = { key: K; dir: SortDir };

/** Rank tables for the enums that appear in more than one Entra grid. */
export const SEVERITY_RANK: Record<string, number> = {
  critical: 4, high: 3, medium: 2, low: 1, info: 0,
};
export const TIER_RANK: Record<string, number> = { tier0: 3, tier1: 2, tier2: 1 };
export const FINDING_STATE_RANK: Record<string, number> = {
  open: 4, acknowledged: 3, snoozed: 2, suppressed: 1, resolved: 0,
};
/**
 * Collector state, worst first when descending.
 *
 * Both spellings of "we collected it" are here on purpose. The API says `ok`
 * (`model.STATUS_OK`) while the pillar model and this file's own chip vocabulary say
 * `measured`; a table that ranked only one of them left every healthy domain unranked, so
 * the null rule pinned them to the bottom in BOTH directions and the column looked inert.
 * `not_implemented` is the same story from the pillar side.
 */
export const DOMAIN_STATE_RANK: Record<string, number> = {
  error: 6, blind: 5, unlicensed: 4, partial: 3, stale: 2,
  measured: 1, ok: 1,
  not_collected: 0, not_implemented: 0,
};
export const CA_STATE_RANK: Record<string, number> = {
  enabled: 3, enabledForReportingButNotEnforced: 2, disabled: 1,
};

/** Comparator kit. Each returns a value where "bigger sorts first when descending". */
export const cmp = {
  /** Text. `localeCompare` so accented names land where a human expects. */
  text(a: string | null | undefined, b: string | null | undefined): number {
    const x = (a ?? "").trim(), y = (b ?? "").trim();
    if (!x && !y) return 0;
    if (!x) return NULL_LAST;
    if (!y) return -NULL_LAST;
    return x.localeCompare(y, undefined, { sensitivity: "base" });
  },
  /** Numbers, with null/NaN pushed out of the ordering entirely. */
  num(a: number | null | undefined, b: number | null | undefined): number {
    const x = typeof a === "number" && Number.isFinite(a) ? a : null;
    const y = typeof b === "number" && Number.isFinite(b) ? b : null;
    if (x === null && y === null) return 0;
    if (x === null) return NULL_LAST;
    if (y === null) return -NULL_LAST;
    return x - y;
  },
  /** ISO timestamps. An unparseable or empty stamp is null, not the epoch. */
  date(a: string | null | undefined, b: string | null | undefined): number {
    return cmp.num(toMs(a), toMs(b));
  },
  /** Enum by rank table. A value missing from the table ranks below every known one. */
  rank(table: Record<string, number>, a: string | null | undefined, b: string | null | undefined): number {
    return cmp.num(table[String(a ?? "")] ?? null, table[String(b ?? "")] ?? null);
  },
  /** Booleans, true first when descending. `null`/`undefined` is unknown, not false. */
  bool(a: boolean | null | undefined, b: boolean | null | undefined): number {
    return cmp.num(a == null ? null : a ? 1 : 0, b == null ? null : b ? 1 : 0);
  },
};

/**
 * The sentinel that makes rule 1 work.
 *
 * `useEntraSorted` multiplies each comparison by the direction, so an ordinary "nulls are
 * small" comparator would float them to the top the moment the arrow flipped. Returning
 * this marker instead lets the sort strip the direction back off for null comparisons.
 */
const NULL_LAST = Number.MAX_SAFE_INTEGER;

function toMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : t;
}

/**
 * Sort rows by the active column, stably, with nulls pinned to the bottom.
 *
 * `compare` returns a plain comparison for the given key using the `cmp` kit above; this
 * hook owns direction, stability and the null rule so no caller has to remember them.
 */
export function useEntraSorted<T, K extends string>(
  rows: T[],
  sort: SortState<K>,
  compare: (a: T, b: T, key: K) => number,
): T[] {
  return useMemo(() => {
    const decorated = rows.map((row, i) => ({ row, i }));
    decorated.sort((a, b) => {
      const raw = compare(a.row, b.row, sort.key);
      // A null marker means "this one goes last" and must survive the direction flip.
      if (raw === NULL_LAST) return 1;
      if (raw === -NULL_LAST) return -1;
      // Stable: original order breaks every tie, so the server's ordering shows through.
      return raw * sort.dir || a.i - b.i;
    });
    return decorated.map((d) => d.row);
  }, [rows, sort.key, sort.dir, compare]);
}

/**
 * Sort state that survives a reload and a tab round-trip.
 *
 * Keyed per grid, because "sorted by age" on the inbox says nothing about what the reader
 * wanted on the applications grid.
 */
export function useSortState<K extends string>(
  gridKey: string, initial: SortState<K>,
): [SortState<K>, (next: SortState<K>) => void] {
  const [raw, setRaw] = usePersistedState<SortState<K>>(`azsup.entra.sort.${gridKey}`, initial);
  // A stored key from an older build must not leave a grid sorted by a column that no
  // longer exists — fall back rather than render an inert arrow.
  const value = raw && typeof raw.key === "string" && (raw.dir === 1 || raw.dir === -1) ? raw : initial;
  return [value, setRaw];
}

/**
 * A sortable column header.
 *
 * Renders a real button inside the `th` so it is reachable by keyboard, and sets
 * `aria-sort` so a screen reader is told what the arrow means.
 */
export function SortTh<K extends string>({
  label, col, sort, setSort, align = "left", firstDir = -1, title, className = "",
}: {
  label: string;
  col: K;
  sort: SortState<K>;
  setSort: (s: SortState<K>) => void;
  align?: "left" | "right" | "center";
  /** Direction a fresh click on this column starts with. Text columns usually want 1. */
  firstDir?: SortDir;
  title?: string;
  className?: string;
}) {
  const active = sort.key === col;
  const justify = align === "right" ? "justify-end" : align === "center" ? "justify-center" : "";
  return (
    <th aria-sort={active ? (sort.dir === -1 ? "descending" : "ascending") : "none"}
        className={`${className} whitespace-nowrap py-1.5 font-medium`}>
      <button
        type="button"
        onClick={() => setSort({ key: col, dir: active ? ((sort.dir * -1) as SortDir) : firstDir })}
        title={title || `Sort by ${label.toLowerCase()}`}
        className={`flex w-full items-center gap-1 uppercase tracking-wide hover:text-gray-800 ${justify} ${
          active ? "text-gray-800" : "text-gray-500"}`}
      >
        {label}
        <span className={active ? "text-brand" : "text-gray-300"}>
          {active ? (sort.dir === -1 ? "▼" : "▲") : "↕"}
        </span>
      </button>
    </th>
  );
}

/**
 * The sentence that stops a column header from lying.
 *
 * Where the server returned a capped page, sorting in the browser reorders THAT page and
 * nothing else. "Top by credential expiry" is then really "top by credential expiry among
 * the 500 rows the server picked by severity", which is a different claim and a worse one
 * for being invisible. Any capped grid renders this.
 */
export function SortScopeNote({ shown, total, sorted = "the loaded rows" }: {
  shown: number; total: number; sorted?: string;
}) {
  if (!total || shown >= total) return null;
  return (
    <div className="px-3 py-1.5 text-[11px] text-amber-700">
      Sorting applies to {sorted} only — {shown.toLocaleString()} of {total.toLocaleString()} rows are
      loaded. Narrow the filters to bring the rest into scope.
    </div>
  );
}


const STATE_STYLE: Record<string, { label: string; chip: string }> = {
  measured: { label: "measured", chip: "bg-green-100 text-green-700" },
  ok: { label: "measured", chip: "bg-green-100 text-green-700" },
  partial: { label: "partial", chip: "bg-amber-100 text-amber-700" },
  blind: { label: "not permitted", chip: "bg-red-100 text-red-700" },
  unlicensed: { label: "not licensed", chip: "bg-violet-100 text-violet-700" },
  error: { label: "error", chip: "bg-red-100 text-red-700" },
  stale: { label: "stale", chip: "bg-amber-100 text-amber-700" },
  not_implemented: { label: "not in this build", chip: "bg-gray-100 text-gray-500" },
  not_collected: { label: "not collected", chip: "bg-gray-100 text-gray-500" },
};

export function StateChip({ state, title }: { state: string; title?: string }) {
  const m = STATE_STYLE[state] ?? STATE_STYLE.not_collected;
  return (
    <span title={title} className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${m.chip}`}>
      {m.label}
    </span>
  );
}

export function agoText(seconds: number | null | undefined): string {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** "refreshed 14m ago" + a refresh affordance. Amber past TTL, red past 24h. */
export function FreshnessBadge({
  meta,
  onRefresh,
  refreshing,
  canRefresh = true,
}: {
  meta?: EntraMeta;
  onRefresh?: () => void;
  refreshing?: boolean;
  canRefresh?: boolean;
}) {
  const age = meta?.age_seconds ?? null;
  const tone =
    age == null ? "text-gray-500" : age > 86400 ? "text-red-600" : meta?.stale ? "text-amber-600" : "text-gray-500";
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={tone}>
        {meta?.loaded ? `refreshed ${agoText(age)}` : "not loaded"}
        {meta?.truncated ? " · partial" : ""}
      </span>
      {canRefresh && onRefresh && (
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded border px-2 py-1 text-xs font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      )}
    </div>
  );
}

/**
 * The coverage banner. Names the exact missing permission or license rather than leaving a
 * screen mysteriously empty.
 */
export function CoverageBanner({ meta, onOpenSetup }: { meta?: EntraMeta; onOpenSetup?: () => void }) {
  // Collapsed by default. This banner appears on every screen, and expanded it consumed a
  // third of the viewport above the content the operator actually came for — which trains
  // people to scroll past it, defeating the point of showing coverage at all.
  const [open, setOpen] = useState(false);
  if (!meta || !meta.loaded) return null;
  const domains = Object.values(meta.domains ?? {});
  const blind = domains.filter((d) => d.status === "blind");
  const unlicensed = domains.filter((d) => d.status === "unlicensed");
  const errored = domains.filter((d) => d.status === "error");
  const truncated = domains.filter((d) => d.truncated);
  // A "partial" domain collected fine but lost a sub-call — usually a license or permission
  // limit. Those notes are the difference between "we found nothing" and "we could not look".
  const limited = domains.filter((d) => d.status === "partial" && (d.notes ?? []).length > 0);
  const blockers = meta.blockers ?? [];
  // Domains whose limitation is already stated as a structured blocker must not ALSO have
  // their prose repeated, or the deduplication buys nothing.
  const explained = new Set(blockers.flatMap((b) => b.domains ?? []));
  const leftover = limited.filter((d) => !explained.has(d.name));
  // Only apps/ca/roles still cap without raising a blocker; those need the generic line.
  const cappedByBlocker = new Set(
    blockers.filter((b) => b.kind === "cap").flatMap((b) => b.domains ?? []));
  const uncappedExplained = truncated.filter((d) => !cappedByBlocker.has(d.name));
  if (!blind.length && !unlicensed.length && !errored.length && !truncated.length
      && !limited.length && !blockers.length) return null;

  const missing = Array.from(new Set(blind.flatMap((d) => d.missing_permissions ?? [])));
  // The collapsed headline names WHICH domains are affected, so it stays actionable
  // instead of being a vague warning nobody can act on without expanding.
  const affected = Array.from(new Set([
    ...blind.map((d) => d.name), ...unlicensed.map((d) => d.name),
    ...errored.map((d) => d.name), ...limited.map((d) => d.name),
    ...truncated.map((d) => d.name),
  ]));
  const tone = errored.length || blind.length
    ? "border-amber-300 bg-amber-50 text-amber-900"
    : "border-gray-200 bg-gray-50 text-gray-700";

  return (
    <div className={`mx-4 mt-3 rounded-lg border px-3 py-2 text-[13px] ${tone}`}>
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 text-left"
        aria-expanded={open}
      >
        <span className="text-[11px] opacity-70">{open ? "▾" : "▸"}</span>
        <span className="shrink-0 font-medium">
          {meta.coverage != null
            ? `${Math.round(meta.coverage * 100)}% of the model was measured`
            : "Some checks could not run"}
        </span>
        <span className="truncate text-xs opacity-80">— limits in {affected.join(", ")}</span>
        <span className="ml-auto shrink-0 text-xs underline underline-offset-2 opacity-80">
          {open ? "hide" : "why"}
        </span>
      </button>

      {open && (
        <>
          <ul className="mt-1.5 space-y-0.5 text-xs">
            {blind.length > 0 && (
              <li>
                <span className="font-medium">Not permitted:</span> {blind.map((d) => d.name).join(", ")}
                {missing.length > 0 && <> — missing <code className="rounded bg-amber-100 px-1">{missing.join(", ")}</code></>}
              </li>
            )}
            {/* A whole domain lost to licensing never produces a blocker — the collector
                returns an unlicensed payload and never gets far enough to raise one. Without
                this row those domains appear in the headline with nothing explaining them. */}
            {unlicensed.length > 0 && (
              <li>
                <span className="font-medium">Not licensed:</span> {unlicensed.map((d) => `${d.name} (${d.error})`).join("; ")}
              </li>
            )}
            {errored.map((d) => (
              <li key={d.name}>
                <span className="font-medium">Collection failed:</span> {d.name} — {d.error}
              </li>
            ))}
          </ul>
          {/* Structured blockers replace the per-domain prose: one row per obstacle, naming
              every domain it affects, so a single missing permission is not repeated once
              per domain that wanted it. */}
          <BlockerList blockers={blockers} />
          {leftover.length > 0 && (
            <ul className="mt-1.5 space-y-0.5 text-xs">
              {leftover.map((d) => (
                <li key={d.name}>
                  <span className="font-medium">{d.name}:</span> {(d.notes ?? []).join(" · ")}
                </li>
              ))}
            </ul>
          )}
          {/* Not every collector raises a cap blocker yet. Anything capped without one still
              has to say so, or a truncated grid silently reads as a complete one. */}
          {uncappedExplained.length > 0 && (
            <ul className="mt-1.5 space-y-0.5 text-xs">
              <li>
                <span className="font-medium">Capped:</span>{" "}
                {uncappedExplained.map((d) => d.name).join(", ")} — counts are a lower bound.
              </li>
            </ul>
          )}
          {onOpenSetup && (
            <button onClick={onOpenSetup} className="mt-1.5 text-xs font-medium underline underline-offset-2">
              How to fix the blind spots →
            </button>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Four distinct empty states. A generic "no data" message is how a blind screen gets
 * mistaken for a clean one.
 */
export function EntraEmpty({
  kind,
  detail,
  onRefresh,
  onOpenSetup,
  checked,
}: {
  kind: "cold" | "blind" | "unlicensed" | "clean";
  detail?: string;
  onRefresh?: () => void;
  onOpenSetup?: () => void;
  checked?: string;
}) {
  const copy = {
    cold: { icon: "⏳", title: "Not loaded yet", body: detail || "Nothing has been collected for this tenant yet." },
    blind: { icon: "🔒", title: "Not measured", body: detail || "The connection lacks the Microsoft Graph permission this needs." },
    unlicensed: { icon: "💠", title: "Not measured", body: detail || "This requires a higher Entra ID license tier." },
    clean: { icon: "✅", title: "Nothing to report", body: detail || "No findings." },
  }[kind];
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center">
      <div className="text-3xl">{copy.icon}</div>
      <div className="mt-2 text-sm font-semibold text-gray-800">{copy.title}</div>
      <div className="mt-1 max-w-md text-[13px] text-gray-500">{copy.body}</div>
      {kind === "clean" && checked && (
        <div className="mt-2 max-w-md text-xs text-gray-400">Checked: {checked}</div>
      )}
      <div className="mt-3 flex gap-2">
        {kind === "cold" && onRefresh && (
          <button onClick={onRefresh} className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white">
            Refresh now
          </button>
        )}
        {(kind === "blind" || kind === "unlicensed") && onOpenSetup && (
          <button onClick={onOpenSetup} className="rounded border px-3 py-1.5 text-sm font-medium text-gray-700">
            Setup &amp; coverage
          </button>
        )}
      </div>
    </div>
  );
}

/** Deterministic SVG gauge — the score is a computed number, never an animation. */
export function ScoreRing({ score, coverage }: { score: number; coverage: number }) {
  const r = 46;
  const circ = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, score)) / 100;
  const colour = score >= 90 ? "#16a34a" : score >= 75 ? "#65a30d" : score >= 60 ? "#d97706" : score >= 40 ? "#ea580c" : "#dc2626";
  return (
    <svg viewBox="0 0 120 120" className="h-28 w-28" role="img" aria-label={`Identity posture ${score} out of 100`}>
      <circle cx="60" cy="60" r={r} fill="none" stroke="#e5e7eb" strokeWidth="10" />
      <circle
        cx="60" cy="60" r={r} fill="none" stroke={colour} strokeWidth="10" strokeLinecap="round"
        strokeDasharray={`${circ * pct} ${circ}`} transform="rotate(-90 60 60)"
      />
      <text x="60" y="58" textAnchor="middle" className="fill-gray-900" style={{ fontSize: 26, fontWeight: 700 }}>
        {score}
      </text>
      <text x="60" y="76" textAnchor="middle" className="fill-gray-400" style={{ fontSize: 11 }}>
        {Math.round(coverage * 100)}% measured
      </text>
    </svg>
  );
}

export function Bar({ value, max = 100, tone = "bg-brand" }: { value: number; max?: number; tone?: string }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-gray-200">
      <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

/** One dated thing on a timeline. `hot` is whatever deserves the red segment on that screen. */
export type EntraTimePoint = { t: number; hot?: boolean };

/**
 * A small set of mutually exclusive choices, shown as buttons rather than a dropdown.
 *
 * Worth the pixels when the options are few and the reader switches between them
 * repeatedly: a select hides two of three choices behind a click and gives no sense that
 * the other views exist. Above roughly four options this stops being an improvement and a
 * select is the right control again.
 *
 * Matches the segmented control already used by Alerts Manager, including `aria-pressed`
 * so the active choice is announced rather than merely colored.
 */
export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: { value: T; label: string; title?: string }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border" role="group" aria-label={label}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            title={o.title || o.label}
            onClick={() => onChange(o.value)}
            className={`px-2.5 py-1 text-[12px] transition ${
              active ? "bg-brand font-medium text-white" : "bg-white text-gray-600 hover:bg-gray-50"}`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;
const PRESETS: [string, number][] = [
  ["24h", DAY_MS], ["7d", 7 * DAY_MS], ["30d", 30 * DAY_MS],
  ["90d", 90 * DAY_MS], ["1y", 365 * DAY_MS],
];

/**
 * Brushable time window — histogram, selected band, dual handles, presets.
 *
 * Shared deliberately. Every Entra list that carries a date wants the same question answered
 * ("where do these cluster, and show me only that"), and the two that grew their own version
 * of it drifted immediately: one binned by calendar day over a truncated slice, the other by
 * span. One instrument, one set of semantics, both screens.
 *
 * The caller filters its own rows from `value` — this component owns no data, only the window.
 */
export function EntraTimeWindow({
  points, value, onChange, shownCount, label, unit = "item", hotLabel, footnote,
}: {
  points: EntraTimePoint[];
  value: [number, number] | null;
  onChange: (v: [number, number] | null) => void;
  shownCount: number;
  label: string;
  unit?: string;
  hotLabel?: string;
  footnote?: string;
}) {
  const N = 48;
  const { minTs, maxTs, buckets } = useMemo(() => {
    const ts = points.filter((p) => Number.isFinite(p.t));
    if (!ts.length) return { minTs: 0, maxTs: 0, buckets: [] as { all: number; hot: number }[] };
    const lo = Math.min(...ts.map((p) => p.t));
    const hi = Math.max(...ts.map((p) => p.t));
    const span = Math.max(1, hi - lo);
    const b = Array.from({ length: N }, () => ({ all: 0, hot: 0 }));
    for (const p of ts) {
      const i = Math.min(N - 1, Math.floor(((p.t - lo) / span) * N));
      b[i].all++;
      if (p.hot) b[i].hot++;
    }
    return { minTs: lo, maxTs: hi, buckets: b };
  }, [points]);

  if (!minTs || minTs === maxTs) return null;

  const lo = value ? value[0] : minTs;
  const hi = value ? value[1] : maxTs;
  const maxBar = Math.max(1, ...buckets.map((b) => b.all));
  const pct = (t: number) => ((t - minTs) / Math.max(1, maxTs - minTs)) * 100;
  const bucketMs = (maxTs - minTs) / N;
  // Adaptive step so the handles move smoothly whether the set spans a day or three years.
  const step = Math.max(60_000, Math.floor((maxTs - minTs) / 500));
  // Long spans read as noise with a clock on them; short ones need the clock.
  const long = maxTs - minTs > 30 * DAY_MS;
  const fmt = (t: number) => long
    ? new Date(t).toLocaleDateString()
    : new Date(t).toLocaleString();

  const setLo = (t: number) => onChange([Math.min(t, hi), hi]);
  const setHi = (t: number) => onChange([lo, Math.max(t, lo)]);

  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
        <span className="font-medium text-gray-700">📅 {label}</span>
        <span className="tabular-nums">{fmt(lo)} → {fmt(hi)}</span>
        <span className="rounded-full bg-brand/10 px-2 py-0.5 font-medium text-brand">
          {shownCount.toLocaleString()} shown
        </span>
        <div className="ml-auto flex items-center gap-1">
          {PRESETS
            // A preset wider than the data selects everything and reads as a broken button.
            .filter(([, ms]) => maxTs - ms > minTs && ms >= HOUR_MS)
            .map(([text, ms]) => (
              <button key={text} onClick={() => onChange([maxTs - ms, maxTs])}
                      title={`The last ${text} of the loaded range`}
                      className="rounded border px-1.5 py-0.5 hover:bg-gray-50">{text}</button>
            ))}
          <button onClick={() => onChange(null)}
                  className="rounded border px-1.5 py-0.5 hover:bg-gray-50">All</button>
        </div>
      </div>

      {/* histogram — click a column to brush to it */}
      <div className="relative h-12 w-full">
        <div className="flex h-full w-full items-end gap-px">
          {buckets.map((b, i) => {
            const t = minTs + i * bucketMs;
            const tEnd = t + bucketMs;
            const inRange = tEnd >= lo && t <= hi;
            const hotShare = b.all ? (b.hot / b.all) * 100 : 0;
            const tip = `${fmt(t)} → ${fmt(tEnd)}\n${b.all} ${unit}${b.all === 1 ? "" : "s"}`
              + (b.hot && hotLabel ? `, ${b.hot} ${hotLabel}` : "");
            return (
              <button
                key={i}
                title={tip}
                disabled={!b.all}
                onClick={() => onChange([t, tEnd])}
                className="group flex h-full flex-1 flex-col justify-end disabled:cursor-default"
              >
                <div className="w-full overflow-hidden rounded-t"
                     style={{ height: `${(b.all / maxBar) * 100}%`,
                              minHeight: b.all > 0 ? "2px" : undefined }}>
                  <div className={inRange ? "bg-red-400" : "bg-red-200"}
                       style={{ height: `${hotShare}%` }} />
                  <div className={inRange
                         ? "bg-brand/60 group-hover:bg-brand"
                         : "bg-gray-200 group-hover:bg-gray-300"}
                       style={{ height: `${100 - hotShare}%` }} />
                </div>
              </button>
            );
          })}
        </div>
        <div className="pointer-events-none absolute inset-y-0 rounded bg-brand/10"
             style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
      </div>

      {/* dual range */}
      <div className="relative mt-1 h-4">
        <div className="pointer-events-none absolute top-1/2 h-1 w-full -translate-y-1/2 rounded-full bg-gray-200" />
        <div className="pointer-events-none absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-brand"
             style={{ left: `${pct(lo)}%`, right: `${100 - pct(hi)}%` }} />
        <input type="range" min={minTs} max={maxTs} step={step} value={lo}
               aria-label={`${label} start`}
               onChange={(e) => setLo(Number(e.target.value))}
               className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
        <input type="range" min={minTs} max={maxTs} step={step} value={hi}
               aria-label={`${label} end`}
               onChange={(e) => setHi(Number(e.target.value))}
               className="quota-range pointer-events-none absolute top-0 h-4 w-full appearance-none bg-transparent" />
      </div>

      <div className="mt-1 flex items-center justify-between text-[10px] text-gray-400">
        <span>{new Date(minTs).toISOString().slice(0, 10)}</span>
        <span>{hotLabel ? `${hotLabel} shown in red` : footnote ?? ""}</span>
        <span>{new Date(maxTs).toISOString().slice(0, 10)}</span>
      </div>
    </div>
  );
}

export function domainNote(d: EntraDomainMeta): string {
  if (d.status === "blind") return `Missing ${(d.missing_permissions ?? []).join(", ") || "a Graph permission"}`;
  if (d.status === "unlicensed" || d.status === "error") return d.error;
  // The collector's own notes come FIRST. Returning the generic capped sentence before
  // reaching them threw away every specific thing a truncated domain had to say — the risk
  // domain knew it had stopped at 200,000 sign-in events and reported "counts are a lower
  // bound" instead, which is the one row on the page a reader could do nothing with.
  const notes = (d.notes ?? []).join(" · ");
  if (d.truncated) {
    return notes ? `${notes} · Counts are a lower bound.` : "Result was capped — counts are a lower bound.";
  }
  return notes;
}

// How each kind of obstacle is introduced, in the order the reader should act. Splitting
// them is the point: "grant this" and "buy this" and "this is a deliberate limit" were all
// rendered as the same amber prose, so nothing could be triaged.
const BLOCKER_META: Record<string, { label: string; chip: string; verb: string }> = {
  consent: { label: "Needs consent", chip: "bg-amber-100 text-amber-800", verb: "Grant" },
  azure_role: { label: "Needs an Azure role", chip: "bg-orange-100 text-orange-800", verb: "Assign" },
  // The `license` KEY is the blocker kind the backend emits; only the label is user-visible.
  licence: { label: "Needs a license", chip: "bg-violet-100 text-violet-800", verb: "Requires" },
  cap: { label: "Deliberate limit", chip: "bg-sky-100 text-sky-800", verb: "Capped at" },
};

export function BlockerList({ blockers }: { blockers: EntraBlocker[] }) {
  if (!blockers.length) return null;
  return (
    <ul className="mt-1.5 space-y-1 text-xs">
      {blockers.map((b, i) => {
        const meta = BLOCKER_META[b.kind] ?? BLOCKER_META.cap;
        return (
          <li key={`${b.kind}-${b.scope}-${i}`} className="flex flex-wrap items-baseline gap-x-1.5">
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${meta.chip}`}>
              {meta.label}
            </span>
            {b.scope && (
              <code className="rounded bg-white/70 px-1 text-[11px]">{b.scope}</code>
            )}
            {b.subject && <span className="text-[11px] opacity-80">on {b.subject}</span>}
            <span>{b.text}</span>
            {b.impact && <span className="opacity-80">{b.impact}</span>}
            {b.domains?.length ? (
              <span className="opacity-70">Affects {b.domains.join(", ")}.</span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
