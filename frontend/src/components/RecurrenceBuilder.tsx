// Advanced recurrence builder — a visual editor that compiles to a standard 5-field cron
// expression. Shared by the Scheduled Tasks form and the AI Insight Packs scheduler so both
// get the same "super advanced" cadence controls (interval, weekdays, day-of-month, months,
// time + multiple hours). The backend runs the generated cron via croniter.
import { useEffect, useState } from "react";

export type RecurUnit = "minutes" | "hours" | "days" | "weeks" | "months";
export type RecurState = {
  unit: RecurUnit;
  interval: number;
  minute: number;
  hours: number[];
  weekdays: number[]; // cron dow: 0=Sun..6=Sat
  dom: number;        // day of month 1-31
  months: number[];   // 1-12
};

const WEEKDAY_OPTS: { v: number; l: string }[] = [
  { v: 1, l: "Mon" }, { v: 2, l: "Tue" }, { v: 3, l: "Wed" }, { v: 4, l: "Thu" },
  { v: 5, l: "Fri" }, { v: 6, l: "Sat" }, { v: 0, l: "Sun" },
];
const MONTH_OPTS: { v: number; l: string }[] = [
  { v: 1, l: "Jan" }, { v: 2, l: "Feb" }, { v: 3, l: "Mar" }, { v: 4, l: "Apr" },
  { v: 5, l: "May" }, { v: 6, l: "Jun" }, { v: 7, l: "Jul" }, { v: 8, l: "Aug" },
  { v: 9, l: "Sep" }, { v: 10, l: "Oct" }, { v: 11, l: "Nov" }, { v: 12, l: "Dec" },
];

export function buildCron(s: RecurState): string {
  const m = Math.max(0, Math.min(59, s.minute || 0));
  const hourList = s.hours.length ? [...new Set(s.hours)].sort((a, b) => a - b).join(",") : "0";
  const dow = s.weekdays.length ? [...new Set(s.weekdays)].sort((a, b) => a - b).join(",") : "*";
  const months = s.months.length ? [...new Set(s.months)].sort((a, b) => a - b).join(",") : "*";
  const n = Math.max(1, s.interval || 1);
  switch (s.unit) {
    case "minutes": return `*/${n} * * * *`;
    case "hours": return `${m} */${n} * * *`;
    case "days": return n > 1 ? `${m} ${hourList} */${n} * *` : `${m} ${hourList} * * *`;
    case "weeks": return `${m} ${hourList} * * ${dow}`;
    case "months": return `${m} ${hourList} ${s.dom || 1} ${months} *`;
  }
}

function expandDowToken(tok: string): number[] {
  const range = /^(\d+)-(\d+)$/.exec(tok);
  if (range) {
    const out: number[] = [];
    for (let i = Number(range[1]); i <= Number(range[2]); i++) out.push(i % 7);
    return out;
  }
  const n = Number(tok);
  return Number.isNaN(n) ? [] : [n % 7];
}

export function parseCron(expr: string): RecurState | null {
  const parts = (expr || "").trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [mi, ho, dom, mon, dow] = parts;
  const nums = (s: string) => s.split(",").map(Number).filter((n) => !Number.isNaN(n));
  const stepOf = (s: string) => { const m = /^\*\/(\d+)$/.exec(s); return m ? Number(m[1]) : null; };
  const base: RecurState = { unit: "days", interval: 1, minute: 0, hours: [9], weekdays: [1, 2, 3, 4, 5], dom: 1, months: [] };
  if (stepOf(mi) && ho === "*" && dom === "*" && mon === "*" && dow === "*") return { ...base, unit: "minutes", interval: stepOf(mi)! };
  if (/^\d+$/.test(mi) && stepOf(ho) && dom === "*" && mon === "*" && dow === "*") return { ...base, unit: "hours", interval: stepOf(ho)!, minute: Number(mi) };
  if (/^\d+$/.test(mi) && dom === "*" && mon === "*" && dow !== "*") return { ...base, unit: "weeks", minute: Number(mi), hours: nums(ho), weekdays: dow.split(",").flatMap(expandDowToken) };
  if (/^\d+$/.test(mi) && dom !== "*" && dow === "*") return { ...base, unit: "months", minute: Number(mi), hours: nums(ho), dom: nums(dom)[0] || 1, months: mon === "*" ? [] : nums(mon) };
  if (/^\d+$/.test(mi) && stepOf(dom) && dow === "*") return { ...base, unit: "days", interval: stepOf(dom)!, minute: Number(mi), hours: nums(ho) };
  if (/^\d+$/.test(mi) && dom === "*" && mon === "*" && dow === "*") return { ...base, unit: "days", interval: 1, minute: Number(mi), hours: ho === "*" ? [] : nums(ho) };
  return null;
}

