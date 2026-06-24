// Live "before → after" visualization of a tag change-set, rendered as left→right
// transformation lanes (echoing the /graph viewer's visual language). It is purely DERIVED from
// the in-progress `ops` array — no backend call — so it updates on every keystroke and lets a user
// SEE what each rule will do BEFORE running Preview/dry-run.
//
// P1 (symbolic): each op becomes a lane {before node} → {operation glyph on an animated
//   connector} → {after node}. rename_key/normalize_value show the actual morph in the middle.
//   Incomplete ops render as a dashed "ghost" lane (won't be applied).
// P2 (enrichment): when a real dry-run `plan` exists, each lane is overlaid with the affected
//   resource count and — for the op's key — a REAL sample resource's current value (so the left
//   node shows the true "before" instead of a placeholder). Degrades cleanly to symbolic.
import { useMemo } from "react";
import type { TagRemediationOp, TagRemediationPlan } from "../../api";

// ------------------------------------------------------------------ op → lane derivation
type Tone = "add" | "set" | "rename" | "normalize" | "remove" | "ghost";

interface Lane {
  index: number;            // 1-based, mirrors the editor row number
  tone: Tone;
  glyph: string;            // operation symbol in the middle
  opLabel: string;          // short human label of the transform
  beforeKey: string;        // left node key (may be "")
  beforeVal: string | null; // left node value; null = unknown/placeholder, "" = absent
  afterKey: string;         // right node key
  afterVal: string | null;  // right node value
  keyMorph: boolean;        // the key itself changes (rename) — animate key swap
  valMorph: boolean;        // the value changes in the middle (normalize/set)
  incomplete: string;       // non-empty reason → ghost lane
  resourceCount: number | null; // P2: affected resources (null until a plan exists)
  sampleBefore: string | null;  // P2: a real sample resource's current value for this key
}

// Same completeness rule the apply/preview path uses (kept local so the flow never disagrees).
function incompleteReason(op: TagRemediationOp): string {
  if (!op.key?.trim()) return op.type === "rename_key" ? "From key is required" : "Key is required";
  if (op.type === "rename_key" && !op.to_key?.trim()) return "To key is required";
  if (op.type === "normalize_value" && !op.to_value?.trim()) return "To value is required";
  if ((op.type === "add_tag" || op.type === "set_tag") && (op.value === undefined || op.value === "")) return "Value is required";
  return "";
}

// Find, in a dry-run plan, how many resources this op's key touches + a real sample current value.
function planEnrichment(op: TagRemediationOp, plan?: TagRemediationPlan | null): { count: number | null; sample: string | null } {
  if (!plan?.items?.length || !op.key) return { count: null, sample: null };
  const key = op.key.trim();
  const keyLc = key.toLowerCase();
  let count = 0;
  let sample: string | null = null;
  for (const it of plan.items) {
    // Case-insensitive key lookup against the resource's CURRENT (before) tags.
    const beforeEntry = Object.entries(it.before || {}).find(([k]) => k.toLowerCase() === keyLc);
    const afterEntry = Object.entries(it.after || {}).find(([k]) => k.toLowerCase() === keyLc);
    const beforeV = beforeEntry?.[1];
    const afterV = afterEntry?.[1];
    // "Touched" = this op actually changes the resource w.r.t. its key (add/set/normalize) or the
    // key is being renamed away. We approximate via before≠after on the key (or its rename target).
    const toKeyLc = (op.to_key || "").toLowerCase();
    const renamedTo = op.type === "rename_key" && toKeyLc ? Object.entries(it.after || {}).find(([k]) => k.toLowerCase() === toKeyLc)?.[1] : undefined;
    const touched = beforeV !== afterV || (op.type === "rename_key" && renamedTo !== undefined && beforeEntry !== undefined);
    if (touched) {
      count += 1;
      if (sample === null && beforeV !== undefined) sample = beforeV;
    }
  }
  return { count: plan.items.length ? count : null, sample };
}

