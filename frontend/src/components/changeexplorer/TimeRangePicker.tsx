/**
 * Time-range picker for the Workload Change Explorer.
 *
 * A single trigger button opens a popover with the familiar Splunk tabs:
 *   • Presets      — Last 15/60 min, 4/24 h, 7/30 d, plus snap presets (Today, Yesterday,
 *                    Week/Month to date, Previous week/month).
 *   • Relative     — "Last N <minutes|hours|days|weeks|months>".
 *   • Date Range   — Between / Since / Before, with date+time inputs.
 *   • Advanced     — raw relative modifiers (earliest/latest) like `-7d@d`, `-24h`, `now`.
 *
 * The backend's analyze takes ABSOLUTE start/end times, so every mode is resolved to a concrete
 * datetime-local pair at Apply (anchored to "now"); a human label is returned alongside for the
 * trigger button. Output strings are `YYYY-MM-DDTHH:mm` (datetime-local), matching the existing
 * `toIso(start)` / `toIso(end)` plumbing.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

type Props = {
  start: string;            // current datetime-local
  end: string;              // current datetime-local
  label?: string;           // human label of the current selection (e.g. "Last 24 hours")
  onApply: (startLocal: string, endLocal: string, label: string) => void;
  disabled?: boolean;
};

const UNIT_MS: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };

function pad(n: number): string { return String(n).padStart(2, "0"); }
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toUtcInput(d: Date): string {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
type Tz = "local" | "utc";
/** Format an absolute Date as a `datetime-local` string in the chosen zone. */
function fmtInput(d: Date, tz: Tz): string { return tz === "utc" ? toUtcInput(d) : toLocalInput(d); }
/** Parse a `datetime-local` string as an absolute Date, interpreting it in the chosen zone. */
function parseInput(s: string, tz: Tz): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
  if (!m) return new Date(NaN);
  const Y = +m[1], Mo = +m[2] - 1, D = +m[3], H = +m[4], Mi = +m[5];
  return tz === "utc" ? new Date(Date.UTC(Y, Mo, D, H, Mi)) : new Date(Y, Mo, D, H, Mi);
}
/** Human label for an absolute Date in the chosen zone (e.g. "Jun 22, 06:32 PM"). */
function fmtAbsDate(d: Date, tz: Tz): string {
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    ...(tz === "utc" ? { timeZone: "UTC" } : {}),
  });
}
function fmtAbs(local: string): string {
  const d = new Date(local);
  return isNaN(d.getTime()) ? "—" : fmtAbsDate(d, "local");
}
/** The browser's UTC offset as a compact label, e.g. "UTC+5" or "UTC-4:30". */
function localOffsetLabel(): string {
  const o = -new Date().getTimezoneOffset();
  const sign = o >= 0 ? "+" : "-";
  const a = Math.abs(o);
  const h = Math.floor(a / 60), m = a % 60;
  return `UTC${sign}${h}${m ? ":" + pad(m) : ""}`;
}
function addMonths(d: Date, n: number): Date { const x = new Date(d); x.setMonth(x.getMonth() + n); return x; }
function snap(d: Date, unit: string): Date {
  const x = new Date(d);
  switch (unit) {
    case "mon": x.setDate(1); x.setHours(0, 0, 0, 0); break;
    case "w": x.setDate(x.getDate() - x.getDay()); x.setHours(0, 0, 0, 0); break;   // week starts Sunday (Splunk w0)
    case "d": x.setHours(0, 0, 0, 0); break;
    case "h": x.setMinutes(0, 0, 0); break;
    case "m": x.setSeconds(0, 0); break;
    case "s": x.setMilliseconds(0); break;
  }
  return x;
}

/** Parse a Splunk-style relative token (e.g. `-7d@d`, `-24h`, `@mon`, `now`) to an absolute Date. */
export function parseRel(token: string, now: Date): Date | null {
  const t = token.trim().toLowerCase();
  if (!t) return null;
  if (t === "now") return new Date(now);
  let d = new Date(now);
  let rest = t;
  const off = rest.match(/^([+-]\d+)(mon|[smhdw])/);
  if (off) {
    const n = parseInt(off[1], 10);
    const u = off[2];
    d = u === "mon" ? addMonths(d, n) : new Date(d.getTime() + n * UNIT_MS[u]);
    rest = rest.slice(off[0].length);
  }
  const sn = rest.match(/^@(mon|[smhdw])$/);
  if (sn) { d = snap(d, sn[1]); rest = ""; }
  else if (rest !== "") return null;
  if (!off && !sn) return null;
  return d;
}