/** Visual recurrence builder that compiles to a cron expression. Owns its own state,
 *  seeded from the incoming cron (best-effort), and emits a cron string on every change. */
export function RecurrenceBuilder({ value, onChange }: { value: string; onChange: (cron: string) => void }) {
  const [s, setS] = useState<RecurState>(() => parseCron(value) ?? { unit: "weeks", interval: 1, minute: 0, hours: [9], weekdays: [1, 2, 3, 4, 5], dom: 1, months: [] });
  // Regenerate cron whenever the builder state changes.
  useEffect(() => { onChange(buildCron(s)); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s]);
  const patch = (p: Partial<RecurState>) => setS((cur) => ({ ...cur, ...p }));
  const toggleIn = (arr: number[], v: number) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  const primaryTime = `${String(s.hours[0] ?? 9).padStart(2, "0")}:${String(s.minute).padStart(2, "0")}`;
  const timeUnit = s.unit === "days" || s.unit === "weeks" || s.unit === "months";
  const chip = (on: boolean) =>
    `rounded-lg border px-2.5 py-1 text-xs ${on ? "border-brand bg-brand/10 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`;
  return (
    <div className="space-y-2 rounded-lg border border-gray-200 bg-gray-50/60 p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-xs font-medium text-gray-600">Repeat every</span>
        {(s.unit === "minutes" || s.unit === "hours" || s.unit === "days") && (
          <input type="number" min={1} value={s.interval} onChange={(e) => patch({ interval: Math.max(1, Number(e.target.value) || 1) })}
            className="w-16 rounded-lg border px-2 py-1 text-sm" />
        )}
        <select value={s.unit} onChange={(e) => patch({ unit: e.target.value as RecurUnit })} className="rounded-lg border px-2 py-1 text-sm">
          <option value="minutes">minute(s)</option>
          <option value="hours">hour(s)</option>
          <option value="days">day(s)</option>
          <option value="weeks">week (on days…)</option>
          <option value="months">month (on date…)</option>
        </select>
      </div>

      {s.unit === "hours" && (
        <label className="flex items-center gap-2 text-xs text-gray-600">At minute
          <input type="number" min={0} max={59} value={s.minute} onChange={(e) => patch({ minute: Math.max(0, Math.min(59, Number(e.target.value) || 0)) })}
            className="w-16 rounded-lg border px-2 py-1 text-sm" />
        </label>
      )}

      {s.unit === "weeks" && (
        <div>
          <div className="mb-1 text-xs text-gray-500">On days</div>
          <div className="flex flex-wrap gap-1.5">
            {WEEKDAY_OPTS.map((d) => (
              <button key={d.v} type="button" onClick={() => patch({ weekdays: toggleIn(s.weekdays, d.v) })} className={chip(s.weekdays.includes(d.v))}>{d.l}</button>
            ))}
          </div>
        </div>
      )}

      {s.unit === "months" && (
        <>
          <label className="flex items-center gap-2 text-xs text-gray-600">On day of month
            <input type="number" min={1} max={31} value={s.dom} onChange={(e) => patch({ dom: Math.max(1, Math.min(31, Number(e.target.value) || 1)) })}
              className="w-16 rounded-lg border px-2 py-1 text-sm" />
          </label>
          <div>
            <div className="mb-1 text-xs text-gray-500">In months <span className="text-gray-400">(all if none selected)</span></div>
            <div className="flex flex-wrap gap-1.5">
              {MONTH_OPTS.map((mo) => (
                <button key={mo.v} type="button" onClick={() => patch({ months: toggleIn(s.months, mo.v) })} className={chip(s.months.includes(mo.v))}>{mo.l}</button>
              ))}
            </div>
          </div>
        </>
      )}

      {timeUnit && (
        <div>
          <label className="flex items-center gap-2 text-xs text-gray-600">At time
            <input type="time" value={primaryTime}
              onChange={(e) => { const [h, m] = e.target.value.split(":").map(Number); patch({ minute: m || 0, hours: [h || 0, ...s.hours.slice(1)] }); }}
              className="rounded-lg border px-2 py-1 text-sm" />
          </label>
          <div className="mt-1">
            <div className="mb-1 text-[11px] text-gray-500">Also at these hours <span className="text-gray-400">(optional — same minute)</span></div>
            <div className="flex flex-wrap gap-1">
              {Array.from({ length: 24 }, (_, h) => h).map((h) => (
                <button key={h} type="button" onClick={() => patch({ hours: toggleIn(s.hours, h) })}
                  className={`rounded border px-1.5 py-0.5 text-[10px] ${s.hours.includes(h) ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-500 hover:bg-gray-50"}`}>{String(h).padStart(2, "0")}</button>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <span>Cron:</span>
        <code className="rounded bg-white px-1.5 py-0.5 font-mono text-gray-700">{buildCron(s)}</code>
      </div>
    </div>
  );
}