function toLane(op: TagRemediationOp, i: number, plan?: TagRemediationPlan | null): Lane {
  const incomplete = incompleteReason(op);
  const key = (op.key || "").trim();
  const { count, sample } = planEnrichment(op, plan);
  const base: Lane = {
    index: i + 1,
    tone: "ghost",
    glyph: "→",
    opLabel: "",
    beforeKey: key,
    beforeVal: null,
    afterKey: key,
    afterVal: null,
    keyMorph: false,
    valMorph: false,
    incomplete,
    resourceCount: count,
    sampleBefore: sample,
  };
  if (incomplete) return base;

  switch (op.type) {
    case "add_tag":
      return { ...base, tone: "add", glyph: "＋", opLabel: "add if missing",
        beforeKey: key, beforeVal: "", afterKey: key, afterVal: op.value ?? "" };
    case "set_tag":
      return { ...base, tone: "set", glyph: "✎", opLabel: "overwrite", valMorph: true,
        beforeKey: key, beforeVal: sample, afterKey: key, afterVal: op.value ?? "" };
    case "rename_key":
      return { ...base, tone: "rename", glyph: "↔", opLabel: "rename key", keyMorph: true,
        beforeKey: key, beforeVal: sample, afterKey: (op.to_key || "").trim(), afterVal: sample };
    case "normalize_value":
      return { ...base, tone: "normalize", glyph: "⟳", opLabel: "normalize value", valMorph: true,
        beforeKey: key, beforeVal: op.from_value ?? sample, afterKey: key, afterVal: op.to_value ?? "" };
    case "remove_key":
      // The key is deleted: it exists on the left (before) and is gone on the right (after).
      return { ...base, tone: "remove", glyph: "🗑", opLabel: "remove key",
        beforeKey: key, beforeVal: sample, afterKey: "", afterVal: null };
    default:
      return base;
  }
}

// ------------------------------------------------------------------ styling
const TONE: Record<Tone, { ring: string; chip: string; dot: string; edge: string; text: string }> = {
  add:       { ring: "border-emerald-300", chip: "bg-emerald-50 text-emerald-700 border-emerald-200", dot: "#10b981", edge: "#10b981", text: "text-emerald-700" },
  set:       { ring: "border-amber-300",   chip: "bg-amber-50 text-amber-700 border-amber-200",       dot: "#f59e0b", edge: "#f59e0b", text: "text-amber-700" },
  rename:    { ring: "border-sky-300",     chip: "bg-sky-50 text-sky-700 border-sky-200",             dot: "#0ea5e9", edge: "#0ea5e9", text: "text-sky-700" },
  normalize: { ring: "border-violet-300",  chip: "bg-violet-50 text-violet-700 border-violet-200",    dot: "#8b5cf6", edge: "#8b5cf6", text: "text-violet-700" },
  remove:    { ring: "border-red-300",     chip: "bg-red-50 text-red-700 border-red-200",             dot: "#ef4444", edge: "#ef4444", text: "text-red-700" },
  ghost:     { ring: "border-dashed border-gray-300", chip: "bg-gray-50 text-gray-400 border-gray-200", dot: "#cbd5e1", edge: "#cbd5e1", text: "text-gray-400" },
};

function truncate(s: string, n = 22): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// One tag chip (a key=value pill). `absent` renders the "no tag" placeholder.
function TagChip({ k, v, toneChip, absent, faded }: { k: string; v: string | null; toneChip: string; absent?: boolean; faded?: boolean }) {
  if (absent) {
    return <span className="inline-flex items-center rounded-md border border-dashed border-gray-300 bg-gray-50 px-2 py-1 text-[11px] italic text-gray-400">no tag</span>;
  }
  return (
    <span className={`inline-flex max-w-[180px] items-center gap-1 rounded-md border px-2 py-1 text-[11px] ${toneChip} ${faded ? "opacity-60" : ""}`} title={`${k}=${v ?? ""}`}>
      <span className="font-medium">{truncate(k || "—")}</span>
      <span className="opacity-50">=</span>
      <span>{v === null ? <span className="italic opacity-60">•••</span> : (v === "" ? <span className="italic opacity-60">(empty)</span> : truncate(v))}</span>
    </span>
  );
}

// The animated connector + operation glyph in the middle of a lane.
function Connector({ lane }: { lane: Lane }) {
  const t = TONE[lane.tone];
  const isGhost = lane.tone === "ghost";
  return (
    <div className="relative flex w-24 shrink-0 flex-col items-center justify-center px-2">
      {/* flowing edge */}
      <svg className="absolute inset-x-0 top-1/2 h-2 w-full -translate-y-1/2" preserveAspectRatio="none" viewBox="0 0 100 4">
        <line x1="0" y1="2" x2="100" y2="2" stroke={t.edge} strokeWidth="1.5" strokeOpacity={isGhost ? 0.4 : 0.55} strokeDasharray={isGhost ? "3 3" : "4 4"} className={isGhost ? "" : "csf-flow"} />
      </svg>
      {/* glyph badge */}
      <div className={`relative z-10 flex flex-col items-center gap-0.5`}>
        <span className={`flex h-7 w-7 items-center justify-center rounded-full border bg-white text-sm ${t.ring} ${t.text}`}>{lane.glyph}</span>
        <span className={`whitespace-nowrap text-[10px] ${t.text}`}>{lane.opLabel || "incomplete"}</span>
      </div>
    </div>
  );
}