const TABS = ["Presets", "Relative", "Date Range", "Advanced"] as const;
type Tab = typeof TABS[number];

const REL_UNITS: { label: string; unit: string }[] = [
  { label: "Minutes", unit: "m" }, { label: "Hours", unit: "h" },
  { label: "Days", unit: "d" }, { label: "Weeks", unit: "w" }, { label: "Months", unit: "mon" },
];

export function TimeRangePicker({ start, end, label, onApply, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("Presets");
  const [tz, setTz] = useState<Tz>("local");
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const [shiftX, setShiftX] = useState(0);

  // Relative builder state.
  const [relN, setRelN] = useState(24);
  const [relUnit, setRelUnit] = useState("h");
  // Date Range state.
  const [drMode, setDrMode] = useState<"between" | "since" | "before">("between");
  const [drStart, setDrStart] = useState(start);
  const [drEnd, setDrEnd] = useState(end);
  // Advanced tokens.
  const [advEarliest, setAdvEarliest] = useState("-24h");
  const [advLatest, setAdvLatest] = useState("now");

  // Switch the Date Range inputs between local/UTC, preserving the same absolute moment.
  function switchTz(next: Tz) {
    if (next === tz) return;
    const sAbs = parseInput(drStart, tz), eAbs = parseInput(drEnd, tz);
    if (!isNaN(sAbs.getTime())) setDrStart(fmtInput(sAbs, next));
    if (!isNaN(eAbs.getTime())) setDrEnd(fmtInput(eAbs, next));
    setTz(next);
  }

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Keep the popover within the viewport regardless of where the trigger sits (left or right of
  // a toolbar). Measure at its natural position, then shift horizontally to clear either edge.
  useLayoutEffect(() => {
    if (!open) { setShiftX(0); return; }
    const el = popRef.current;
    if (!el) return;
    el.style.transform = "";
    const rect = el.getBoundingClientRect();
    const margin = 8;
    let dx = 0;
    if (rect.right > window.innerWidth - margin) dx = (window.innerWidth - margin) - rect.right;
    if (rect.left + dx < margin) dx = margin - rect.left;
    setShiftX(dx);
  }, [open, tab]);

  const apply = (s: Date, e: Date, lbl: string) => { onApply(toLocalInput(s), toLocalInput(e), lbl); setOpen(false); };

  const presets: { label: string; run: () => void }[] = useMemo(() => {
    const mk = (lbl: string, fn: () => [Date, Date]) => ({ label: lbl, run: () => { const [s, e] = fn(); apply(s, e, lbl); } });
    const rel = (lbl: string, n: number, u: string) => mk(lbl, () => {
      const now = new Date();
      const s = u === "mon" ? addMonths(now, -n) : new Date(now.getTime() - n * UNIT_MS[u]);
      return [s, now];
    });
    return [
      rel("Last 15 minutes", 15, "m"), rel("Last 60 minutes", 60, "m"),
      rel("Last 4 hours", 4, "h"), rel("Last 24 hours", 24, "h"),
      rel("Last 7 days", 7, "d"), rel("Last 30 days", 30, "d"),
      mk("Today", () => { const now = new Date(); return [snap(now, "d"), now]; }),
      mk("Yesterday", () => { const sod = snap(new Date(), "d"); return [new Date(sod.getTime() - UNIT_MS.d), sod]; }),
      mk("Week to date", () => { const now = new Date(); return [snap(now, "w"), now]; }),
      mk("Month to date", () => { const now = new Date(); return [snap(now, "mon"), now]; }),
      mk("Previous week", () => { const sow = snap(new Date(), "w"); return [new Date(sow.getTime() - UNIT_MS.w), sow]; }),
      mk("Previous month", () => { const som = snap(new Date(), "mon"); return [snap(new Date(som.getTime() - UNIT_MS.d), "mon"), som]; }),
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const advPreview = useMemo(() => {
    const now = new Date();
    const s = parseRel(advEarliest, now), e = parseRel(advLatest, now);
    return { s, e, ok: !!(s && e && s.getTime() < e.getTime()) };
  }, [advEarliest, advLatest]);

  const triggerLabel = label || (start && end ? `${fmtAbs(start)} → ${fmtAbs(end)}` : "Pick a time range");
  const tzShort = tz === "utc" ? "UTC" : localOffsetLabel();

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          // Seed the Date Range inputs from the current selection, shown in the active zone.
          const sAbs = new Date(start), eAbs = new Date(end);
          if (!isNaN(sAbs.getTime())) setDrStart(fmtInput(sAbs, tz));
          if (!isNaN(eAbs.getTime())) setDrEnd(fmtInput(eAbs, tz));
          setOpen((o) => !o);
        }}
        className="flex items-center gap-2 rounded border bg-white px-2.5 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        title="Choose a time range"
      >
        <span>🕒</span>
        <span className="max-w-[16rem] truncate">{triggerLabel}</span>
        <span className="rounded bg-gray-100 px-1 text-[10px] font-medium text-gray-500">{tzShort}</span>
        <span className="text-gray-400">▾</span>
      </button>

      {open && (
        <div ref={popRef} style={{ transform: shiftX ? `translateX(${shiftX}px)` : undefined }} className="absolute left-0 z-50 mt-1 flex w-[32rem] max-w-[calc(100vw-1rem)] overflow-hidden rounded-xl border bg-white shadow-xl">
          {/* Tab rail */}
          <div className="w-32 shrink-0 border-r bg-gray-50 py-1">
            {TABS.map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`block w-full px-3 py-2 text-left text-sm ${tab === t ? "bg-white font-medium text-gray-900" : "text-gray-600 hover:bg-gray-100"}`}
              >{t}</button>
            ))}
          </div>

          {/* Panel */}
          <div className="min-w-0 flex-1 p-3">
            {/* Zone toggle — applies to how the Date Range inputs are read/shown + the labels. */}
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[11px] text-gray-400">Times shown in {tz === "utc" ? "UTC" : `local (${localOffsetLabel()})`}</span>
              <div className="flex overflow-hidden rounded-md border text-[11px]">
                <button onClick={() => switchTz("local")} className={`px-2 py-0.5 ${tz === "local" ? "bg-brand/10 font-medium text-brand" : "text-gray-500 hover:bg-gray-50"}`}>Local</button>
                <button onClick={() => switchTz("utc")} className={`border-l px-2 py-0.5 ${tz === "utc" ? "bg-brand/10 font-medium text-brand" : "text-gray-500 hover:bg-gray-50"}`}>UTC</button>
              </div>
            </div>

            {tab === "Presets" && (
              <div className="grid grid-cols-2 gap-1">
                {presets.map((p) => (
                  <button key={p.label} onClick={p.run} className="rounded px-2 py-1.5 text-left text-sm text-gray-700 hover:bg-brand/5 hover:text-brand">{p.label}</button>
                ))}
              </div>
            )}

            {tab === "Relative" && (
              <div>
                <div className="text-xs text-gray-500">Earliest</div>
                <div className="mt-1 flex items-center gap-2">
                  <span className="text-sm text-gray-600">Last</span>
                  <input type="number" min={1} value={relN} onChange={(e) => setRelN(Math.max(1, Number(e.target.value) || 1))} className="w-20 rounded border px-2 py-1 text-sm" />
                  <select value={relUnit} onChange={(e) => setRelUnit(e.target.value)} className="rounded border px-2 py-1 text-sm">
                    {REL_UNITS.map((u) => <option key={u.unit} value={u.unit}>{u.label}</option>)}
                  </select>
                </div>
                <div className="mt-3">
                  <button
                    onClick={() => {
                      const now = new Date();
                      const s = relUnit === "mon" ? addMonths(now, -relN) : new Date(now.getTime() - relN * UNIT_MS[relUnit]);
                      const uLbl = REL_UNITS.find((u) => u.unit === relUnit)?.label.toLowerCase() ?? relUnit;
                      apply(s, now, `Last ${relN} ${uLbl}`);
                    }}
                    className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white"
                  >Apply</button>
                </div>
              </div>
            )}

            {tab === "Date Range" && (
              <div className="space-y-2">
                <div className="flex gap-1 text-sm">
                  {(["between", "since", "before"] as const).map((m) => (
                    <button key={m} onClick={() => setDrMode(m)} className={`rounded px-2 py-1 capitalize ${drMode === m ? "bg-brand/10 font-medium text-brand" : "text-gray-600 hover:bg-gray-100"}`}>{m}</button>
                  ))}
                </div>
                {drMode === "between" && (
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="text-xs text-gray-500">From<input type="datetime-local" value={drStart} onChange={(e) => setDrStart(e.target.value)} className="mt-0.5 block rounded border px-2 py-1 text-sm" /></label>
                    <label className="text-xs text-gray-500">To<input type="datetime-local" value={drEnd} onChange={(e) => setDrEnd(e.target.value)} className="mt-0.5 block rounded border px-2 py-1 text-sm" /></label>
                  </div>
                )}
                {drMode === "since" && (
                  <label className="block text-xs text-gray-500">Since<input type="datetime-local" value={drStart} onChange={(e) => setDrStart(e.target.value)} className="mt-0.5 block rounded border px-2 py-1 text-sm" /></label>
                )}
                {drMode === "before" && (
                  <label className="block text-xs text-gray-500">Before<input type="datetime-local" value={drEnd} onChange={(e) => setDrEnd(e.target.value)} className="mt-0.5 block rounded border px-2 py-1 text-sm" /></label>
                )}
                {/* Show the same instant in the OTHER zone so a value typed in one is unambiguous. */}
                <div className="text-[11px] text-gray-400">
                  {(() => {
                    const other: Tz = tz === "utc" ? "local" : "utc";
                    const otherLbl = other === "utc" ? "UTC" : `local ${localOffsetLabel()}`;
                    const sAbs = parseInput(drStart, tz), eAbs = parseInput(drEnd, tz);
                    const parts: string[] = [];
                    if ((drMode === "between" || drMode === "since") && !isNaN(sAbs.getTime())) parts.push(fmtAbsDate(sAbs, other));
                    if ((drMode === "between" || drMode === "before") && !isNaN(eAbs.getTime())) parts.push(fmtAbsDate(eAbs, other));
                    return parts.length ? <>= {parts.join(" → ")} ({otherLbl})</> : null;
                  })()}
                </div>
                <div>
                  <button
                    onClick={() => {
                      const now = new Date();
                      if (drMode === "between") {
                        const s = parseInput(drStart, tz), e = parseInput(drEnd, tz);
                        if (isNaN(s.getTime()) || isNaN(e.getTime()) || s >= e) return;
                        apply(s, e, `${fmtAbsDate(s, tz)} → ${fmtAbsDate(e, tz)}`);
                      } else if (drMode === "since") {
                        const s = parseInput(drStart, tz);
                        if (isNaN(s.getTime())) return;
                        apply(s, now, `Since ${fmtAbsDate(s, tz)}`);
                      } else {
                        const e = parseInput(drEnd, tz);
                        if (isNaN(e.getTime())) return;
                        // "Before" needs a bounded start; default to 90 days prior (Activity Log practical reach).
                        apply(new Date(e.getTime() - 90 * UNIT_MS.d), e, `Before ${fmtAbsDate(e, tz)}`);
                      }
                    }}
                    className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white"
                  >Apply</button>
                </div>
              </div>
            )}

            {tab === "Advanced" && (
              <div className="space-y-2">
                <p className="text-[11px] text-gray-500">Relative modifiers, e.g. <code>-7d@d</code>, <code>-24h</code>, <code>@mon</code>, <code>now</code>. <code>@</code> snaps to the unit boundary.</p>
                <label className="block text-xs text-gray-500">Earliest<input value={advEarliest} onChange={(e) => setAdvEarliest(e.target.value)} className="mt-0.5 block w-full rounded border px-2 py-1 font-mono text-sm" placeholder="-24h" /></label>
                <label className="block text-xs text-gray-500">Latest<input value={advLatest} onChange={(e) => setAdvLatest(e.target.value)} className="mt-0.5 block w-full rounded border px-2 py-1 font-mono text-sm" placeholder="now" /></label>
                <div className="text-[11px] text-gray-500">
                  {advPreview.ok && advPreview.s && advPreview.e
                    ? <span className="text-emerald-700">{fmtAbsDate(advPreview.s, tz)} → {fmtAbsDate(advPreview.e, tz)} ({tz === "utc" ? "UTC" : localOffsetLabel()})</span>
                    : <span className="text-red-600">Invalid range — check the modifiers.</span>}
                </div>
                <button
                  onClick={() => { if (advPreview.ok && advPreview.s && advPreview.e) apply(advPreview.s, advPreview.e, `${advEarliest} → ${advLatest}`); }}
                  disabled={!advPreview.ok}
                  className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
                >Apply</button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