function LaneRow({ lane, onHover, highlighted }: { lane: Lane; onHover?: (i: number | null) => void; highlighted?: boolean }) {
  const t = TONE[lane.tone];
  const isGhost = lane.tone === "ghost";
  // The right node: when a key is renamed, show the NEW key in the chip; when a value morphs, the
  // new value is on the right. For add/set the left side is "no tag"/sample and the right is the set value.
  const leftAbsent = lane.tone === "add" && (lane.sampleBefore === null);
  return (
    <div
      className={`flex w-fit max-w-full items-stretch gap-1 rounded-lg border bg-white p-2 transition ${highlighted ? "ring-2 ring-brand/30" : ""} ${isGhost ? "opacity-80" : ""}`}
      onMouseEnter={() => onHover?.(lane.index - 1)}
      onMouseLeave={() => onHover?.(null)}
    >
      <span className="mr-1 flex h-5 w-5 shrink-0 items-center justify-center self-center rounded bg-gray-100 text-[10px] font-medium text-gray-500">{lane.index}</span>
      {/* LEFT (before) */}
      <div className="flex flex-col items-start justify-center gap-1">
        <span className="text-[9px] uppercase tracking-wide text-gray-300">before</span>
        <TagChip k={lane.beforeKey} v={lane.sampleBefore !== null ? lane.sampleBefore : lane.beforeVal} toneChip="bg-gray-50 text-gray-600 border-gray-200" absent={leftAbsent} faded />
      </div>
      {/* MIDDLE (transform) */}
      <Connector lane={lane} />
      {/* RIGHT (after) */}
      <div className="flex flex-col items-end justify-center gap-1">
        <span className="text-[9px] uppercase tracking-wide text-gray-300">after</span>
        {isGhost
          ? <span className="text-[11px] italic text-gray-400">{lane.incomplete}</span>
          : lane.tone === "remove"
            ? <span className="inline-flex items-center rounded-md border border-dashed border-red-200 bg-red-50 px-2 py-1 text-[11px] italic text-red-400 line-through" title={`${lane.beforeKey} removed`}>{truncate(lane.beforeKey || "—")} removed</span>
            : <TagChip k={lane.afterKey} v={lane.afterVal} toneChip={t.chip} />}
      </div>
      {/* P2 resource count badge */}
      {lane.resourceCount !== null && !isGhost && (
        <div className="ml-1 flex shrink-0 flex-col items-center justify-center">
          <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${lane.resourceCount > 0 ? "bg-gray-100 text-gray-600" : "bg-gray-50 text-gray-300"}`} title={`${lane.resourceCount} resource(s) affected by this operation`}>
            {lane.resourceCount} res
          </span>
        </div>
      )}
    </div>
  );
}

export function ChangeSetFlow({
  ops,
  plan,
  hoveredIndex,
  onHover,
}: {
  ops: TagRemediationOp[];
  plan?: TagRemediationPlan | null;
  hoveredIndex?: number | null;
  onHover?: (i: number | null) => void;
}) {
  const lanes = useMemo(() => ops.map((op, i) => toLane(op, i, plan)), [ops, plan]);
  const valid = lanes.filter((l) => !l.incomplete).length;
  const keysAffected = new Set(lanes.filter((l) => !l.incomplete).map((l) => l.beforeKey.toLowerCase()).filter(Boolean)).size;
  const overwrites = (plan?.overwrites ?? null);

  return (
    <div className="rounded-lg border border-gray-200 bg-gradient-to-b from-slate-50 to-white p-2">
      {/* scoped, reduced-motion-aware flow animation */}
      <style>{`
        .csf-flow { stroke-dashoffset: 0; animation: csf-march 0.9s linear infinite; }
        @keyframes csf-march { to { stroke-dashoffset: -16; } }
        @media (prefers-reduced-motion: reduce) { .csf-flow { animation: none; } }
      `}</style>
      <div className="mb-1.5 flex items-center gap-2 px-1">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Transformation preview</span>
        <span className="text-[10px] text-gray-400">{valid} op(s) · {keysAffected} key(s){overwrites !== null ? ` · ${overwrites} overwrite(s)` : ""}</span>
        {plan?.items?.length ? <span className="ml-auto rounded-full bg-sky-50 px-2 py-0.5 text-[10px] font-medium text-sky-600">live · {plan.count} resources</span> : <span className="ml-auto text-[10px] text-gray-300">symbolic — run Preview for resource counts</span>}
      </div>
      {lanes.length === 0 ? (
        <div className="p-4 text-center text-[11px] text-gray-400">Add an operation to preview the transformation.</div>
      ) : (
        <div className="space-y-1.5">
          {lanes.map((lane) => (
            <LaneRow key={lane.index} lane={lane} onHover={onHover} highlighted={hoveredIndex === lane.index - 1} />
          ))}
        </div>
      )}
    </div>
  );
}
