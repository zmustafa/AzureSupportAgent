// AI Insight Packs — the central library + runs hub for scheduled AI digests.
//
// A pack is a reusable, scope-agnostic definition (what to watch, which data, how noisy).
// Users author packs via an AI generator wizard or a form, run/test them on demand, schedule
// them against a scope, and read the resulting digests. This panel owns:
//   • Library gallery (packs + starter templates)
//   • AI generator wizard (goal → interview → generate → review)
//   • Pack editor form
//   • Run / Schedule dialog (scope picker + on-demand run + recurring schedule)
//   • Digest viewer + run history + upcoming schedule
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  downloadBlob,
  type InsightPack,
  type InsightPackHealth,
  type InsightPackLibrary,
  type InsightCollection,
  type InsightCoverage,
  type InsightOccurrence,
  type InsightRun,
  type InsightRunJob,
  type InsightRunStep,
  type InsightScope,
  type InsightVerdict,
  type InsightWatcher,
  type ChangeWorkload,
  type AgentAnswer,
  type InsightWizardQuestion,
  type InsightPackPreview,
  type InsightRefineMode,
  type InsightRefineChange,
  type InsightCritiqueFinding,
  type InsightSampleFinding,
} from "../api";
import { RecurrenceBuilder } from "./RecurrenceBuilder";
import { formatTimestamp, formatRelativeFromNow } from "../utils/format";

// ---------------------------------------------------------------- shared bits
function formatError(e: unknown): string {
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : "Something went wrong.";
}

const VERDICT_META: Record<InsightVerdict, { label: string; badge: string; dot: string }> = {
  nothing_notable: { label: "Nothing notable", badge: "bg-gray-100 text-gray-600", dot: "bg-gray-400" },
  notable: { label: "Notable", badge: "bg-amber-100 text-amber-800", dot: "bg-amber-500" },
  urgent: { label: "Urgent", badge: "bg-red-100 text-red-700", dot: "bg-red-500" },
};

const RISK_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  high: "bg-orange-100 text-orange-700",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-gray-100 text-gray-600",
  informational: "bg-gray-100 text-gray-500",
};

function VerdictBadge({ verdict }: { verdict: InsightVerdict }) {
  const m = VERDICT_META[verdict] ?? VERDICT_META.notable;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${m.badge}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}

function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// Render a digest "when" value in the viewer's local timezone. Values may be full
// ISO timestamps (e.g. 2026-07-03T15:33:25.8086355Z) or free-text the model wrote
// (e.g. "last 24h"); non-parseable strings are returned unchanged.
function formatWhen(raw?: string | null): string {
  if (!raw) return "";
  const t = new Date(raw).getTime();
  if (Number.isNaN(t)) return raw;
  return new Date(t).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

const VERDICT_RANK: Record<InsightVerdict, number> = { nothing_notable: 0, notable: 1, urgent: 2 };

// Mirrors backend snapshots.scope_key so the UI can collapse runs by (pack, scope).
function scopeKey(s?: InsightScope): string {
  if (!s) return "none";
  const mode = s.mode || "workload";
  if (mode === "tenant") return "tenant";
  if (mode === "subscription") return `sub:${s.subscription_id || s.subscription_name || ""}`;
  const wids = s.workload_ids ?? (s.workload_id ? [s.workload_id] : []);
  return `${mode}:${[...wids].sort().join(",")}`;
}

// A run counts as "unread" only if it was material enough to notify and hasn't been opened.
function isUnread(r: InsightRun): boolean {
  return !!r.notified && !r.read_at;
}

// Day bucket label for grouping runs in the inbox.
function dayLabel(iso?: string): string {
  if (!iso) return "Earlier";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Earlier";
  const now = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString([], { weekday: "long" });
  return d.toLocaleDateString([], {
    month: "short", day: "numeric",
    year: now.getFullYear() === d.getFullYear() ? undefined : "numeric",
  });
}

// Relative label for a *future* instant ("in 3h", "in 2d") — used for next-run times.
function timeUntil(iso?: string | null): string {
  if (!iso) return "";
  const s = Math.floor((new Date(iso).getTime() - Date.now()) / 1000);
  if (Number.isNaN(s)) return "";
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.floor(s / 60)}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h`;
  return `in ${Math.floor(s / 86400)}d`;
}

// Verdict-rank → bar color for the health sparkline (0 quiet · 1 notable · 2 urgent).
const SPARK_COLORS = ["#d1d5db", "#f59e0b", "#ef4444"];

function Sparkline({ values, className = "" }: { values: number[]; className?: string }) {
  if (!values.length) return <span className="text-[11px] text-gray-300">no runs</span>;
  const w = 4, gap = 2, h = 16;
  return (
    <svg width={values.length * (w + gap)} height={h} className={className} aria-hidden>
      {values.map((v, i) => {
        const bh = v >= 2 ? 15 : v === 1 ? 9 : 3;
        return <rect key={i} x={i * (w + gap)} y={h - bh} width={w} height={bh} rx={1} fill={SPARK_COLORS[v] ?? SPARK_COLORS[0]} />;
      })}
    </svg>
  );
}

// A pack is "snoozed" while its snoozed_until instant is still in the future.
function snoozeInfo(pack?: InsightPack | null): { active: boolean; until?: string } {
  const iso = pack?.snoozed_until;
  if (!iso) return { active: false };
  const t = new Date(iso).getTime();
  return { active: !Number.isNaN(t) && t > Date.now(), until: iso };
}

// Watcher/coverage status → label + colors (shared by the schedule list + coverage matrix).
const COVERAGE_STATUS: Record<string, { label: string; cls: string; dot: string }> = {
  covered: { label: "Covered", cls: "text-green-700", dot: "bg-green-500" },
  stale: { label: "Stale", cls: "text-amber-700", dot: "bg-amber-500" },
  paused: { label: "Paused", cls: "text-gray-500", dot: "bg-gray-400" },
  gap: { label: "Gap", cls: "text-red-600", dot: "bg-red-400" },
};
function CoverageDot({ status }: { status: string }) {
  const m = COVERAGE_STATUS[status] ?? COVERAGE_STATUS.gap;
  return <span className={`inline-flex items-center gap-1 text-[11px] font-medium ${m.cls}`}><span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />{m.label}</span>;
}

// ---------------------------------------------------------------- background run progress
type RunJobRequest = {
  pack_id?: string;
  pack?: Partial<InsightPack>;
  scope: InsightScope;
  notify?: boolean;
};

// Starts an on-demand run as a background job and polls it for detailed progress. The run
// keeps executing server-side even if the caller closes the dialog; the final digest also
// lands in the run history regardless.
function useRunJob(onDone?: (run: InsightRun) => void) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const doneRef = useRef<string | null>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const q = useQuery({
    queryKey: ["insightRunJob", jobId],
    queryFn: () => api.getInsightRunJob(jobId as string),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const s = query.state.data?.job.status;
      return s === "succeeded" || s === "failed" ? false : 800;
    },
  });
  const job = q.data?.job ?? null;

  useEffect(() => {
    if (job && job.status === "succeeded" && job.run && doneRef.current !== job.id) {
      doneRef.current = job.id;
      onDoneRef.current?.(job.run);
    }
  }, [job]);

  const start = useCallback(async (req: RunJobRequest) => {
    setStarting(true);
    setStartError("");
    doneRef.current = null;
    try {
      const { job_id } = await api.startInsightRun(req);
      setJobId(job_id);
    } catch (e) {
      setStartError(formatError(e));
    } finally {
      setStarting(false);
    }
  }, []);

  const reset = useCallback(() => {
    setJobId(null);
    setStartError("");
    doneRef.current = null;
  }, []);

  const isRunning = starting || (!!job && (job.status === "queued" || job.status === "running"));
  return { start, reset, job, isRunning, starting, error: startError || job?.error || "" };
}

const RUN_STAGE_ICON: Record<string, string> = {
  scope: "🎯", gather: "🛰️", reason: "🧠", gate: "⚖️", deliver: "📣", done: "✅", error: "⚠️", queued: "⏳",
};

// Live progress panel for a background run: an animated bar + a timeline of milestones.
function RunProgress({ job, starting }: { job: InsightRunJob | null; starting: boolean }) {
  const pct = job?.pct ?? (starting ? 3 : 0);
  const failed = job?.status === "failed";
  const done = job?.status === "succeeded";
  const steps = job?.steps ?? [];
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-800">
          {!done && !failed && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-brand" />
          )}
          <span>{job?.label ?? (starting ? "Starting…" : "Preparing…")}</span>
        </div>
        <span className={`text-xs font-semibold tabular-nums ${failed ? "text-red-600" : "text-gray-500"}`}>{pct}%</span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-gray-200">
        <div
          className={`h-full rounded-full transition-all duration-500 ${failed ? "bg-red-500" : done ? "bg-green-500" : "bg-brand"}`}
          style={{ width: `${Math.max(3, pct)}%` }}
        />
      </div>
      {steps.length > 0 && (
        <ol className="mt-3 max-h-40 space-y-1.5 overflow-y-auto pr-1">
          {steps.map((s: InsightRunStep, i: number) => {
            const isLast = i === steps.length - 1;
            const active = isLast && !done && !failed;
            return (
              <li key={i} className="flex items-start gap-2 text-xs">
                <span className="mt-0.5 w-4 shrink-0 text-center">
                  {s.state === "error" ? "⚠️" : active ? (
                    <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-gray-300 border-t-brand align-middle" />
                  ) : (RUN_STAGE_ICON[s.stage] ?? "•")}
                </span>
                <span className={`${s.state === "error" ? "text-red-600" : active ? "text-gray-800" : "text-gray-500"}`}>
                  <span className="font-medium">{s.label}</span>
                  {s.detail && <span className="text-gray-400"> · {s.detail}</span>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      <p className="mt-3 text-[11px] text-gray-400">Runs in the background — safe to close this dialog; the digest is saved to Recent runs.</p>
    </div>
  );
}

// ---------------------------------------------------------------- digest renderer
export function DigestView({ run }: { run: InsightRun }) {
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="text-2xl leading-none">{run.pack_icon || "🧠"}</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-900">{run.pack_name}</span>
              <VerdictBadge verdict={run.verdict} />
              {run.notified && (
                <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[11px] font-medium text-brand">
                  Notified
                </span>
              )}
            </div>
            <div className="mt-0.5 text-xs text-gray-500">
              {run.scope_label} · last {run.lookback_hours}h · {run.counts.changes} change(s)
              {run.counts.flags.length > 0 && ` · ${run.counts.flags.length} security flag(s)`}
              {run.created_at && ` · ${timeAgo(run.created_at)}`}
            </div>
          </div>
        </div>
      </div>

      <p className="text-[15px] font-medium text-gray-800">{run.headline}</p>

      {run.ai_error && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          The AI step degraded to a deterministic summary: {run.ai_error}
        </div>
      )}

      {run.bullets.length > 0 && (
        <ul className="space-y-1.5">
          {run.bullets.map((b, i) => (
            <li key={i} className="flex gap-2 text-sm text-gray-700">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand/60" />
              <span>{b}</span>
            </li>
          ))}
        </ul>
      )}

      {run.table.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Change</th>
                <th className="px-3 py-2 font-medium">Risk</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Recommended action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {run.table.map((r, i) => (
                <tr key={i} className="align-top">
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500" title={r.time}>{formatWhen(r.time)}</td>
                  <td className="px-3 py-2 text-gray-800">
                    <div>{r.change}</div>
                    {r.workload && <div className="text-xs text-gray-400">{r.workload}</div>}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${RISK_STYLE[r.risk] ?? RISK_STYLE.low}`}>
                      {r.risk || "—"}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">{r.owner}</td>
                  <td className="px-3 py-2 text-gray-700">{r.recommended_action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {run.bullets.length === 0 && run.table.length === 0 && !run.ai_error && (
        <p className="text-sm text-gray-500">Nothing worth flagging in this window.</p>
      )}

      {run.gate_reason && (
        <p className="text-[11px] text-gray-400">Materiality gate: {run.gate_reason}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- modal shell
function Modal({ title, subtitle, onClose, children, wide, size }: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
  size?: "xl" | "3xl" | "5xl";
}) {
  const maxW = size === "5xl" ? "max-w-5xl" : (size === "3xl" || wide) ? "max-w-3xl" : "max-w-xl";
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/30 p-4 sm:p-8" onClick={onClose}>
      <div
        className={`my-4 w-full ${maxW} rounded-2xl bg-white shadow-xl`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b border-gray-100 px-5 py-4">
          <div>
            <h3 className="text-base font-semibold text-gray-900">{title}</h3>
            {subtitle && <p className="mt-0.5 text-xs text-gray-500">{subtitle}</p>}
          </div>
          <button onClick={onClose} className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600">
            ✕
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- generator wizard
type WizStage = "intent" | "interview" | "generating" | "preview" | "error";

// A resumable in-progress interview, persisted so an accidental close doesn't lose work.
type WizSnapshot = { goal: string; answers: AgentAnswer[]; step: number };
const WIZ_STORAGE_KEY = "insightWizard.draft.v1";

// Starter goals shown on the intent screen — one click seeds a strong, specific goal.
const WIZ_STARTERS: { icon: string; label: string; goal: string }[] = [
  { icon: "🌐", label: "Public exposure", goal: "Alert me whenever a change exposes a workload to the public internet — new public IPs, permissive NSG rules, or storage/keyvault firewalls opened up." },
  { icon: "🛂", label: "Privileged access", goal: "Watch for anyone being granted privileged or Owner-level access, new RBAC role assignments, and eligible PIM roles across the tenant." },
  { icon: "💰", label: "Cost creep", goal: "Surface idle and orphaned resources and their estimated monthly waste so I can clean them up before the bill grows." },
  { icon: "🔑", label: "Identity risk", goal: "Flag expiring secrets and certificates, privileged users without MFA, and ownerless app registrations." },
  { icon: "📡", label: "Retirements", goal: "Tell me about upcoming Azure service retirements and breaking changes that affect my workloads." },
  { icon: "📏", label: "Policy drift", goal: "Notify me about new non-compliant resources and policy exemptions since the last check." },
];

// Cheap fuzzy overlap of two short strings, for duplicate-pack detection.
function _tokens(s: string): Set<string> {
  return new Set(
    s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").split(/\s+/).filter((w) => w.length > 3),
  );
}
function _overlap(a: string, b: string): number {
  const ta = _tokens(a), tb = _tokens(b);
  if (ta.size === 0 || tb.size === 0) return 0;
  let hit = 0;
  ta.forEach((t) => { if (tb.has(t)) hit++; });
  return hit / Math.min(ta.size, tb.size);
}

function GeneratorWizard({ onDraft, onClose, library }: {
  onDraft: (draft: InsightPack, summary: string) => void;
  onClose: () => void;
  library: InsightPackLibrary;
}) {
  const [stage, setStage] = useState<WizStage>("intent");
  const [goal, setGoal] = useState("");
  const [step, setStep] = useState(0);
  const [questions, setQuestions] = useState<InsightWizardQuestion[]>([]);
  const [note, setNote] = useState("");
  const [answers, setAnswers] = useState<AgentAnswer[]>([]);
  const [current, setCurrent] = useState<Record<string, string | string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showErrors, setShowErrors] = useState(false);
  const [offTopic, setOffTopic] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [preview, setPreview] = useState<InsightPackPreview | null>(null);
  const [draft, setDraft] = useState<InsightPack | null>(null);
  const [summary, setSummary] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [resumable, setResumable] = useState<WizSnapshot | null>(null);
  // Preview-run stage
  const [runWorkloadId, setRunWorkloadId] = useState("");
  const [sampleRun, setSampleRun] = useState<InsightRun | null>(null);
  const [runBusy, setRunBusy] = useState(false);

  const cancelledRef = useRef(false);
  const previewTimer = useRef<number | null>(null);
  const { data: wlData } = useQuery({ queryKey: ["changeWorkloads"], queryFn: () => api.changeExplorerWorkloads() });
  const workloads: ChangeWorkload[] = wlData?.workloads ?? [];
  const selectedWl = workloads.find((w) => w.id === runWorkloadId);

  // ---- resume: read any persisted in-progress interview once on mount
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(WIZ_STORAGE_KEY);
      if (raw) {
        const snap = JSON.parse(raw) as WizSnapshot;
        if (snap?.goal && (snap.answers?.length || snap.step)) setResumable(snap);
      }
    } catch { /* ignore malformed */ }
  }, []);

  // ---- persist progress while interviewing
  useEffect(() => {
    if (stage === "interview" && goal.trim()) {
      try {
        sessionStorage.setItem(WIZ_STORAGE_KEY, JSON.stringify({ goal, answers, step } satisfies WizSnapshot));
      } catch { /* quota / disabled */ }
    }
  }, [stage, goal, answers, step]);

  function clearProgress() {
    try { sessionStorage.removeItem(WIZ_STORAGE_KEY); } catch { /* ignore */ }
  }

  // ---- elapsed timer during the (slow) generate step
  useEffect(() => {
    if (stage !== "generating") { setElapsed(0); return; }
    const started = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
    return () => window.clearInterval(id);
  }, [stage]);

  // ---- live "pack so far" preview (deterministic, no LLM) — debounced
  function schedulePreview(g: string, ans: AgentAnswer[]) {
    if (previewTimer.current) window.clearTimeout(previewTimer.current);
    previewTimer.current = window.setTimeout(() => {
      api.insightPreview(g.trim(), ans).then(setPreview).catch(() => { /* preview is best-effort */ });
    }, 350);
  }
  useEffect(() => () => { if (previewTimer.current) window.clearTimeout(previewTimer.current); }, []);

  // ---- duplicate detection against the existing library
  const duplicate = useMemo(() => {
    if (goal.trim().length < 8) return null;
    let best: { pack: InsightPack; score: number } | null = null;
    for (const p of library.packs) {
      const score = Math.max(_overlap(goal, p.name), _overlap(goal, p.description));
      if (score >= 0.6 && (!best || score > best.score)) best = { pack: p, score };
    }
    return best?.pack ?? null;
  }, [goal, library.packs]);

  const vague = goal.trim().length > 0 && goal.trim().length < 25;

  async function beginInterview(g: string, priorAnswers: AgentAnswer[], atStep: number) {
    setError(""); setOffTopic(false); setSuggestions([]); setBusy(true);
    cancelledRef.current = false;
    schedulePreview(g, priorAnswers);
    try {
      const res = await api.insightInterview(g.trim(), priorAnswers, atStep);
      if (cancelledRef.current) return;
      if (res.off_topic) {
        setOffTopic(true); setNote(res.note); setSuggestions(res.suggestions ?? []);
        setStage("interview"); return;
      }
      if (res.done || res.questions.length === 0) { await generate(g, priorAnswers); return; }
      setQuestions(res.questions); setNote(res.note); setStep(atStep + 1);
      setCurrent({}); setCustom({}); setShowErrors(false); setStage("interview");
    } catch (e) { if (!cancelledRef.current) setError(formatError(e)); }
    finally { if (!cancelledRef.current) setBusy(false); }
  }

  function start() {
    if (!goal.trim()) { setError("Describe what the pack should watch for."); return; }
    setAnswers([]); setStep(0); beginInterview(goal, [], 0);
  }

  function resume() {
    if (!resumable) return;
    setGoal(resumable.goal); setAnswers(resumable.answers); setStep(resumable.step);
    setResumable(null);
    beginInterview(resumable.goal, resumable.answers, resumable.step);
  }

  function toggleMulti(qid: string, opt: string) {
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      return { ...c, [qid]: prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt] };
    });
  }

  function addCustomChip(qid: string) {
    const v = (custom[qid] ?? "").trim();
    if (!v) return;
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      return prev.includes(v) ? c : { ...c, [qid]: [...prev, v] };
    });
    setCustom((c) => ({ ...c, [qid]: "" }));
  }

  function mergedForStep(): AgentAnswer[] {
    return questions.map((q) => {
      if (q.kind === "multi") {
        const arr = Array.isArray(current[q.id]) ? (current[q.id] as string[]) : [];
        return { id: q.id, prompt: q.prompt, answer: arr };
      }
      const cust = (custom[q.id] ?? "").trim();
      const sel = (current[q.id] as string) ?? "";
      return { id: q.id, prompt: q.prompt, answer: cust || sel };
    });
  }

  function isAnswered(q: InsightWizardQuestion): boolean {
    if (q.kind === "multi") return Array.isArray(current[q.id]) && (current[q.id] as string[]).length > 0;
    return !!((custom[q.id] ?? "").trim() || (current[q.id] as string));
  }

  async function submitStep() {
    const missing = questions.filter((q) => q.required && !isAnswered(q));
    if (missing.length) { setShowErrors(true); setError("Please answer the required question(s)."); return; }
    const all = [...answers, ...mergedForStep()];
    setAnswers(all);
    beginInterview(goal, all, step);
  }

  async function generate(g: string, all: AgentAnswer[]) {
    setStage("generating"); setBusy(true); setError("");
    cancelledRef.current = false;
    try {
      const res = await api.insightGenerate(g.trim(), all);
      if (cancelledRef.current) return;
      setDraft(res.draft); setSummary(res.summary); setSampleRun(null);
      clearProgress();
      setStage("preview");
    } catch (e) { if (!cancelledRef.current) { setError(formatError(e)); setStage("error"); } }
    finally { if (!cancelledRef.current) setBusy(false); }
  }

  function cancelThinking() {
    cancelledRef.current = true; setBusy(false);
    setStage(questions.length ? "interview" : "intent");
  }

  async function runSample() {
    if (!draft || !runWorkloadId) { setError("Pick a workload to preview against."); return; }
    setRunBusy(true); setError("");
    const supported = draft.supported_scopes.length ? draft.supported_scopes : ["workload"];
    const mode = (supported.includes("workload") ? "workload" : supported[0]) as InsightScope["mode"];
    const scope: InsightScope = {
      mode,
      workload_ids: [runWorkloadId],
      workload_names: selectedWl ? [selectedWl.name] : undefined,
      connection_id: selectedWl?.connection_id,
    };
    try {
      const { run } = await api.runInsightPack({ pack: draft, scope, notify: false });
      setSampleRun(run);
    } catch (e) { setError(formatError(e)); }
    finally { setRunBusy(false); }
  }

  // ---- keyboard: number keys pick options (single-question steps), Enter continues, Esc closes
  useEffect(() => {
    if (stage !== "interview" || offTopic) return;
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (e.key === "Escape" && !busy) { onClose(); return; }
      if (typing) return;
      if (e.key === "Enter" && !busy) { e.preventDefault(); submitStep(); return; }
      if (questions.length === 1 && /^[1-9]$/.test(e.key)) {
        const q = questions[0];
        const opt = q.options[Number(e.key) - 1];
        if (!opt) return;
        e.preventDefault();
        if (q.kind === "multi") toggleMulti(q.id, opt.value);
        else setCurrent((c) => ({ ...c, [q.id]: opt.value }));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stage, offTopic, busy, questions]); // eslint-disable-line react-hooks/exhaustive-deps

  const stepLabels = ["Goal", "AI interview", "Generate", "Preview & save"];
  const activeStepIdx = stage === "intent" ? 0 : stage === "interview" ? 1 : stage === "generating" ? 2 : 3;
  const completeness = Math.min(1, 0.15 + answers.length * 0.18 + (stage === "preview" ? 1 : 0));

  function PreviewPane() {
    if (!preview) {
      return <p className="text-xs text-gray-400">Answer a question to see the pack take shape…</p>;
    }
    const th = preview.materiality?.notify_threshold;
    return (
      <div className="space-y-3 text-sm" aria-live="polite">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Working name</div>
          <div className="font-medium text-gray-800">{preview.name || "New Insight Pack"}</div>
        </div>
        {preview.source_labels && preview.source_labels.length > 0 && (
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Data sources</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {preview.source_labels.map((s) => (
                <span key={s} className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{s}</span>
              ))}
            </div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Notify when</div>
            <div className="text-gray-700">{th ? (VERDICT_META[th]?.label ?? th) : "—"}</div>
          </div>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Lookback</div>
            <div className="text-gray-700">{preview.lookback_hours ? `${preview.lookback_hours}h` : "—"}</div>
          </div>
        </div>
        {preview.materiality?.always_notify_if && preview.materiality.always_notify_if.length > 0 && (
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">Always alert on</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {preview.materiality.always_notify_if.map((f) => (
                <span key={f} className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800">{f}</span>
              ))}
            </div>
          </div>
        )}
        <p className="text-[11px] text-gray-400">This is a live estimate — the AI finalizes every field when you generate.</p>
      </div>
    );
  }

  function AnswerSummary() {
    if (answers.length === 0) return null;
    return (
      <details className="rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2">
        <summary className="cursor-pointer text-xs font-medium text-gray-500">
          {answers.length} answer{answers.length === 1 ? "" : "s"} so far
        </summary>
        <ul className="mt-2 space-y-1">
          {answers.map((a, i) => {
            const val = Array.isArray(a.answer) ? a.answer.join(", ") : a.answer;
            return (
              <li key={i} className="text-xs text-gray-600">
                <span className="text-gray-400">{a.prompt}:</span> {val || "(skipped)"}
              </li>
            );
          })}
        </ul>
      </details>
    );
  }

  return (
    <Modal title="Generate an insight pack with AI" subtitle="Describe what you want to watch — the AI designs the pack." onClose={onClose} wide>
      <div className="mb-4 flex items-center gap-2 text-xs text-gray-400">
        {stepLabels.map((l, i) => (
          <span key={l} className="flex items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 ${i <= activeStepIdx ? "bg-brand/10 font-medium text-brand" : ""}`}>
              {i + 1}. {l}
            </span>
            {i < stepLabels.length - 1 && <span aria-hidden>→</span>}
          </span>
        ))}
      </div>

      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      {stage === "intent" && (
        <div className="space-y-4">
          {resumable && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-brand/30 bg-brand/5 px-3 py-2 text-sm">
              <span className="text-gray-700">You have an unfinished pack in progress.</span>
              <div className="flex gap-2">
                <button onClick={() => { setResumable(null); clearProgress(); }} className="rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-white">Discard</button>
                <button onClick={resume} className="rounded-lg bg-brand px-3 py-1 text-xs font-medium text-white hover:bg-brand/90">Resume</button>
              </div>
            </div>
          )}
          <div>
            <label htmlFor="wiz-goal" className="block text-sm font-medium text-gray-700">What should this pack watch for?</label>
            <textarea
              id="wiz-goal"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={4}
              maxLength={600}
              placeholder="e.g. Watch for anything that exposes a workload to the public internet, or grants someone privileged access."
              className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
            />
            <div className="mt-1 flex items-center justify-between text-xs text-gray-400">
              <span>{vague ? "A bit more detail will produce a sharper pack." : "The clearer the intent, the better the design."}</span>
              <span>{goal.trim().length}/600</span>
            </div>
          </div>

          {duplicate && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              A similar pack already exists: <span className="font-medium">{duplicate.icon} {duplicate.name}</span>. You can still create a new one.
            </div>
          )}

          <div>
            <div className="mb-1.5 text-xs font-medium text-gray-500">Or start from an idea</div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {WIZ_STARTERS.map((s) => (
                <button
                  key={s.label}
                  onClick={() => setGoal(s.goal)}
                  className="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 text-left text-sm text-gray-700 transition hover:border-brand hover:bg-brand/5"
                >
                  <span className="text-lg leading-none">{s.icon}</span>
                  <span className="font-medium">{s.label}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Cancel</button>
            <button onClick={start} disabled={busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
              {busy ? "Thinking…" : "Start"}
            </button>
          </div>
        </div>
      )}

      {stage === "interview" && offTopic && (
        <div className="space-y-4">
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-800">
            {note || "That doesn't look like an Azure monitoring goal. Try describing a change, cost, access or security signal you want to watch."}
          </div>
          {suggestions.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-gray-500">Try one of these</div>
              {suggestions.map((s, i) => (
                <button key={i} onClick={() => { setGoal(s); setOffTopic(false); setStage("intent"); }}
                  className="block w-full rounded-lg border border-gray-200 px-3 py-2 text-left text-sm text-gray-700 hover:border-brand hover:bg-brand/5">
                  {s}
                </button>
              ))}
            </div>
          )}
          <div className="flex justify-end">
            <button onClick={() => { setOffTopic(false); setStage("intent"); }} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90">
              Edit goal
            </button>
          </div>
        </div>
      )}

      {stage === "interview" && !offTopic && (
        <div className="grid gap-5 md:grid-cols-[1fr_260px]">
          <div className="space-y-4">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100" aria-hidden>
              <div className="h-full rounded-full bg-brand transition-all" style={{ width: `${Math.round(completeness * 100)}%` }} />
            </div>
            {note && <p className="text-sm text-gray-500">{note}</p>}
            <AnswerSummary />
            {busy && questions.length === 0 ? (
              <div className="space-y-3">
                {[0, 1, 2].map((i) => <div key={i} className="h-16 animate-pulse rounded-lg bg-gray-100" />)}
              </div>
            ) : (
              questions.map((q) => (
                <fieldset key={q.id} className="space-y-2">
                  <legend className="text-sm font-medium text-gray-700">
                    {q.prompt}
                    {q.required && <span className="ml-1 text-red-500">*</span>}
                  </legend>
                  {q.help && <p className="text-xs text-gray-400">{q.help}</p>}
                  {showErrors && q.required && !isAnswered(q) && (
                    <p className="text-xs text-red-600">This question is required.</p>
                  )}
                  {q.kind === "text" ? (
                    <input
                      value={(current[q.id] as string) ?? ""}
                      onChange={(e) => setCurrent((c) => ({ ...c, [q.id]: e.target.value }))}
                      className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                    />
                  ) : (
                    <div role={q.kind === "multi" ? "group" : "radiogroup"} className="space-y-1.5">
                      {q.options.map((opt) => {
                        const selected = q.kind === "multi"
                          ? Array.isArray(current[q.id]) && (current[q.id] as string[]).includes(opt.value)
                          : current[q.id] === opt.value;
                        return (
                          <button
                            key={opt.value}
                            role={q.kind === "multi" ? "checkbox" : "radio"}
                            aria-checked={selected}
                            onClick={() => q.kind === "multi" ? toggleMulti(q.id, opt.value) : setCurrent((c) => ({ ...c, [q.id]: opt.value }))}
                            className={`flex w-full items-start gap-2.5 rounded-lg border px-3 py-2 text-left text-sm transition ${selected ? "border-brand bg-brand/5" : "border-gray-200 hover:border-gray-300 hover:bg-gray-50"}`}
                          >
                            <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center border ${q.kind === "multi" ? "rounded" : "rounded-full"} ${selected ? "border-brand bg-brand text-white" : "border-gray-300"}`}>
                              {selected && <span className="text-[10px] leading-none">✓</span>}
                            </span>
                            <span className="min-w-0">
                              <span className="flex items-center gap-1.5">
                                <span className={selected ? "font-medium text-brand" : "text-gray-700"}>{opt.value}</span>
                                {opt.recommended && <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700">Recommended</span>}
                              </span>
                              {opt.description && <span className="block text-xs text-gray-400">{opt.description}</span>}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {q.allow_custom && q.kind === "multi" && (
                    <div className="flex gap-2">
                      <input
                        value={custom[q.id] ?? ""}
                        onChange={(e) => setCustom((c) => ({ ...c, [q.id]: e.target.value }))}
                        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustomChip(q.id); } }}
                        placeholder="Add your own…"
                        className="flex-1 rounded-lg border border-gray-200 px-3 py-1.5 text-sm focus:border-brand focus:outline-none"
                      />
                      <button onClick={() => addCustomChip(q.id)} className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Add</button>
                    </div>
                  )}
                  {q.allow_custom && q.kind === "single" && (
                    <input
                      value={custom[q.id] ?? ""}
                      onChange={(e) => { setCustom((c) => ({ ...c, [q.id]: e.target.value })); setCurrent((c) => ({ ...c, [q.id]: "" })); }}
                      placeholder="Or type your own…"
                      className="w-full rounded-lg border border-gray-200 px-3 py-1.5 text-sm focus:border-brand focus:outline-none"
                    />
                  )}
                </fieldset>
              ))
            )}
            <div className="flex items-center justify-between pt-1">
              <div className="flex gap-2">
                {step > 1 && (
                  <button onClick={() => { setGoal(goal); setStage("intent"); }} disabled={busy} className="rounded-lg px-3 py-2 text-sm text-gray-500 hover:bg-gray-100 disabled:opacity-50">
                    Back
                  </button>
                )}
                <button onClick={() => generate(goal, [...answers, ...mergedForStep()])} disabled={busy} className="rounded-lg px-3 py-2 text-sm text-gray-500 hover:bg-gray-100 disabled:opacity-50">
                  Generate now
                </button>
              </div>
              <button onClick={submitStep} disabled={busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
                {busy ? "Thinking…" : "Continue"}
              </button>
            </div>
          </div>
          <aside className="rounded-xl border border-gray-200 bg-gray-50/50 p-4">
            <div className="mb-2 text-xs font-semibold text-gray-500">Pack so far</div>
            <PreviewPane />
          </aside>
        </div>
      )}

      {stage === "generating" && (
        <div className="flex flex-col items-center gap-3 py-10 text-gray-500">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-brand border-t-transparent motion-reduce:animate-none" />
          <p className="text-sm">Designing your insight pack… {elapsed > 2 ? `(${elapsed}s)` : ""}</p>
          <p className="text-xs text-gray-400">Reasoning models can take a moment.</p>
          <button onClick={cancelThinking} className="mt-1 rounded-lg px-3 py-1.5 text-sm text-gray-500 hover:bg-gray-100">Cancel</button>
        </div>
      )}

      {stage === "preview" && draft && (
        <div className="space-y-4">
          <div className="rounded-xl border border-gray-200 p-4">
            <div className="flex items-start gap-3">
              <div className="text-2xl leading-none">{draft.icon || "🧠"}</div>
              <div className="min-w-0">
                <div className="text-sm font-semibold text-gray-900">{draft.name}</div>
                <div className="text-xs text-gray-500">{draft.description}</div>
                {summary && <p className="mt-1.5 text-xs text-gray-500">{summary}</p>}
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {draft.sources.map((s) => (
                <span key={s} className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{s}</span>
              ))}
              <span className="rounded-full bg-brand/10 px-2 py-0.5 text-xs text-brand">
                {VERDICT_META[draft.materiality.notify_threshold]?.label ?? draft.materiality.notify_threshold}
              </span>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{draft.lookback_hours}h lookback</span>
            </div>
          </div>

          {draft.materiality.notify_threshold === "nothing_notable" && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              This pack notifies on <span className="font-medium">everything</span> — it may be noisy. You can raise the threshold on the next screen.
            </div>
          )}

          <div className="rounded-xl border border-gray-200 p-4">
            <div className="text-sm font-medium text-gray-800">Preview a sample run</div>
            <div className="text-xs text-gray-500">Test the pack against a real workload (read-only, no notification).</div>
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <select value={runWorkloadId} onChange={(e) => setRunWorkloadId(e.target.value)} className="flex-1 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                <option value="">Select a workload…</option>
                {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}{w.demo ? " (demo)" : ""}</option>)}
              </select>
              <button onClick={runSample} disabled={runBusy || !runWorkloadId} className="rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                {runBusy ? "Running…" : "Preview run"}
              </button>
            </div>
            {runBusy && <div className="mt-3 h-24 animate-pulse rounded-lg bg-gray-100" />}
            {sampleRun && !runBusy && (
              <div className="mt-3 rounded-lg border border-gray-100 bg-gray-50/60 p-3">
                <DigestView run={sampleRun} />
              </div>
            )}
          </div>

          <div className="flex justify-between">
            <button onClick={() => generate(goal, answers)} disabled={busy} className="rounded-lg px-3 py-2 text-sm text-gray-500 hover:bg-gray-100 disabled:opacity-50">
              Regenerate
            </button>
            <button onClick={() => onDraft(draft, summary)} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90">
              Continue to review &amp; save
            </button>
          </div>
        </div>
      )}

      {stage === "error" && (
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Close</button>
          <button onClick={() => generate(goal, answers)} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90">Retry</button>
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- pack form
const CATEGORIES = ["security", "change", "identity", "cost", "operations", "general"];
const MIN_RISKS = ["low", "medium", "high"];
const THRESHOLDS: { value: InsightVerdict; label: string }[] = [
  { value: "urgent", label: "Only urgent" },
  { value: "notable", label: "Notable or higher" },
  { value: "nothing_notable", label: "Everything (always notify)" },
];
const SCOPES = ["tenant", "subscription", "workload", "workload_dependencies"];
const LOOKBACK_PRESETS: { label: string; hours: number }[] = [
  { label: "6h", hours: 6 }, { label: "24h", hours: 24 }, { label: "7d", hours: 168 }, { label: "30d", hours: 720 },
];
const ICON_CHOICES = ["🧠", "🕵️", "🌐", "🛂", "💰", "🔑", "📡", "📏", "🛡️", "💾", "⚠️", "🔔", "📊", "🚨", "🧭", "🩺"];

// Human labels for the flattened diff fields the copilot returns.
const EDIT_FIELD_LABELS: Record<string, string> = {
  name: "Name", icon: "Icon", category: "Category", description: "Description",
  sources: "Data sources", supported_scopes: "Supported scopes", lookback_hours: "Lookback (hours)",
  min_risk: "Minimum risk", notify_threshold: "Notify threshold",
  always_notify_if: "Always-notify flags", instructions: "AI instructions",
};

function fmtDiffVal(field: string, v: unknown): string {
  if (v == null || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
  if (field === "instructions") {
    const s = String(v).replace(/\s+/g, " ").trim();
    return s.length > 70 ? s.slice(0, 70) + "…" : s || "—";
  }
  return String(v);
}

// Apply one flattened copilot change onto a pack (inverse of the backend _flat()).
function applyEditChange(pack: InsightPack, ch: InsightRefineChange): InsightPack {
  switch (ch.field) {
    case "name": return { ...pack, name: String(ch.after ?? "") };
    case "icon": return { ...pack, icon: String(ch.after ?? "") };
    case "category": return { ...pack, category: String(ch.after ?? "general") };
    case "description": return { ...pack, description: String(ch.after ?? "") };
    case "sources": return { ...pack, sources: (ch.after as string[]) ?? [] };
    case "supported_scopes": return { ...pack, supported_scopes: (ch.after as string[]) ?? [] };
    case "lookback_hours": return { ...pack, lookback_hours: Number(ch.after) || 24 };
    case "min_risk": return { ...pack, filters: { ...pack.filters, min_risk: String(ch.after ?? "low") } };
    case "notify_threshold": return { ...pack, materiality: { ...pack.materiality, notify_threshold: ch.after as InsightVerdict } };
    case "always_notify_if": return { ...pack, materiality: { ...pack.materiality, always_notify_if: (ch.after as string[]) ?? [] } };
    case "instructions": return { ...pack, instructions: String(ch.after ?? "") };
    default: return pack;
  }
}

// Wrap an AI-synthesized sample finding as an InsightRun so DigestView can render it.
function sampleToRun(pack: InsightPack, s: InsightSampleFinding): InsightRun {
  return {
    id: "ai-sample", pack_id: pack.id || "draft", pack_name: pack.name || "Untitled pack",
    pack_icon: pack.icon || "🧠", tenant_id: "", trigger: "sample",
    scope: { mode: "workload" }, scope_label: "example scope", lookback_hours: pack.lookback_hours,
    verdict: s.verdict, headline: s.headline, bullets: s.bullets,
    table: s.table.map((r) => ({ ...r, workload: "" })),
    counts: { changes: s.table.length, flags: [] }, sources: pack.sources,
    notified: false, gate_reason: "", status: "ok",
  };
}

function NoiseGauge({ score }: { score: number }) {
  const label = score <= 30 ? "Quiet" : score <= 60 ? "Balanced" : "Noisy";
  const bar = score <= 30 ? "bg-emerald-500" : score <= 60 ? "bg-amber-500" : "bg-red-500";
  const txt = score <= 30 ? "text-emerald-700" : score <= 60 ? "text-amber-700" : "text-red-700";
  return (
    <div>
      <div className="flex items-center justify-between text-[11px] font-medium text-gray-500">
        <span>Noise level</span><span className={txt}>{label}</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
        <div className={`h-full rounded-full ${bar} transition-all motion-reduce:transition-none`} style={{ width: `${score}%` }} />
      </div>
    </div>
  );
}

function toggleArr(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

type EditorTab = "preview" | "sample" | "review";

function PackForm({ initial, library, onClose, onSaved }: {
  initial: InsightPack;
  library: InsightPackLibrary;
  onClose: () => void;
  onSaved: (p: InsightPack) => void;
}) {
  const [pack, setPack] = useState<InsightPack>(initial);
  const [error, setError] = useState("");
  const [showErrors, setShowErrors] = useState(false);
  const qc = useQueryClient();

  // ---- undo / redo history (discrete edits + AI applies push; free typing does not)
  const [past, setPast] = useState<InsightPack[]>([]);
  const [future, setFuture] = useState<InsightPack[]>([]);
  function applyPack(next: InsightPack) { setPast((p) => [...p, pack].slice(-50)); setFuture([]); setPack(next); }
  function setField<K extends keyof InsightPack>(k: K, v: InsightPack[K]) { setPack((p) => ({ ...p, [k]: v })); }
  function undo() {
    if (!past.length) return;
    const prev = past[past.length - 1];
    setFuture((f) => [pack, ...f]); setPack(prev); setPast((p) => p.slice(0, -1));
  }
  function redo() {
    if (!future.length) return;
    const nxt = future[0];
    setPast((p) => [...p, pack]); setPack(nxt); setFuture((f) => f.slice(1));
  }

  // ---- AI copilot
  const [command, setCommand] = useState("");
  const [aiBusy, setAiBusy] = useState<InsightRefineMode | null>(null);
  const [aiError, setAiError] = useState("");
  const [proposal, setProposal] = useState<{ changes: InsightRefineChange[]; rationale: string } | null>(null);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<EditorTab>("preview");
  const [explanation, setExplanation] = useState("");
  const [findings, setFindings] = useState<InsightCritiqueFinding[] | null>(null);
  const [activeSample, setActiveSample] = useState<{ kind: "ai" | "real"; run: InsightRun } | null>(null);

  // ---- real test run
  const [runWorkloadId, setRunWorkloadId] = useState("");
  const [runBusy, setRunBusy] = useState(false);
  const { data: wlData } = useQuery({ queryKey: ["changeWorkloads"], queryFn: () => api.changeExplorerWorkloads() });
  const workloads: ChangeWorkload[] = wlData?.workloads ?? [];

  const insRef = useRef<HTMLTextAreaElement>(null);
  const storageKey = `insightEditor.draft.${initial.id || "new"}`;
  const [restorable, setRestorable] = useState<InsightPack | null>(null);

  const save = useMutation({
    mutationFn: () => api.upsertInsightPack(pack),
    onSuccess: ({ pack: saved }) => {
      qc.invalidateQueries({ queryKey: ["insightPacks"] });
      try { sessionStorage.removeItem(storageKey); } catch { /* ignore */ }
      onSaved(saved);
    },
    onError: (e) => setError(formatError(e)),
  });

  const dirty = useMemo(() => JSON.stringify(pack) !== JSON.stringify(initial), [pack, initial]);

  // ---- validation
  const issues = useMemo(() => {
    const list: { field: string; msg: string; level: "error" | "warn" }[] = [];
    if (!pack.name.trim()) list.push({ field: "name", msg: "Name is required.", level: "error" });
    if (pack.sources.length === 0) list.push({ field: "sources", msg: "Pick at least one data source.", level: "error" });
    if (pack.instructions.trim().length < 40) list.push({ field: "instructions", msg: "Instructions look thin — describe what to prioritize and when to stay quiet.", level: "error" });
    if (!pack.instructions.includes("{{scope_label}}")) list.push({ field: "instructions", msg: "Missing the {{scope_label}} placeholder.", level: "warn" });
    if (!pack.instructions.includes("{{lookback_hours}}")) list.push({ field: "instructions", msg: "Missing the {{lookback_hours}} placeholder.", level: "warn" });
    const dupe = library.packs.find((p) => p.id !== pack.id && p.name.trim().toLowerCase() === pack.name.trim().toLowerCase() && pack.name.trim());
    if (dupe) list.push({ field: "name", msg: `Another pack is already named “${dupe.name}”.`, level: "warn" });
    if (pack.materiality.notify_threshold === "nothing_notable") list.push({ field: "notify_threshold", msg: "This notifies on everything — expect noise.", level: "warn" });
    return list;
  }, [pack, library.packs]);
  const errors = issues.filter((i) => i.level === "error");

  const completeness = useMemo(() => {
    let done = 0;
    if (pack.name.trim()) done++;
    if (pack.description.trim()) done++;
    if (pack.sources.length) done++;
    if (pack.instructions.trim().length >= 40) done++;
    return Math.round((done / 4) * 100);
  }, [pack]);

  const noise = useMemo(() => {
    const t = pack.materiality.notify_threshold;
    let s = t === "nothing_notable" ? 55 : t === "notable" ? 30 : 10;
    s += Math.min(30, pack.materiality.always_notify_if.length * 4);
    s += pack.filters.min_risk === "low" ? 12 : pack.filters.min_risk === "medium" ? 6 : 0;
    s += pack.lookback_hours >= 168 ? 8 : 0;
    return Math.min(100, s);
  }, [pack]);

  const summary = useMemo(() => {
    const srcs = pack.sources.map((id) => library.sources.find((s) => s.id === id)?.label ?? id);
    const scopeTxt = pack.supported_scopes.length ? pack.supported_scopes.join(", ") : "any scope";
    const thr = VERDICT_META[pack.materiality.notify_threshold]?.label ?? pack.materiality.notify_threshold;
    const always = pack.materiality.always_notify_if.length;
    const srcTxt = srcs.length ? srcs.join(", ") : "no data sources yet";
    return `Watches ${srcTxt} across ${scopeTxt} over the last ${pack.lookback_hours}h. Notifies on ${thr}${always ? `, and always for ${always} critical signal${always > 1 ? "s" : ""}` : ""}.`;
  }, [pack, library.sources]);

  // ---- resume: read any persisted draft once on mount
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (raw) {
        const snap = JSON.parse(raw) as InsightPack;
        if (snap && JSON.stringify(snap) !== JSON.stringify(initial)) setRestorable(snap);
      }
    } catch { /* ignore malformed */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- persist while dirty
  useEffect(() => {
    if (!dirty) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify(pack)); } catch { /* quota / disabled */ }
  }, [pack, dirty, storageKey]);

  // ---- keyboard: Ctrl/Cmd+S save, Ctrl/Cmd+Z undo, Esc close (guarded)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const meta = e.ctrlKey || e.metaKey;
      if (meta && e.key.toLowerCase() === "s") { e.preventDefault(); trySave(); }
      else if (meta && e.key.toLowerCase() === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if (meta && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  });

  function guardedClose() {
    if (dirty && !window.confirm("Discard unsaved changes to this pack?")) return;
    onClose();
  }
  function trySave() {
    if (errors.length) { setShowErrors(true); setTab("review"); return; }
    save.mutate();
  }

  function insertPlaceholder(text: string) {
    const el = insRef.current;
    const start = el?.selectionStart ?? pack.instructions.length;
    const end = el?.selectionEnd ?? start;
    const next = pack.instructions.slice(0, start) + text + pack.instructions.slice(end);
    setField("instructions", next);
    requestAnimationFrame(() => { if (el) { el.focus(); el.selectionStart = el.selectionEnd = start + text.length; } });
  }

  // ---- copilot dispatch
  async function runRefine(mode: InsightRefineMode, instruction = "") {
    setAiBusy(mode); setAiError("");
    try {
      const res = await api.refineInsightPack(pack, instruction, mode);
      if (mode === "command" || mode === "improve_instructions" || mode === "suggest") {
        if (res.changes && res.changes.length) {
          setProposal({ changes: res.changes, rationale: res.rationale ?? "" });
          setAccepted(new Set(res.changes.map((c) => c.field)));
        } else {
          setAiError(res.rationale || "The AI didn't suggest any changes.");
        }
      } else if (mode === "explain") {
        setExplanation(res.explanation ?? ""); setTab("preview");
      } else if (mode === "critique") {
        setFindings(res.findings ?? []); setTab("review");
      } else if (mode === "sample" && res.sample) {
        setActiveSample({ kind: "ai", run: sampleToRun(pack, res.sample) }); setTab("sample");
      }
    } catch (e) {
      setAiError(formatError(e));
    } finally {
      setAiBusy(null);
    }
  }

  function submitCommand() {
    const q = command.trim();
    if (!q || aiBusy) return;
    setCommand("");
    runRefine("command", q);
  }

  function applyProposal() {
    if (!proposal) return;
    let next = pack;
    for (const ch of proposal.changes) if (accepted.has(ch.field)) next = applyEditChange(next, ch);
    applyPack(next);
    setProposal(null);
  }

  async function runRealSample() {
    if (!runWorkloadId || runBusy) return;
    setRunBusy(true); setAiError("");
    try {
      const { run } = await api.runInsightPack({ pack, scope: { mode: "workload", workload_ids: [runWorkloadId] }, notify: false });
      setActiveSample({ kind: "real", run });
    } catch (e) {
      setAiError(formatError(e));
    } finally {
      setRunBusy(false);
    }
  }

  const inputCls = "mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none";
  const errRing = (field: string) => (showErrors && errors.some((e) => e.field === field) ? "border-red-400" : "border-gray-300");

  return (
    <Modal title={initial.id ? "Edit pack" : "New insight pack"} subtitle="Scope-agnostic definition — scope & schedule are chosen when you run it." onClose={guardedClose} size="5xl">
      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {restorable && (
        <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <span>You have unsaved edits from a previous session.</span>
          <div className="flex gap-2">
            <button onClick={() => { setPack(restorable); setRestorable(null); }} className="rounded-md bg-amber-600 px-2.5 py-1 font-medium text-white hover:bg-amber-700">Restore</button>
            <button onClick={() => { setRestorable(null); try { sessionStorage.removeItem(storageKey); } catch { /* ignore */ } }} className="rounded-md px-2.5 py-1 font-medium text-amber-700 hover:bg-amber-100">Dismiss</button>
          </div>
        </div>
      )}

      {/* completeness + undo header */}
      <div className="mb-3 flex items-center gap-3">
        <div className="flex-1">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-brand transition-all motion-reduce:transition-none" style={{ width: `${completeness}%` }} />
          </div>
        </div>
        <span className="text-[11px] text-gray-400">{completeness}% complete</span>
        <div className="flex gap-1">
          <button onClick={undo} disabled={!past.length} title="Undo (Ctrl+Z)" className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 disabled:opacity-40">↶</button>
          <button onClick={redo} disabled={!future.length} title="Redo (Ctrl+Y)" className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 disabled:opacity-40">↷</button>
        </div>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* ---------------------------------------------------------------- form pane */}
        <div className="max-h-[64vh] space-y-4 overflow-y-auto pr-1">
          <div className="flex gap-3">
            <div className="w-16">
              <label className="block text-xs font-medium text-gray-500">Icon</label>
              <input value={pack.icon} onChange={(e) => setField("icon", e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-2 py-2 text-center text-lg focus:border-brand focus:outline-none" />
            </div>
            <div className="flex-1">
              <label className="block text-xs font-medium text-gray-500">Name</label>
              <input value={pack.name} onChange={(e) => setField("name", e.target.value)} className={`mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:border-brand focus:outline-none ${errRing("name")}`} />
            </div>
            <div className="w-36">
              <label className="block text-xs font-medium text-gray-500">Category</label>
              <select value={pack.category} onChange={(e) => applyPack({ ...pack, category: e.target.value })} className={inputCls}>
                {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          </div>
          <div className="flex flex-wrap gap-1">
            {ICON_CHOICES.map((ic) => (
              <button key={ic} onClick={() => setField("icon", ic)} className={`rounded-md px-1.5 py-1 text-base hover:bg-gray-100 ${pack.icon === ic ? "bg-brand/10 ring-1 ring-brand" : ""}`}>{ic}</button>
            ))}
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500">Description</label>
            <input value={pack.description} onChange={(e) => setField("description", e.target.value)} className={inputCls} placeholder="One sentence on what this pack watches" />
          </div>

          <div>
            <div className="flex items-center justify-between">
              <label className="block text-xs font-medium text-gray-500">Data sources</label>
              <button onClick={() => runRefine("suggest")} disabled={!!aiBusy} className="text-[11px] font-medium text-brand hover:underline disabled:opacity-50">
                {aiBusy === "suggest" ? "Thinking…" : "✨ Suggest"}
              </button>
            </div>
            <div className="mt-1 flex flex-wrap gap-2">
              {library.sources.map((s) => {
                const on = pack.sources.includes(s.id);
                return (
                  <button key={s.id} title={s.description} onClick={() => applyPack({ ...pack, sources: toggleArr(pack.sources, s.id) })}
                    className={`rounded-full border px-3 py-1.5 text-sm ${on ? "border-brand bg-brand/10 text-brand" : `${errRing("sources")} text-gray-600 hover:bg-gray-50`}`}>
                    {s.icon} {s.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-500">Lookback (hours)</label>
              <input type="number" min={1} value={pack.lookback_hours} onChange={(e) => setField("lookback_hours", Number(e.target.value) || 24)} className={inputCls} />
              <div className="mt-1.5 flex gap-1">
                {LOOKBACK_PRESETS.map((p) => (
                  <button key={p.hours} onClick={() => applyPack({ ...pack, lookback_hours: p.hours })}
                    className={`rounded-md px-2 py-0.5 text-[11px] ${pack.lookback_hours === p.hours ? "bg-brand/10 text-brand" : "bg-gray-100 text-gray-500 hover:bg-gray-200"}`}>{p.label}</button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500">Minimum risk to include</label>
              <select value={pack.filters.min_risk ?? "low"} onChange={(e) => applyPack({ ...pack, filters: { ...pack.filters, min_risk: e.target.value } })} className={inputCls}>
                {MIN_RISKS.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500">Supported scopes</label>
            <div className="mt-1 flex flex-wrap gap-2">
              {SCOPES.map((s) => {
                const on = pack.supported_scopes.includes(s);
                return (
                  <button key={s} onClick={() => applyPack({ ...pack, supported_scopes: toggleArr(pack.supported_scopes, s) })}
                    className={`rounded-full border px-3 py-1.5 text-xs ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                    {s}
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500">Notify threshold</label>
            <select value={pack.materiality.notify_threshold} onChange={(e) => applyPack({ ...pack, materiality: { ...pack.materiality, notify_threshold: e.target.value as InsightVerdict } })} className={inputCls}>
              {THRESHOLDS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500">Always notify if these are detected</label>
            <div className="mt-1 flex flex-wrap gap-2">
              {library.flag_codes.map((f) => {
                const on = pack.materiality.always_notify_if.includes(f.code);
                return (
                  <button key={f.code} title={f.code} onClick={() => applyPack({ ...pack, materiality: { ...pack.materiality, always_notify_if: toggleArr(pack.materiality.always_notify_if, f.code) } })}
                    className={`rounded-full border px-2.5 py-1 text-xs ${on ? "border-red-300 bg-red-50 text-red-700" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                    {f.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between">
              <label className="block text-xs font-medium text-gray-500">AI instructions</label>
              <div className="flex items-center gap-2">
                <button onClick={() => insertPlaceholder("{{scope_label}}")} className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10px] font-mono text-gray-600 hover:bg-gray-200">+scope_label</button>
                <button onClick={() => insertPlaceholder("{{lookback_hours}}")} className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10px] font-mono text-gray-600 hover:bg-gray-200">+lookback_hours</button>
                <button onClick={() => runRefine("improve_instructions")} disabled={!!aiBusy} className="text-[11px] font-medium text-brand hover:underline disabled:opacity-50">
                  {aiBusy === "improve_instructions" ? "Improving…" : "✨ Improve"}
                </button>
              </div>
            </div>
            <textarea ref={insRef} value={pack.instructions} onChange={(e) => setField("instructions", e.target.value)} rows={9}
              className={`mt-1 w-full rounded-lg border px-3 py-2 font-mono text-xs focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand ${errRing("instructions")}`} />
            <div className="mt-1 flex justify-between text-[11px] text-gray-400">
              <span>{pack.instructions.length} chars</span>
              <span>Use {"{{scope_label}}"} & {"{{lookback_hours}}"}</span>
            </div>
          </div>
        </div>

        {/* ---------------------------------------------------------------- copilot pane */}
        <div className="flex max-h-[64vh] flex-col rounded-xl border border-gray-200 bg-gray-50/60">
          <div className="flex shrink-0 border-b border-gray-200 text-xs font-medium">
            {(["preview", "sample", "review"] as EditorTab[]).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`flex-1 px-3 py-2 capitalize ${tab === t ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:bg-gray-100"}`}>
                {t}{t === "review" && issues.length ? ` (${issues.length})` : ""}
              </button>
            ))}
          </div>

          {/* proposal diff (shown above any tab when the AI proposes changes) */}
          {proposal && (
            <div className="shrink-0 border-b border-brand/20 bg-brand/5 p-3">
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-brand">✨ AI proposes {proposal.changes.length} change{proposal.changes.length > 1 ? "s" : ""}</div>
              {proposal.rationale && <p className="mb-2 text-[11px] text-gray-500">{proposal.rationale}</p>}
              <div className="space-y-1.5">
                {proposal.changes.map((ch) => (
                  <label key={ch.field} className="flex cursor-pointer items-start gap-2 rounded-md bg-white/70 px-2 py-1.5 text-[11px]">
                    <input type="checkbox" checked={accepted.has(ch.field)} onChange={() => setAccepted((s) => { const n = new Set(s); n.has(ch.field) ? n.delete(ch.field) : n.add(ch.field); return n; })} className="mt-0.5" />
                    <span className="min-w-0">
                      <span className="font-medium text-gray-700">{EDIT_FIELD_LABELS[ch.field] ?? ch.field}</span>
                      <span className="block text-gray-400 line-through">{fmtDiffVal(ch.field, ch.before)}</span>
                      <span className="block text-gray-700">{fmtDiffVal(ch.field, ch.after)}</span>
                    </span>
                  </label>
                ))}
              </div>
              <div className="mt-2 flex justify-end gap-2">
                <button onClick={() => setProposal(null)} className="rounded-md px-2.5 py-1 text-[11px] text-gray-500 hover:bg-gray-100">Discard</button>
                <button onClick={applyProposal} disabled={!accepted.size} className="rounded-md bg-brand px-2.5 py-1 text-[11px] font-medium text-white hover:bg-brand/90 disabled:opacity-50">Apply {accepted.size || ""}</button>
              </div>
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {aiError && <div className="mb-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-[11px] text-red-700">{aiError}</div>}

            {tab === "preview" && (
              <div className="space-y-3">
                <p className="text-sm text-gray-700">{summary}</p>
                <div className="flex flex-wrap gap-1.5">
                  {pack.sources.map((s) => <span key={s} className="rounded-full bg-white px-2 py-0.5 text-[11px] text-gray-600 ring-1 ring-gray-200">{library.sources.find((x) => x.id === s)?.label ?? s}</span>)}
                  <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[11px] text-brand">{VERDICT_META[pack.materiality.notify_threshold]?.label}</span>
                  <span className="rounded-full bg-white px-2 py-0.5 text-[11px] text-gray-600 ring-1 ring-gray-200">{pack.lookback_hours}h</span>
                </div>
                <NoiseGauge score={noise} />
                <button onClick={() => runRefine("explain")} disabled={!!aiBusy} className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {aiBusy === "explain" ? "Explaining…" : "✨ Explain this pack"}
                </button>
                {explanation && <div className="whitespace-pre-line rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-600">{explanation}</div>}
              </div>
            )}

            {tab === "sample" && (
              <div className="space-y-3">
                <button onClick={() => runRefine("sample")} disabled={!!aiBusy} className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {aiBusy === "sample" ? "Generating…" : "✨ Generate an example notification"}
                </button>
                <div className="rounded-lg border border-gray-200 bg-white p-3">
                  <div className="text-[11px] font-medium text-gray-500">Real test run</div>
                  <div className="mt-1 text-[11px] text-gray-400">Runs against a real workload, read-only, no notification.</div>
                  <div className="mt-2 flex gap-2">
                    <select value={runWorkloadId} onChange={(e) => setRunWorkloadId(e.target.value)} className="min-w-0 flex-1 rounded-lg border border-gray-300 px-2 py-1.5 text-xs focus:border-brand focus:outline-none">
                      <option value="">Select a workload…</option>
                      {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}{w.demo ? " (demo)" : ""}</option>)}
                    </select>
                    <button onClick={runRealSample} disabled={runBusy || !runWorkloadId} className="rounded-lg bg-gray-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                      {runBusy ? "Running…" : "Run"}
                    </button>
                  </div>
                </div>
                {(aiBusy === "sample" || runBusy) && <div className="h-24 animate-pulse rounded-lg bg-gray-100 motion-reduce:animate-none" />}
                {activeSample && !runBusy && aiBusy !== "sample" && (
                  <div className="rounded-lg border border-gray-100 bg-white p-3">
                    <div className="mb-1.5 text-[11px] font-medium text-gray-400">{activeSample.kind === "ai" ? "AI example (illustrative, not real data)" : "Real test run"}</div>
                    <DigestView run={activeSample.run} />
                  </div>
                )}
              </div>
            )}

            {tab === "review" && (
              <div className="space-y-3">
                {issues.length === 0 && <p className="text-xs text-emerald-700">No validation issues.</p>}
                {issues.map((i, idx) => (
                  <div key={idx} className={`rounded-lg border px-2.5 py-1.5 text-[11px] ${i.level === "error" ? "border-red-200 bg-red-50 text-red-700" : "border-amber-200 bg-amber-50 text-amber-800"}`}>
                    <span className="font-medium">{EDIT_FIELD_LABELS[i.field] ?? i.field}:</span> {i.msg}
                  </div>
                ))}
                <button onClick={() => runRefine("critique")} disabled={!!aiBusy} className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {aiBusy === "critique" ? "Reviewing…" : "✨ Run AI review"}
                </button>
                {findings && findings.length === 0 && <p className="text-xs text-emerald-700">AI found nothing to flag. 👍</p>}
                {findings?.map((f, idx) => (
                  <div key={idx} className="rounded-lg border border-gray-200 bg-white p-2.5 text-[11px]">
                    <div className="flex items-center gap-1.5">
                      <span className={`rounded px-1.5 py-0.5 font-medium ${f.severity === "high" ? "bg-red-100 text-red-700" : f.severity === "medium" ? "bg-amber-100 text-amber-800" : "bg-gray-100 text-gray-600"}`}>{f.severity}</span>
                      {f.field && <span className="text-gray-400">{EDIT_FIELD_LABELS[f.field] ?? f.field}</span>}
                    </div>
                    <p className="mt-1 text-gray-600">{f.message}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* AI command bar */}
          <div className="shrink-0 border-t border-gray-200 p-2.5">
            <div className="flex gap-2">
              <input value={command} onChange={(e) => setCommand(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") submitCommand(); }}
                placeholder="Tell AI to change this pack…" className="min-w-0 flex-1 rounded-lg border border-gray-300 px-3 py-2 text-xs focus:border-brand focus:outline-none" />
              <button onClick={submitCommand} disabled={!command.trim() || !!aiBusy} className="rounded-lg bg-brand px-3 py-2 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-50">
                {aiBusy === "command" ? "…" : "Send"}
              </button>
            </div>
            <div className="mt-1.5 text-[10px] text-gray-400">e.g. “make it quieter and only for production” · “add cost waste detection”</div>
          </div>
        </div>
      </div>

      {/* footer */}
      <div className="mt-4 flex items-center justify-between border-t border-gray-100 pt-4">
        <span className="text-[11px] text-gray-400">{dirty ? "Unsaved changes" : "No changes"}</span>
        <div className="flex gap-2">
          <button onClick={guardedClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Cancel</button>
          <button onClick={trySave} disabled={save.isPending} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
            {save.isPending ? "Saving…" : "Save pack"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- run / schedule dialog
type ScopeMode = InsightScope["mode"];

const SCHEDULE_TIMEZONES: string[] = (() => {
  let zones: string[] = [];
  try {
    const sv = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
    if (sv) zones = sv("timeZone");
  } catch {
    /* older browsers */
  }
  if (zones.length === 0) zones = ["America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Asia/Dubai", "Asia/Tokyo", "Australia/Sydney"];
  return ["UTC", ...zones.filter((z) => z !== "UTC")];
})();

function RunScheduleDialog({ pack, onClose, initialWorkloadId, initialWorkloadName }: {
  pack: InsightPack;
  onClose: () => void;
  initialWorkloadId?: string;
  initialWorkloadName?: string;
}) {
  const qc = useQueryClient();
  const { data: wlData } = useQuery({ queryKey: ["changeWorkloads"], queryFn: () => api.changeExplorerWorkloads() });
  const workloads: ChangeWorkload[] = wlData?.workloads ?? [];

  const supported = pack.supported_scopes.length ? pack.supported_scopes : ["workload"];
  const [mode, setMode] = useState<ScopeMode>(supported[0] as ScopeMode);
  const [workloadId, setWorkloadId] = useState(initialWorkloadId ?? "");
  const [notify, setNotify] = useState(false);
  const [runResult, setRunResult] = useState<InsightRun | null>(null);
  const [error, setError] = useState("");

  // scheduling
  const [scheduleKind, setScheduleKind] = useState<"daily" | "weekly" | "cron">("daily");
  const [cronMode, setCronMode] = useState<"builder" | "raw">("builder");
  const [cronExpr, setCronExpr] = useState("0 9 * * 1-5");
  const [time, setTime] = useState("08:00");
  const [weekday, setWeekday] = useState(1);
  const [timezone, setTimezone] = useState("UTC");
  const [preview, setPreview] = useState<{ valid: boolean; error: string | null; next_run_at: string | null; next_runs: string[]; schedule_label: string | null } | null>(null);

  // Live cadence preview (also validates cron), mirroring the Scheduled Tasks editor.
  useEffect(() => {
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const r = await api.previewSchedule({
          schedule_kind: scheduleKind,
          cron_expr: scheduleKind === "cron" ? cronExpr : null,
          time_of_day: time,
          weekday: scheduleKind === "weekly" ? weekday : 0,
          timezone,
        });
        if (!cancelled) setPreview(r);
      } catch {
        if (!cancelled) setPreview(null);
      }
    }, 300);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [scheduleKind, cronExpr, time, weekday, timezone]);

  const selectedWl = workloads.find((w) => w.id === workloadId);

  function buildScope(): InsightScope | null {
    if (!workloadId) { setError("Pick a workload to anchor the scope."); return null; }
    return {
      mode,
      workload_ids: [workloadId],
      workload_names: selectedWl ? [selectedWl.name] : (initialWorkloadName ? [initialWorkloadName] : undefined),
      connection_id: selectedWl?.connection_id,
    };
  }

  const runJob = useRunJob((r) => {
    setRunResult(r);
    qc.invalidateQueries({ queryKey: ["insightRuns"] });
    qc.invalidateQueries({ queryKey: ["insightLatest"] });
  });

  function startRun() {
    const scope = buildScope();
    if (!scope) return;
    setError("");
    setRunResult(null);
    runJob.start({ pack_id: pack.id, scope, notify });
  }

  const schedule = useMutation({
    mutationFn: () => {
      const scope = buildScope();
      if (!scope) throw new Error("Pick a workload to anchor the scope.");
      return api.upsertTask({
        name: `${pack.name} — ${selectedWl?.name ?? "scope"}`,
        target_type: "insight_pack",
        target_config: { pack_id: pack.id, scope },
        schedule_kind: scheduleKind,
        cron_expr: scheduleKind === "cron" ? cronExpr : null,
        time_of_day: time,
        weekday: scheduleKind === "weekly" ? weekday : null,
        timezone,
        run_mode: "auto",
        status: "on",
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insightUpcoming"] });
      onClose();
    },
    onError: (e) => setError(formatError(e)),
  });

  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  return (
    <Modal title={`Run or schedule · ${pack.name}`} subtitle="Pick a scope, then run it now or set it on a recurring schedule." onClose={onClose} wide>
      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      <div className="space-y-4">
        <div>
          <label className="block text-xs font-medium text-gray-500">Scope</label>
          <div className="mt-1 flex flex-wrap gap-2">
            {supported.map((s) => (
              <button key={s} onClick={() => setMode(s as ScopeMode)}
                className={`rounded-full border px-3 py-1.5 text-xs ${mode === s ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                {s === "workload_dependencies" ? "workload + deps" : s}
              </button>
            ))}
          </div>
          <p className="mt-1 text-[11px] text-gray-400">
            {mode === "tenant" ? "Scans the whole tenant reachable via the selected workload's connection." :
             mode === "subscription" ? "Scans the subscription of the selected workload." :
             mode === "workload_dependencies" ? "Scans the workload and its dependencies." : "Scans just the selected workload."}
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">Anchor workload</label>
          <select value={workloadId} onChange={(e) => setWorkloadId(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
            <option value="">Select a workload…</option>
            {workloadId && !selectedWl && <option value={workloadId}>{initialWorkloadName ?? "Selected workload"}</option>}
            {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}{w.demo ? " (demo)" : ""}</option>)}
          </select>
        </div>

        {/* Run now */}
        <div className="rounded-xl border border-gray-200 p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-gray-800">Run now</div>
              <div className="text-xs text-gray-500">Generate a digest immediately.</div>
            </div>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-1.5 text-xs text-gray-600">
                <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
                Send notification
              </label>
              <button onClick={startRun} disabled={runJob.isRunning} className="rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                {runJob.isRunning ? "Running…" : runResult ? "Run again" : "Run"}
              </button>
            </div>
          </div>
          {(runJob.isRunning || (runJob.job && !runResult)) && (
            <div className="mt-4 border-t border-gray-100 pt-4">
              <RunProgress job={runJob.job} starting={runJob.starting} />
            </div>
          )}
          {runResult && (
            <div className="mt-4 border-t border-gray-100 pt-4">
              <DigestView run={runResult} />
            </div>
          )}
        </div>

        {/* Schedule */}
        <div className="rounded-xl border border-gray-200 p-4">
          <div className="text-sm font-medium text-gray-800">Schedule</div>
          <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label className="block text-xs text-gray-500">Frequency</label>
              <select
                value={scheduleKind !== "cron" ? scheduleKind : cronMode}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "daily" || v === "weekly") setScheduleKind(v);
                  else if (v === "builder") { setScheduleKind("cron"); setCronMode("builder"); }
                  else { setScheduleKind("cron"); setCronMode("raw"); }
                }}
                className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="builder">Advanced (recurrence builder)</option>
                <option value="raw">Custom (cron expression)</option>
              </select>
            </div>
            {scheduleKind === "weekly" && (
              <div>
                <label className="block text-xs text-gray-500">Day</label>
                <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                  {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
                </select>
              </div>
            )}
            {scheduleKind !== "cron" && (
              <div>
                <label className="block text-xs text-gray-500">Time</label>
                <input type="time" value={time} onChange={(e) => setTime(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none" />
              </div>
            )}
            <div>
              <label className="block text-xs text-gray-500">Timezone</label>
              <select value={timezone} onChange={(e) => setTimezone(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                {SCHEDULE_TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
              </select>
            </div>
          </div>

          {scheduleKind === "cron" && cronMode === "builder" && (
            <div className="mt-3">
              <RecurrenceBuilder value={cronExpr} onChange={setCronExpr} />
            </div>
          )}
          {scheduleKind === "cron" && cronMode === "raw" && (
            <div className="mt-3">
              <label className="block text-xs text-gray-500">Cron expression</label>
              <input value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} placeholder="0 8 * * *" className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-sm focus:border-brand focus:outline-none" />
              <div className="mt-1 flex flex-wrap gap-1">
                {[["Hourly","0 * * * *"],["Daily 08:00","0 8 * * *"],["Weekdays 09:00","0 9 * * 1-5"],["Weekly Mon","0 9 * * 1"],["Monthly 1st","0 9 1 * *"]].map(([lbl, expr]) => (
                  <button key={expr} type="button" onClick={() => setCronExpr(expr)} className="rounded border border-gray-200 bg-white px-1.5 py-0.5 font-mono text-[10px] text-gray-500 hover:bg-gray-50 hover:text-gray-700">{lbl}</button>
                ))}
              </div>
            </div>
          )}

          {/* Live cadence preview */}
          <div className="mt-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs">
            {preview === null ? (
              <span className="text-gray-400">Computing next run…</span>
            ) : preview.valid ? (
              <div className="space-y-1">
                <div className="text-gray-600">
                  <span className="font-medium text-gray-700">{preview.schedule_label}</span>
                  {preview.next_run_at && (
                    <>{" · "}Next run <span className="font-medium text-gray-800">{formatTimestamp(preview.next_run_at)}</span>{" "}
                      <span className="text-gray-400">({formatRelativeFromNow(preview.next_run_at)})</span></>
                  )}
                </div>
                {(preview.next_runs?.length ?? 0) > 1 && (
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-gray-400">
                    <span className="text-gray-500">Upcoming:</span>
                    {preview.next_runs.slice(0, 5).map((r, i) => (
                      <span key={i} title={formatRelativeFromNow(r)}>{formatTimestamp(r)}</span>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <span className="text-red-600">✗ {preview.error}</span>
            )}
          </div>

          <div className="mt-3">
            <button onClick={() => schedule.mutate()} disabled={schedule.isPending || (scheduleKind === "cron" && preview !== null && !preview.valid)} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
              {schedule.isPending ? "Scheduling…" : "Create schedule"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- pack card
function CollectionManager({ collections, counts, onCreate, onRename, onDelete, busy }: {
  collections: InsightCollection[];
  counts: Record<string, number>;
  onCreate: (name: string) => void;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string) => void;
  busy?: boolean;
}) {
  const [newName, setNewName] = useState("");
  return (
    <div className="mb-4 rounded-xl border border-gray-200 bg-white p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-700">📁 Collections</span>
        <span className="text-[11px] text-gray-400">Organize packs into custom folders</span>
      </div>
      {collections.length === 0 ? (
        <p className="text-xs text-gray-400">No collections yet. Create one below, then file packs from each pack’s 📁 menu.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {collections.map((c) => (
            <div key={c.id} className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-xs">
              <span>{c.icon || "📁"}</span>
              <span className="font-medium text-gray-700">{c.name}</span>
              <span className="rounded-full bg-gray-200 px-1.5 text-[10px] text-gray-500">{counts[c.id] ?? 0}</span>
              <button onClick={() => { const n = prompt("Rename collection", c.name); if (n && n.trim()) onRename(c.id, n.trim()); }}
                disabled={busy} className="ml-1 text-gray-400 hover:text-gray-600" title="Rename">✎</button>
              <button onClick={() => { if (confirm(`Delete collection "${c.name}"? Packs stay, just un-filed.`)) onDelete(c.id); }}
                disabled={busy} className="text-red-400 hover:text-red-600" title="Delete">✕</button>
            </div>
          ))}
        </div>
      )}
      <form onSubmit={(e) => { e.preventDefault(); const n = newName.trim(); if (n) { onCreate(n); setNewName(""); } }}
        className="mt-2 flex items-center gap-1.5">
        <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New collection name…"
          className="w-56 rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs focus:border-brand focus:outline-none" />
        <button type="submit" disabled={!newName.trim() || busy} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-40">Create</button>
      </form>
    </div>
  );
}

function CollectionPicker({ pack, collections, onChange, onCreate, busy }: {
  pack: InsightPack;
  collections: InsightCollection[];
  onChange: (ids: string[]) => void;
  onCreate: (name: string) => void;
  busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const ids = new Set(pack.collection_ids ?? []);
  const toggle = (id: string) => {
    const next = new Set(ids);
    if (next.has(id)) next.delete(id); else next.add(id);
    onChange([...next]);
  };
  return (
    <div className="relative">
      <button onClick={() => setOpen((o) => !o)} title="Add to collections"
        className={`rounded-lg px-2.5 py-1.5 text-xs hover:bg-gray-100 ${ids.size ? "text-brand" : "text-gray-600"}`}>
        📁{ids.size ? ` ${ids.size}` : ""}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-20 mt-1 w-56 rounded-xl border border-gray-200 bg-white p-2 shadow-lg">
            <div className="px-1 pb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Collections</div>
            <div className="max-h-48 space-y-0.5 overflow-y-auto">
              {collections.length === 0 && <div className="px-1 py-1 text-xs text-gray-400">No collections yet.</div>}
              {collections.map((c) => (
                <label key={c.id} className="flex cursor-pointer items-center gap-2 rounded-lg px-1.5 py-1 text-xs hover:bg-gray-50">
                  <input type="checkbox" checked={ids.has(c.id)} onChange={() => toggle(c.id)} disabled={busy} className="h-3.5 w-3.5 rounded border-gray-300" />
                  <span>{c.icon || "📁"}</span><span className="truncate">{c.name}</span>
                </label>
              ))}
            </div>
            <form onSubmit={(e) => { e.preventDefault(); const n = newName.trim(); if (n) { onCreate(n); setNewName(""); } }}
              className="mt-1 flex items-center gap-1 border-t border-gray-100 pt-1.5">
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New collection…"
                className="min-w-0 flex-1 rounded-lg border border-gray-300 px-2 py-1 text-xs focus:border-brand focus:outline-none" />
              <button type="submit" disabled={!newName.trim() || busy} className="rounded-lg bg-brand px-2 py-1 text-xs font-medium text-white disabled:opacity-40">Add</button>
            </form>
          </div>
        </>
      )}
    </div>
  );
}

function PackCard({ pack, onRun, onEdit, onClone, onDelete, onToggle, latest, health, onHistory, selectable, selected, onSelectChange, pinned, onTogglePin, collections, onSetCollections, onCreateCollection, collectionBusy, view = "grid" }: {
  pack: InsightPack;
  onRun: () => void;
  onEdit: () => void;
  onClone: () => void;
  onDelete: () => void;
  onToggle: () => void;
  latest?: InsightRun;
  health?: InsightPackHealth;
  onHistory?: () => void;
  selectable?: boolean;
  selected?: boolean;
  onSelectChange?: (v: boolean) => void;
  pinned?: boolean;
  onTogglePin?: () => void;
  collections?: InsightCollection[];
  onSetCollections?: (ids: string[]) => void;
  onCreateCollection?: (name: string) => void;
  collectionBusy?: boolean;
  view?: "grid" | "list";
}) {
  const snooze = snoozeInfo(pack);
  const noisy = !!health && health.notified >= 5 && health.noise_score >= 0.6;
  const pinStar = onTogglePin ? (
    <button onClick={onTogglePin} title={pinned ? "Unpin" : "Pin to top"}
      className={`text-base leading-none transition ${pinned ? "text-amber-500" : "text-gray-300 hover:text-gray-400"}`}>
      {pinned ? "★" : "☆"}
    </button>
  ) : null;
  const collectionMenu = collections && onSetCollections ? (
    <CollectionPicker pack={pack} collections={collections} onChange={onSetCollections}
      onCreate={(name) => onCreateCollection?.(name)} busy={collectionBusy} />
  ) : null;
  const snoozeBadge = snooze.active ? <span className="inline-flex items-center gap-1 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-medium text-indigo-600" title={`Muted until ${new Date(snooze.until!).toLocaleString()}`}>😴 Snoozed</span> : null;
  const noisyBadge = noisy ? <span className="inline-flex items-center gap-1 rounded bg-orange-50 px-1.5 py-0.5 text-[10px] font-medium text-orange-600" title="Notifies often — consider raising the threshold">🔊 Noisy</span> : null;

  if (view === "list") {
    return (
      <div className={`flex items-center gap-3 rounded-xl border bg-white px-3 py-2 transition hover:shadow-sm ${selected ? "border-brand ring-1 ring-brand/30" : "border-gray-200"} ${!pack.enabled ? "opacity-75" : ""}`}>
        {selectable
          ? <input type="checkbox" checked={!!selected} onChange={(e) => onSelectChange?.(e.target.checked)} className="h-4 w-4 rounded border-gray-300" />
          : pinStar}
        <span className="text-lg leading-none">{pack.icon || "🧠"}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-900">{pack.name}</span>
            {pack.builtin && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-500">Built-in</span>}
            {!pack.enabled && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-400">Off</span>}
            {snoozeBadge}{noisyBadge}
          </div>
          <div className="truncate text-[11px] text-gray-400">{pack.category} · {pack.sources.join(", ")} · last {pack.lookback_hours}h</div>
        </div>
        {health && health.runs_total > 0 && <button onClick={onHistory} title="Trends & history"><Sparkline values={health.spark} /></button>}
        {latest && <VerdictBadge verdict={latest.verdict} />}
        <div className="flex shrink-0 items-center gap-1">
          {collectionMenu}
          <button onClick={onRun} className="rounded-lg bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand/90">Run</button>
          <button onClick={onEdit} className="rounded-lg px-2 py-1 text-xs text-gray-600 hover:bg-gray-100">Edit</button>
          {!selectable && (
            <button onClick={onToggle} title={pack.enabled ? "Enabled" : "Disabled"}
              className={`relative h-5 w-9 rounded-full transition ${pack.enabled ? "bg-brand" : "bg-gray-300"}`}>
              <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${pack.enabled ? "left-4" : "left-0.5"}`} />
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={`flex flex-col rounded-2xl border bg-white p-4 shadow-sm transition hover:shadow-md ${selected ? "border-brand ring-1 ring-brand/30" : "border-gray-200"} ${!pack.enabled ? "opacity-75" : ""}`}>
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="text-2xl leading-none">{pack.icon || "🧠"}</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-900">{pack.name}</span>
              {pack.builtin && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-500">Built-in</span>}
              {!pack.enabled && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-400">Off</span>}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              <span className="inline-block rounded bg-brand/5 px-1.5 py-0.5 text-[11px] font-medium text-brand">{pack.category}</span>
              {snoozeBadge}
              {noisyBadge}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {pinStar}
          {selectable ? (
            <input type="checkbox" checked={!!selected} onChange={(e) => onSelectChange?.(e.target.checked)} className="h-4 w-4 rounded border-gray-300" />
          ) : (
            <button onClick={onToggle} title={pack.enabled ? "Enabled" : "Disabled"}
              className={`relative h-5 w-9 rounded-full transition ${pack.enabled ? "bg-brand" : "bg-gray-300"}`}>
              <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${pack.enabled ? "left-4" : "left-0.5"}`} />
            </button>
          )}
        </div>
      </div>

      <p className="mt-2 line-clamp-2 text-xs text-gray-500">{pack.description}</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {pack.sources.map((s) => <span key={s} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{s}</span>)}
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">last {pack.lookback_hours}h</span>
        {pack.materiality.always_notify_if.length > 0 && (
          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] text-red-600">{pack.materiality.always_notify_if.length} always-notify</span>
        )}
      </div>

      {health && health.runs_total > 0 && (
        <button onClick={onHistory} className="mt-3 flex items-center gap-2 rounded-lg bg-gray-50 px-2.5 py-1.5 text-left transition hover:bg-gray-100" title="View trends & history">
          <Sparkline values={health.spark} />
          <span className="text-[11px] text-gray-500">{health.runs_total} runs · {Math.round(health.noise_score * 100)}% notify</span>
          <span className="ml-auto text-[11px] text-brand">History →</span>
        </button>
      )}

      {latest && (
        <div className="mt-2 flex items-center gap-2 rounded-lg bg-gray-50 px-2.5 py-1.5">
          <VerdictBadge verdict={latest.verdict} />
          <span className="truncate text-[11px] text-gray-500">{latest.headline}</span>
        </div>
      )}

      <div className="mt-4 flex items-center gap-1.5 border-t border-gray-100 pt-3">
        <button onClick={onRun} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90">Run / schedule</button>
        <button onClick={onEdit} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100">Edit</button>
        <button onClick={onClone} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100">Clone</button>
        {collectionMenu}
        {!pack.builtin && <button onClick={onDelete} className="ml-auto rounded-lg px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50">Delete</button>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- trends / coverage widgets
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-3 py-2">
      <div className="text-lg font-semibold text-gray-900">{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-gray-400">{label}</div>
    </div>
  );
}

function CompareCard({ title, run }: { title: string; run: InsightRun }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400">{title}</span>
        <span className="text-[11px] text-gray-400">{timeAgo(run.created_at)}</span>
      </div>
      <VerdictBadge verdict={run.verdict} />
      <p className="mt-1.5 line-clamp-2 text-xs text-gray-600">{run.headline}</p>
      <div className="mt-1.5 text-[11px] text-gray-400">
        {run.counts.changes} change(s){run.counts.flags.length ? ` · ${run.counts.flags.length} flag(s)` : ""}
      </div>
    </div>
  );
}

function SnoozeMenu({ onPick, disabled }: { onPick: (days: number) => void; disabled?: boolean }) {
  return (
    <select disabled={disabled} value=""
      onChange={(e) => { const d = Number(e.target.value); e.currentTarget.value = ""; if (d) onPick(d); }}
      className="rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50 focus:border-brand focus:outline-none disabled:opacity-50">
      <option value="">Snooze…</option>
      <option value="1">Mute 1 day</option>
      <option value="3">Mute 3 days</option>
      <option value="7">Mute 7 days</option>
      <option value="30">Mute 30 days</option>
    </select>
  );
}

function PackHistoryDrawer({ pack, health, runs, loading, onClose, onOpenRun, onTune }: {
  pack: InsightPack;
  health?: InsightPackHealth;
  runs: InsightRun[];
  loading: boolean;
  onClose: () => void;
  onOpenRun: (r: InsightRun) => void;
  onTune: () => void;
}) {
  const [a, b] = runs; // newest, previous
  return (
    <Modal title={`${pack.icon || "🧠"} ${pack.name}`} subtitle="Trends & history" onClose={onClose} wide>
      <div className="space-y-4">
        {health && health.runs_total > 0 ? (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Runs" value={String(health.runs_total)} />
              <Stat label="Notified" value={`${health.notified} · ${Math.round(health.noise_score * 100)}%`} />
              <Stat label="Material" value={String(health.material)} />
              <Stat label="False positive" value={`${health.false_positive} · ${Math.round(health.fp_rate * 100)}%`} />
            </div>
            <div className="rounded-xl border border-gray-200 bg-white p-3">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Verdict timeline (oldest → newest)</div>
              <Sparkline values={health.spark} className="h-6" />
            </div>
            {health.suggest_raise_threshold && (
              <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                🔊 Noisy pack — {health.notified} notifications, {health.false_positive} flagged false-positive.
                <button onClick={onTune} className="ml-auto shrink-0 rounded-lg border border-amber-300 bg-white px-2.5 py-1 font-medium hover:bg-amber-100">Raise threshold</button>
              </div>
            )}
            {a && b && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <CompareCard title="Latest" run={a} />
                <CompareCard title="Previous" run={b} />
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-gray-500">No runs recorded for this pack yet.</p>
        )}
        <div>
          <div className="mb-2 text-sm font-semibold text-gray-700">Recent runs</div>
          {loading ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : runs.length === 0 ? (
            <p className="text-sm text-gray-500">No runs yet.</p>
          ) : (
            <div className="space-y-1.5">
              {runs.slice(0, 30).map((r) => (
                <button key={r.id} onClick={() => onOpenRun(r)} className="flex w-full items-center gap-3 rounded-xl border border-gray-200 bg-white px-3 py-2 text-left hover:bg-gray-50">
                  <VerdictBadge verdict={r.verdict} />
                  <span className="min-w-0 flex-1 truncate text-xs text-gray-600">{r.headline}</span>
                  {r.notified && <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[10px] text-brand">Notified</span>}
                  <span className="shrink-0 text-[11px] text-gray-400">{timeAgo(r.created_at)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

function CoverageMatrix({ data, loading, onOpenWatcher }: {
  data?: InsightCoverage;
  loading: boolean;
  onOpenWatcher: (w: InsightWatcher) => void;
}) {
  const cats = data?.categories ?? [];
  const watchers = data?.watchers ?? [];
  const RANK: Record<string, number> = { covered: 3, stale: 2, paused: 1, gap: 0 };
  const rows = useMemo(() => {
    const byScope = new Map<string, InsightWatcher[]>();
    for (const w of watchers) {
      const arr = byScope.get(w.scope_label) ?? [];
      arr.push(w); byScope.set(w.scope_label, arr);
    }
    return [...byScope.entries()].sort((x, y) => x[0].localeCompare(y[0]));
  }, [watchers]);
  if (loading) return <p className="text-sm text-gray-500">Loading coverage…</p>;
  if (watchers.length === 0) return <p className="text-sm text-gray-500">No scheduled packs to map yet. Schedule a pack to fill the matrix.</p>;
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white">
        <table className="min-w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
              <th className="px-3 py-2 font-medium">Scope</th>
              {cats.map((c) => <th key={c.id} className="px-3 py-2 text-center font-medium" title={c.label}>{c.icon}</th>)}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map(([scope, ws]) => (
              <tr key={scope}>
                <td className="whitespace-nowrap px-3 py-2 font-medium text-gray-800">{scope}</td>
                {cats.map((c) => {
                  const inCat = ws.filter((w) => w.category === c.id);
                  if (inCat.length === 0) return <td key={c.id} className="px-3 py-2 text-center text-gray-200">·</td>;
                  const best = inCat.reduce((x, y) => (RANK[y.status] > RANK[x.status] ? y : x));
                  const m = COVERAGE_STATUS[best.status] ?? COVERAGE_STATUS.gap;
                  return (
                    <td key={c.id} className="px-3 py-2 text-center">
                      <button onClick={() => onOpenWatcher(best)} title={`${best.pack_name} · ${m.label}`}
                        className={`inline-flex h-6 w-6 items-center justify-center rounded-full ${m.dot} text-[10px] text-white`}>
                        {inCat.length > 1 ? inCat.length : ""}
                        <span className="sr-only">{m.label}</span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-3 text-[11px] text-gray-500">
        {(["covered", "stale", "paused"] as const).map((s) => (
          <span key={s} className="inline-flex items-center gap-1"><span className={`h-2.5 w-2.5 rounded-full ${COVERAGE_STATUS[s].dot}`} />{COVERAGE_STATUS[s].label}</span>
        ))}
        <span className="inline-flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-gray-200" />No watcher</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- main panel
const EMPTY_PACK: InsightPack = {
  id: "", name: "", icon: "🧠", category: "general", description: "",
  sources: ["change_explorer"], supported_scopes: ["workload", "subscription", "tenant"],
  lookback_hours: 24, filters: { min_risk: "low" },
  materiality: { notify_threshold: "notable", always_notify_if: [] },
  output: { format: ["bullets", "table"] }, instructions: "", enabled: true, builtin: false,
};

export function InsightPacksPanel() {
  const qc = useQueryClient();
  const { section } = useParams<{ section?: string }>();
  const [tab, setTab] = useState<"today" | "library" | "runs" | "schedule">("today");
  // Deep-links (e.g. from insight notifications) can target a tab via /insights/:section.
  useEffect(() => {
    if (section === "runs" || section === "schedule" || section === "library" || section === "today") setTab(section);
  }, [section]);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [editing, setEditing] = useState<InsightPack | null>(null);
  const [running, setRunning] = useState<InsightPack | null>(null);
  const [viewRun, setViewRun] = useState<InsightRun | null>(null);
  // Recent-runs inbox filters + expansion state.
  const [verdictFilter, setVerdictFilter] = useState<InsightVerdict[]>([]);
  const [runPackFilter, setRunPackFilter] = useState("");
  const [notifiedOnly, setNotifiedOnly] = useState(false);
  const [runSearch, setRunSearch] = useState("");
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Library (P6) + trends (P4) view state.
  const [librarySearch, setLibrarySearch] = useState("");
  const [libCategory, setLibCategory] = useState("");
  const [libSource, setLibSource] = useState("");
  const [libSort, setLibSort] = useState<"active" | "noisy" | "runs" | "name">("active");
  const [libStatus, setLibStatus] = useState<"all" | "enabled" | "disabled">("all");
  const [libGroupBy, setLibGroupBy] = useState<"category" | "source" | "status" | "collection" | "none">("category");
  const [libView, setLibView] = useState<"grid" | "list">("grid");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [managingCollections, setManagingCollections] = useState(false);
  const [bulkMode, setBulkMode] = useState(false);
  const [selectedPacks, setSelectedPacks] = useState<Set<string>>(new Set());
  const [historyPack, setHistoryPack] = useState<InsightPack | null>(null);
  // Schedule (P5) sub-view + digest feedback (P3) state.
  const [schedView, setSchedView] = useState<"timeline" | "coverage">("timeline");
  const [caseCreated, setCaseCreated] = useState<{ id: string; title: string } | null>(null);
  const [pdfBusy, setPdfBusy] = useState(false);
  // Transient status toast (e.g. "Run now" feedback). Auto-clears after a few seconds.
  const [toast, setToast] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t); }, [toast]);
  const navigate = useNavigate();
  const location = useLocation();

  // A workload's "＋ Add a watcher" entry point lands here with the workload in router
  // state: pre-scope the schedule dialog to it, optionally pre-filter to the gap's category.
  const [preAnchor, setPreAnchor] = useState<{ id: string; name?: string } | null>(null);
  useEffect(() => {
    const st = location.state as { anchorWorkloadId?: string; anchorWorkloadName?: string; category?: string } | null;
    if (!st?.anchorWorkloadId) return;
    setPreAnchor({ id: st.anchorWorkloadId, name: st.anchorWorkloadName });
    if (st.category) setLibCategory(st.category);
    // Consume the state so a refresh / back-nav doesn't re-trigger the banner.
    navigate(location.pathname, { replace: true, state: null });
  }, [location.state, location.pathname, navigate]);

  const lib = useQuery({ queryKey: ["insightPacks"], queryFn: () => api.insightPacks() });
  const templates = useQuery({ queryKey: ["insightTemplates"], queryFn: () => api.insightTemplates() });
  const latest = useQuery({ queryKey: ["insightLatest"], queryFn: () => api.insightLatest() });
  const runs = useQuery({
    queryKey: ["insightRuns"],
    queryFn: () => api.insightRuns(undefined, 300),
    enabled: tab === "runs" || tab === "today",
  });
  const upcoming = useQuery({ queryKey: ["insightUpcoming"], queryFn: () => api.insightUpcoming(7), enabled: tab === "schedule" });
  const health = useQuery({ queryKey: ["insightHealth"], queryFn: () => api.insightHealth() });
  const coverage = useQuery({ queryKey: ["insightCoverageAll"], queryFn: () => api.insightCoverage(), enabled: tab === "schedule" });
  const packHistory = useQuery({
    queryKey: ["insightRuns", "pack", historyPack?.id],
    queryFn: () => api.insightRuns(historyPack!.id, 100),
    enabled: !!historyPack,
  });

  const latestByPack = useMemo(() => {
    const m: Record<string, InsightRun> = {};
    for (const r of latest.data?.latest ?? []) m[r.pack_id] = r;
    return m;
  }, [latest.data]);
  const healthByPack = useMemo<Record<string, InsightPackHealth>>(() => health.data?.health ?? {}, [health.data]);

  const toggle = useMutation({
    mutationFn: (p: InsightPack) => api.setInsightPackEnabled(p.id, !p.enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const clone = useMutation({
    mutationFn: (id: string) => api.cloneInsightPack(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteInsightPack(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });

  // ---- Runs inbox + Today derivations (runs come newest-first from the API) ----
  const allRuns = runs.data?.runs ?? [];
  const unreadCount = useMemo(() => allRuns.filter(isUnread).length, [allRuns]);

  // Today = the latest run per (pack, scope), triaged urgent → notable → quiet.
  const todayItems = useMemo(() => {
    const seen = new Set<string>();
    const out: InsightRun[] = [];
    for (const r of allRuns) {
      const k = `${r.pack_id}|${scopeKey(r.scope)}`;
      if (seen.has(k)) continue;
      seen.add(k); out.push(r);
    }
    return out.sort((a, b) =>
      (VERDICT_RANK[b.verdict] - VERDICT_RANK[a.verdict]) ||
      (b.created_at ?? "").localeCompare(a.created_at ?? ""));
  }, [allRuns]);
  const todayCounts = useMemo(() => ({
    urgent: todayItems.filter((r) => r.verdict === "urgent").length,
    notable: todayItems.filter((r) => r.verdict === "notable").length,
    quiet: todayItems.filter((r) => r.verdict === "nothing_notable").length,
  }), [todayItems]);

  // Recent-runs inbox: filter, then group by day and collapse consecutive same-scope runs.
  const filteredRuns = useMemo(() => {
    const q = runSearch.trim().toLowerCase();
    return allRuns.filter((r) => {
      if (verdictFilter.length && !verdictFilter.includes(r.verdict)) return false;
      if (runPackFilter && r.pack_id !== runPackFilter) return false;
      if (notifiedOnly && !r.notified) return false;
      if (q && !`${r.pack_name} ${r.headline} ${r.scope_label}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [allRuns, verdictFilter, runPackFilter, notifiedOnly, runSearch]);

  const runSections = useMemo(() => {
    const sections: { day: string; groups: { key: string; runs: InsightRun[] }[] }[] = [];
    let curDay: { day: string; groups: { key: string; runs: InsightRun[] }[] } | null = null;
    let curGroup: { key: string; runs: InsightRun[] } | null = null;
    for (const r of filteredRuns) {
      const day = dayLabel(r.created_at);
      if (!curDay || curDay.day !== day) { curDay = { day, groups: [] }; sections.push(curDay); curGroup = null; }
      const gk = `${r.pack_id}|${scopeKey(r.scope)}`;
      if (!curGroup || curGroup.key !== gk) { curGroup = { key: gk, runs: [] }; curDay.groups.push(curGroup); }
      curGroup.runs.push(r);
    }
    return sections;
  }, [filteredRuns]);

  const navList = tab === "today" ? todayItems : filteredRuns;

  const runState = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { read?: boolean; acknowledged?: boolean; false_positive?: boolean } }) =>
      api.setInsightRunState(id, body),
    onSuccess: ({ run }) => {
      setViewRun((cur) => (cur && cur.id === run.id ? run : cur));
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
      qc.invalidateQueries({ queryKey: ["insightLatest"] });
    },
  });
  const markAllRead = useMutation({
    mutationFn: () => api.markAllInsightRunsRead(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
      qc.invalidateQueries({ queryKey: ["insightLatest"] });
    },
  });
  const rerun = useRunJob((run) => {
    setViewRun(run);
    qc.invalidateQueries({ queryKey: ["insightRuns"] });
    qc.invalidateQueries({ queryKey: ["insightLatest"] });
  });
  const snooze = useMutation({
    mutationFn: ({ id, days }: { id: string; days: number }) => api.snoozeInsightPack(id, days),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const pin = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => api.pinInsightPack(id, pinned),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const setPackCollections = useMutation({
    mutationFn: ({ id, ids }: { id: string; ids: string[] }) => api.setInsightPackCollections(id, ids),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const createCollection = useMutation({
    mutationFn: ({ name }: { name: string; packId?: string }) => api.createInsightCollection(name),
    onSuccess: async ({ collection }, vars) => {
      await qc.invalidateQueries({ queryKey: ["insightPacks"] });
      // If created from a pack's picker, immediately assign that pack to the new collection.
      if (vars.packId) {
        const p = (lib.data?.packs ?? []).find((x) => x.id === vars.packId);
        const ids = [...(p?.collection_ids ?? []), collection.id];
        setPackCollections.mutate({ id: vars.packId, ids });
      }
    },
  });
  const renameCollection = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.updateInsightCollection(id, { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const deleteCollection = useMutation({
    mutationFn: (id: string) => api.deleteInsightCollection(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insightPacks"] }),
  });
  const bulkEnable = useMutation({
    mutationFn: ({ ids, enabled }: { ids: string[]; enabled: boolean }) =>
      Promise.all(ids.map((id) => api.setInsightPackEnabled(id, enabled))),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insightPacks"] });
      setSelectedPacks(new Set());
      setBulkMode(false);
    },
  });
  const taskRunNow = useMutation({
    mutationFn: (taskId: string) => api.runTaskNow(taskId),
    onSuccess: (res) => {
      setToast({ kind: "ok", msg: res?.message || "Run started — it continues on the server. Check Recent runs for the result." });
      qc.invalidateQueries({ queryKey: ["insightUpcoming"] });
      qc.invalidateQueries({ queryKey: ["insightCoverageAll"] });
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
    },
    onError: (e) => setToast({ kind: "err", msg: formatError(e) }),
  });
  const taskToggle = useMutation({
    mutationFn: (taskId: string) => api.toggleTask(taskId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insightUpcoming"] });
      qc.invalidateQueries({ queryKey: ["insightCoverageAll"] });
    },
  });
  const createCase = useMutation({
    mutationFn: (r: InsightRun) => api.createCase({
      title: `${r.pack_name}: ${r.headline}`.slice(0, 180),
      summary: r.bullets.join("\n"),
      severity: r.verdict === "urgent" ? "high" : r.verdict === "notable" ? "medium" : "low",
      workload_id: r.scope.workload_ids?.[0] ?? r.scope.workload_id ?? null,
      workload_name: r.scope.workload_names?.[0] ?? null,
      connection_id: r.scope.connection_id ?? null,
    }),
    onSuccess: (c) => setCaseCreated({ id: c.id, title: c.title }),
  });

  async function exportRunPdf(r: InsightRun) {
    setPdfBusy(true);
    try {
      const blob = await api.insightRunPdf(r.id);
      downloadBlob(blob, `insight-${(r.pack_name || "pack").replace(/\s+/g, "-").toLowerCase()}.pdf`);
    } catch (e) {
      alert(formatError(e));
    } finally {
      setPdfBusy(false);
    }
  }
  async function openRunById(id: string) {
    try { const { run } = await api.insightRun(id); openRun(run); } catch { /* ignore */ }
  }

  function openRun(r: InsightRun) {
    setCaseCreated(null);
    setViewRun(r);
    if (isUnread(r)) runState.mutate({ id: r.id, body: { read: true } });
  }
  function toggleGroup(key: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  const library = lib.data;
  const packs = library?.packs ?? [];
  const existingIds = new Set(packs.map((p) => p.id));
  const availableTemplates = (templates.data?.templates ?? []).filter((t) => !existingIds.has(t.id));

  // Library filter + sort (P6). "Recently active" / "noisiest" lean on latest runs + health.
  const catOptions = library?.categories ?? [];
  const sourceOptions = library?.sources ?? [];
  const libPacks = useMemo(() => {
    const q = librarySearch.trim().toLowerCase();
    const list = packs.filter((p) => {
      if (q && !`${p.name} ${p.description} ${p.category} ${p.sources.join(" ")}`.toLowerCase().includes(q)) return false;
      if (libCategory && p.category !== libCategory) return false;
      if (libSource && !p.sources.includes(libSource)) return false;
      if (libStatus === "enabled" && !p.enabled) return false;
      if (libStatus === "disabled" && p.enabled) return false;
      return true;
    });
    const lastActive = (p: InsightPack) => latestByPack[p.id]?.created_at ?? "";
    const noise = (p: InsightPack) => healthByPack[p.id]?.noise_score ?? -1;
    const totalRuns = (p: InsightPack) => healthByPack[p.id]?.runs_total ?? 0;
    return [...list].sort((a, b) => {
      if (libSort === "name") return a.name.localeCompare(b.name);
      if (libSort === "noisy") return noise(b) - noise(a) || a.name.localeCompare(b.name);
      if (libSort === "runs") return totalRuns(b) - totalRuns(a) || a.name.localeCompare(b.name);
      return lastActive(b).localeCompare(lastActive(a)) || a.name.localeCompare(b.name);
    });
  }, [packs, librarySearch, libCategory, libSource, libStatus, libSort, latestByPack, healthByPack]);
  function toggleSelect(id: string) {
    setSelectedPacks((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }

  // Organization (P7): pinned section + collapsible group-by sections.
  const collections = library?.collections ?? [];
  const collectionById = useMemo(() => {
    const m: Record<string, InsightCollection> = {};
    for (const c of collections) m[c.id] = c;
    return m;
  }, [collections]);
  const pinnedPacks = useMemo(() => libPacks.filter((p) => p.pinned), [libPacks]);
  const collectionCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of packs) for (const id of p.collection_ids ?? []) c[id] = (c[id] ?? 0) + 1;
    return c;
  }, [packs]);
  const libGroups = useMemo(() => {
    // Pinned packs surface in their own top section; keep them out of the groups
    // so they aren't listed twice.
    const rest = libPacks.filter((p) => !p.pinned);
    type G = { key: string; label: string; icon: string; packs: InsightPack[] };
    if (libGroupBy === "none") {
      return rest.length ? [{ key: "all", label: "All packs", icon: "🧠", packs: rest }] : [];
    }
    const map = new Map<string, G>();
    const ensure = (key: string, label: string, icon: string) => {
      let g = map.get(key);
      if (!g) { g = { key, label, icon, packs: [] }; map.set(key, g); }
      return g;
    };
    if (libGroupBy === "category") {
      const meta: Record<string, { label: string; icon: string }> = {};
      for (const c of catOptions) meta[c.id] = { label: c.label, icon: c.icon };
      for (const p of rest) {
        const m = meta[p.category];
        ensure(p.category, m?.label ?? p.category, m?.icon ?? "🏷️").packs.push(p);
      }
      const order = catOptions.map((c) => c.id);
      return [...map.values()].sort((a, b) => {
        const ai = order.indexOf(a.key), bi = order.indexOf(b.key);
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi) || a.label.localeCompare(b.label);
      });
    }
    if (libGroupBy === "source") {
      const meta: Record<string, { label: string; icon: string }> = {};
      for (const s of sourceOptions) meta[s.id] = { label: s.label, icon: s.icon };
      for (const p of rest) {
        const key = p.sources[0] ?? "other";
        const m = meta[key];
        ensure(key, m?.label ?? key, m?.icon ?? "🔗").packs.push(p);
      }
      const order = sourceOptions.map((s) => s.id);
      return [...map.values()].sort((a, b) => {
        const ai = order.indexOf(a.key), bi = order.indexOf(b.key);
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi) || a.label.localeCompare(b.label);
      });
    }
    if (libGroupBy === "status") {
      const bucket = (p: InsightPack) => (!p.enabled ? "disabled" : snoozeInfo(p).active ? "snoozed" : "active");
      const meta: Record<string, { label: string; icon: string; order: number }> = {
        active: { label: "Active", icon: "✅", order: 0 },
        snoozed: { label: "Snoozed", icon: "😴", order: 1 },
        disabled: { label: "Disabled", icon: "⏸️", order: 2 },
      };
      for (const p of rest) { const k = bucket(p); ensure(k, meta[k].label, meta[k].icon).packs.push(p); }
      return [...map.values()].sort((a, b) => meta[a.key].order - meta[b.key].order);
    }
    // collection
    for (const c of collections) ensure(c.id, c.name, c.icon || "📁");
    for (const p of rest) {
      const ids = (p.collection_ids ?? []).filter((id) => collectionById[id]);
      if (!ids.length) { ensure("__none", "Uncategorized", "📭").packs.push(p); continue; }
      for (const id of ids) ensure(id, collectionById[id].name, collectionById[id].icon || "📁").packs.push(p);
    }
    const groups = [...map.values()].filter((g) => g.key === "__none" || g.packs.length > 0 || collectionById[g.key]);
    return groups.sort((a, b) => {
      if (a.key === "__none") return 1;
      if (b.key === "__none") return -1;
      return a.label.localeCompare(b.label);
    });
  }, [libPacks, libGroupBy, catOptions, sourceOptions, collections, collectionById]);
  function toggleGroupCollapse(key: string) {
    setCollapsedGroups((prev) => { const n = new Set(prev); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  }
  function packCardEl(p: InsightPack) {
    return (
      <PackCard key={p.id} pack={p} latest={latestByPack[p.id]} health={healthByPack[p.id]}
        view={libView}
        onRun={() => setRunning(p)}
        onEdit={() => setEditing(p)}
        onClone={() => clone.mutate(p.id)}
        onDelete={() => { if (confirm(`Delete "${p.name}"?`)) del.mutate(p.id); }}
        onToggle={() => toggle.mutate(p)}
        onHistory={() => setHistoryPack(p)}
        pinned={!!p.pinned}
        onTogglePin={() => pin.mutate({ id: p.id, pinned: !p.pinned })}
        collections={collections}
        onSetCollections={(ids) => setPackCollections.mutate({ id: p.id, ids })}
        onCreateCollection={(name) => createCollection.mutate({ name, packId: p.id })}
        collectionBusy={setPackCollections.isPending || createCollection.isPending}
        selectable={bulkMode}
        selected={selectedPacks.has(p.id)}
        onSelectChange={() => toggleSelect(p.id)} />
    );
  }
  function renderPacks(list: InsightPack[]) {
    if (libView === "list") return <div className="space-y-2">{list.map(packCardEl)}</div>;
    return <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">{list.map(packCardEl)}</div>;
  }

  // Upcoming occurrences grouped by day for the schedule timeline (P5).
  const upcomingSections = useMemo(() => {
    const out: { day: string; items: InsightOccurrence[] }[] = [];
    let cur: { day: string; items: InsightOccurrence[] } | null = null;
    for (const o of upcoming.data?.occurrences ?? []) {
      const day = dayLabel(o.at);
      if (!cur || cur.day !== day) { cur = { day, items: [] }; out.push(cur); }
      cur.items.push(o);
    }
    return out;
  }, [upcoming.data]);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      {/* transient status toast (Run now feedback, etc.) */}
      {toast && (
        <div className={`pointer-events-none fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-lg px-4 py-2 text-sm font-medium shadow-lg ${toast.kind === "ok" ? "bg-gray-900 text-white" : "bg-red-600 text-white"}`}>
          {toast.msg}
        </div>
      )}
      {/* header */}
      <div className="border-b border-gray-200 bg-white px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              <span>🧠</span> AI Insight Packs
            </h1>
            <p className="mt-0.5 text-sm text-gray-500">
              Scheduled AI packs that gather change data, reason over it, and ping you only when something material happens.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setEditing({ ...EMPTY_PACK })} className="rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-700 hover:bg-gray-50">New pack</button>
            <button onClick={() => setWizardOpen(true)} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90">Generate with AI</button>
          </div>
        </div>
        <div className="mt-3 flex gap-1">
          {(["today", "library", "runs", "schedule"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`relative rounded-lg px-3 py-1.5 text-sm capitalize transition ${tab === t ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>
              {t === "today" ? "Today" : t === "runs" ? "Recent runs" : t === "schedule" ? "Schedule" : "Library"}
              {t === "runs" && unreadCount > 0 && (
                <span className={`ml-1.5 inline-flex min-w-[18px] justify-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${tab === t ? "bg-white/20 text-white" : "bg-brand text-white"}`}>
                  {unreadCount}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        {tab === "today" && (
          <div className="space-y-4">
            {runs.isLoading ? (
              <p className="text-sm text-gray-500">Loading today’s intelligence…</p>
            ) : todayItems.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-gray-300 bg-white p-8 text-center">
                <div className="text-2xl">🧠</div>
                <p className="mt-2 text-sm font-medium text-gray-700">No insights yet</p>
                <p className="mt-1 text-xs text-gray-500">Run or schedule a pack — the latest digest per scope lands here.</p>
                <button onClick={() => setTab("library")} className="mt-3 rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90">Browse packs</button>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-red-50 px-3 py-1 text-xs font-medium text-red-700"><span className="h-1.5 w-1.5 rounded-full bg-red-500" />{todayCounts.urgent} urgent</span>
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800"><span className="h-1.5 w-1.5 rounded-full bg-amber-500" />{todayCounts.notable} notable</span>
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-600"><span className="h-1.5 w-1.5 rounded-full bg-gray-400" />{todayCounts.quiet} quiet</span>
                </div>
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  {todayItems.map((r) => (
                    <div key={r.id} className={`flex flex-col rounded-2xl border bg-white p-4 shadow-sm transition hover:shadow-md ${r.verdict === "urgent" ? "border-red-200" : r.verdict === "notable" ? "border-amber-200" : "border-gray-200"}`}>
                      <div className="flex items-start gap-3">
                        <div className="text-2xl leading-none">{r.pack_icon || "🧠"}</div>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-sm font-semibold text-gray-900">{r.pack_name}</span>
                            <VerdictBadge verdict={r.verdict} />
                            {isUnread(r) && <span className="h-2 w-2 rounded-full bg-brand" title="Unread" />}
                          </div>
                          <div className="mt-0.5 truncate text-[11px] text-gray-500">{r.scope_label} · {timeAgo(r.created_at)}</div>
                        </div>
                      </div>
                      <p className="mt-2 line-clamp-2 text-sm text-gray-700">{r.headline}</p>
                      <div className="mt-3 flex items-center gap-1.5 border-t border-gray-100 pt-3">
                        <button onClick={() => openRun(r)} className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800">Open</button>
                        {r.acknowledged_at ? (
                          <span className="inline-flex items-center gap-1 rounded-lg bg-green-50 px-2.5 py-1.5 text-xs text-green-700">✓ Acknowledged</span>
                        ) : (
                          <button onClick={() => runState.mutate({ id: r.id, body: { acknowledged: true } })} disabled={runState.isPending} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50">Acknowledge</button>
                        )}
                        {r.false_positive && <span className="ml-auto rounded-lg bg-gray-100 px-2 py-1 text-[10px] text-gray-500">False positive</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {tab === "library" && (
          <>
            {preAnchor && (
              <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-brand/30 bg-brand/[0.04] px-3 py-2 text-sm">
                <span className="text-gray-700">
                  Adding a watcher for <b>{preAnchor.name ?? "the selected workload"}</b> — pick a pack and schedule it; the run/schedule dialog is pre-scoped to this workload.
                </span>
                <button onClick={() => { setPreAnchor(null); setLibCategory(""); }} className="ml-auto text-xs text-gray-500 hover:text-gray-700">Clear</button>
              </div>
            )}
            {lib.isLoading ? (
              <p className="text-sm text-gray-500">Loading packs…</p>
            ) : packs.length === 0 ? (
              <p className="text-sm text-gray-500">No packs yet. Generate one with AI or start from a template below.</p>
            ) : (
              <>
                <div className="mb-4 flex flex-wrap items-center gap-2">
                  <input value={librarySearch} onChange={(e) => setLibrarySearch(e.target.value)} placeholder="Search packs…"
                    className="min-w-[12rem] flex-1 rounded-lg border border-gray-300 px-3 py-1.5 text-sm focus:border-brand focus:outline-none" />
                  <select value={libCategory} onChange={(e) => setLibCategory(e.target.value)} className="rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs text-gray-700 focus:border-brand focus:outline-none">
                    <option value="">All categories</option>
                    {catOptions.map((c) => <option key={c.id} value={c.id}>{c.icon} {c.label}</option>)}
                  </select>
                  <select value={libSource} onChange={(e) => setLibSource(e.target.value)} className="rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs text-gray-700 focus:border-brand focus:outline-none">
                    <option value="">All sources</option>
                    {sourceOptions.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                  </select>
                  <select value={libSort} onChange={(e) => setLibSort(e.target.value as typeof libSort)} className="rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs text-gray-700 focus:border-brand focus:outline-none">
                    <option value="active">Recently active</option>
                    <option value="noisy">Noisiest</option>
                    <option value="runs">Most runs</option>
                    <option value="name">Name</option>
                  </select>
                  <div className="inline-flex overflow-hidden rounded-lg border border-gray-300 text-xs">
                    {(["all", "enabled", "disabled"] as const).map((s) => (
                      <button key={s} onClick={() => setLibStatus(s)} className={`px-2.5 py-1.5 capitalize transition ${libStatus === s ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>{s}</button>
                    ))}
                  </div>
                  <div className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2 py-1 text-xs text-gray-600">
                    <span className="text-gray-400">Group</span>
                    <select value={libGroupBy} onChange={(e) => setLibGroupBy(e.target.value as typeof libGroupBy)} className="bg-transparent text-xs text-gray-700 focus:outline-none">
                      <option value="category">Category</option>
                      <option value="source">Source</option>
                      <option value="status">Status</option>
                      <option value="collection">Collection</option>
                      <option value="none">None</option>
                    </select>
                  </div>
                  <div className="inline-flex overflow-hidden rounded-lg border border-gray-300 text-xs">
                    <button onClick={() => setLibView("grid")} title="Grid view" className={`px-2.5 py-1.5 transition ${libView === "grid" ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>▦</button>
                    <button onClick={() => setLibView("list")} title="Compact list" className={`px-2.5 py-1.5 transition ${libView === "list" ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>☰</button>
                  </div>
                  <button onClick={() => setManagingCollections((v) => !v)}
                    className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${managingCollections ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                    📁 Collections
                  </button>
                  <button onClick={() => { setBulkMode((v) => !v); setSelectedPacks(new Set()); }}
                    className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${bulkMode ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                    {bulkMode ? "Done" : "Select"}
                  </button>
                </div>

                {managingCollections && (
                  <CollectionManager
                    collections={collections}
                    counts={collectionCounts}
                    onCreate={(name) => createCollection.mutate({ name })}
                    onRename={(id, name) => renameCollection.mutate({ id, name })}
                    onDelete={(id) => deleteCollection.mutate(id)}
                    busy={createCollection.isPending || renameCollection.isPending || deleteCollection.isPending}
                  />
                )}

                {bulkMode && selectedPacks.size > 0 && (
                  <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-brand/30 bg-brand/[0.04] px-3 py-2 text-sm">
                    <span className="font-medium text-gray-700">{selectedPacks.size} selected</span>
                    <button onClick={() => bulkEnable.mutate({ ids: [...selectedPacks], enabled: true })} disabled={bulkEnable.isPending} className="rounded-lg bg-brand px-3 py-1 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-50">Enable</button>
                    <button onClick={() => bulkEnable.mutate({ ids: [...selectedPacks], enabled: false })} disabled={bulkEnable.isPending} className="rounded-lg border border-gray-300 px-3 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50">Disable</button>
                    <button onClick={() => setSelectedPacks(new Set())} className="ml-auto text-xs text-gray-500 hover:text-gray-700">Clear</button>
                  </div>
                )}

                {libPacks.length === 0 ? (
                  <p className="text-sm text-gray-500">No packs match these filters.</p>
                ) : (
                  <div className="space-y-6">
                    {pinnedPacks.length > 0 && (
                      <section>
                        <button onClick={() => toggleGroupCollapse("__pinned")} className="mb-2 flex w-full items-center gap-2 text-left">
                          <span className={`text-gray-400 transition ${collapsedGroups.has("__pinned") ? "-rotate-90" : ""}`}>▾</span>
                          <span className="text-sm font-semibold text-gray-800">📌 Pinned</span>
                          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">{pinnedPacks.length}</span>
                        </button>
                        {!collapsedGroups.has("__pinned") && renderPacks(pinnedPacks)}
                      </section>
                    )}
                    {libGroupBy === "none" && pinnedPacks.length === 0 ? (
                      renderPacks(libPacks)
                    ) : (
                      libGroups.map((g) => {
                        const collapsed = collapsedGroups.has(g.key);
                        const editable = libGroupBy === "collection" && g.key !== "__none" && !!collectionById[g.key];
                        return (
                          <section key={g.key}>
                            <div className="mb-2 flex items-center gap-2">
                              <button onClick={() => toggleGroupCollapse(g.key)} className="flex items-center gap-2 text-left">
                                <span className={`text-gray-400 transition ${collapsed ? "-rotate-90" : ""}`}>▾</span>
                                <span className="text-sm font-semibold text-gray-800">{g.icon} {g.label}</span>
                                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">{g.packs.length}</span>
                              </button>
                              {editable && (
                                <div className="flex items-center gap-1">
                                  <button onClick={() => { const n = prompt("Rename collection", g.label); if (n && n.trim()) renameCollection.mutate({ id: g.key, name: n.trim() }); }}
                                    className="rounded px-1.5 py-0.5 text-[11px] text-gray-400 hover:bg-gray-100 hover:text-gray-600">Rename</button>
                                  <button onClick={() => { if (confirm(`Delete collection "${g.label}"? Packs stay, just un-filed.`)) deleteCollection.mutate(g.key); }}
                                    className="rounded px-1.5 py-0.5 text-[11px] text-red-500 hover:bg-red-50">Delete</button>
                                </div>
                              )}
                            </div>
                            {!collapsed && (g.packs.length ? renderPacks(g.packs) : <p className="pl-6 text-xs text-gray-400">No packs in this collection yet.</p>)}
                          </section>
                        );
                      })
                    )}
                  </div>
                )}
              </>
            )}

            {availableTemplates.length > 0 && (
              <div className="mt-8">
                <h2 className="mb-3 text-sm font-semibold text-gray-700">Starter templates</h2>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {availableTemplates.map((t) => (
                    <div key={t.id} className="rounded-2xl border border-dashed border-gray-300 bg-white p-4">
                      <div className="flex items-start gap-3">
                        <div className="text-2xl">{t.icon}</div>
                        <div>
                          <div className="text-sm font-semibold text-gray-900">{t.name}</div>
                          <span className="text-[11px] text-brand">{t.category}</span>
                        </div>
                      </div>
                      <p className="mt-2 line-clamp-2 text-xs text-gray-500">{t.description}</p>
                      <button onClick={() => clone.mutate(t.id)} className="mt-3 rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50">
                        Use template
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {tab === "runs" && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {(["urgent", "notable", "nothing_notable"] as InsightVerdict[]).map((v) => {
                const on = verdictFilter.includes(v);
                return (
                  <button key={v} onClick={() => setVerdictFilter((f) => on ? f.filter((x) => x !== v) : [...f, v])}
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${VERDICT_META[v].dot}`} />{VERDICT_META[v].label}
                  </button>
                );
              })}
              <select value={runPackFilter} onChange={(e) => setRunPackFilter(e.target.value)} className="rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-700 focus:border-brand focus:outline-none">
                <option value="">All packs</option>
                {packs.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <label className="inline-flex items-center gap-1.5 text-xs text-gray-600">
                <input type="checkbox" checked={notifiedOnly} onChange={(e) => setNotifiedOnly(e.target.checked)} /> Notified only
              </label>
              <input value={runSearch} onChange={(e) => setRunSearch(e.target.value)} placeholder="Search headline, pack, scope…" className="min-w-[10rem] flex-1 rounded-lg border border-gray-300 px-2.5 py-1 text-xs focus:border-brand focus:outline-none" />
              {unreadCount > 0 && (
                <button onClick={() => markAllRead.mutate()} disabled={markAllRead.isPending} className="rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  Mark all read
                </button>
              )}
            </div>

            {runs.isLoading ? (
              <p className="text-sm text-gray-500">Loading runs…</p>
            ) : filteredRuns.length === 0 ? (
              <p className="text-sm text-gray-500">{allRuns.length === 0 ? "No runs yet. Run a pack to see digests here." : "No runs match these filters."}</p>
            ) : (
              <div className="space-y-4">
                {runSections.map((section) => (
                  <div key={section.day} className="space-y-1.5">
                    <div className="sticky top-0 z-10 bg-gray-50/90 py-1 text-xs font-semibold uppercase tracking-wide text-gray-400 backdrop-blur">{section.day}</div>
                    {section.groups.map((g) => {
                      const head = g.runs[0];
                      const extra = g.runs.length - 1;
                      const expanded = expandedGroups.has(head.id);
                      return (
                        <div key={head.id}>
                          <button onClick={() => openRun(head)}
                            className={`flex w-full items-center gap-3 rounded-xl border px-4 py-3 text-left transition hover:shadow-sm ${isUnread(head) ? "border-brand/40 bg-brand/[0.03]" : "border-gray-200 bg-white"}`}>
                            <span className={`h-2 w-2 shrink-0 rounded-full ${isUnread(head) ? "bg-brand" : "bg-transparent"}`} />
                            <span className="text-xl">{head.pack_icon}</span>
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2">
                                <span className={`text-sm text-gray-900 ${isUnread(head) ? "font-semibold" : "font-medium"}`}>{head.pack_name}</span>
                                <VerdictBadge verdict={head.verdict} />
                                {head.notified && <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[10px] text-brand">Notified</span>}
                                {head.acknowledged_at && <span className="rounded-full bg-green-50 px-2 py-0.5 text-[10px] text-green-700">Ack</span>}
                                {head.false_positive && <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-500">False positive</span>}
                              </div>
                              <div className="truncate text-xs text-gray-500">{head.headline}</div>
                            </div>
                            <div className="shrink-0 text-right text-xs text-gray-400">
                              <div>{head.scope_label}</div>
                              <div>{timeAgo(head.created_at)}</div>
                            </div>
                          </button>
                          {extra > 0 && (
                            <button onClick={() => toggleGroup(head.id)} className="ml-9 mt-1 text-[11px] text-gray-400 hover:text-gray-600">
                              {expanded ? "Hide earlier runs" : `+${extra} earlier run${extra > 1 ? "s" : ""} for this scope`}
                            </button>
                          )}
                          {expanded && g.runs.slice(1).map((r) => (
                            <button key={r.id} onClick={() => openRun(r)} className="ml-9 mt-1 flex w-[calc(100%-2.25rem)] items-center gap-3 rounded-lg border border-gray-100 bg-white px-3 py-2 text-left hover:bg-gray-50">
                              <VerdictBadge verdict={r.verdict} />
                              <span className="min-w-0 flex-1 truncate text-xs text-gray-500">{r.headline}</span>
                              <span className="shrink-0 text-[11px] text-gray-400">{timeAgo(r.created_at)}</span>
                            </button>
                          ))}
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === "schedule" && (
          <div className="space-y-4">
            <div className="inline-flex overflow-hidden rounded-lg border border-gray-300 text-sm">
              {(["timeline", "coverage"] as const).map((v) => (
                <button key={v} onClick={() => setSchedView(v)} className={`px-3 py-1.5 transition ${schedView === v ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>
                  {v === "timeline" ? "Timeline" : "Coverage matrix"}
                </button>
              ))}
            </div>

            {schedView === "timeline" ? (
              <div className="space-y-6">
                <div>
                  <h2 className="mb-2 text-sm font-semibold text-gray-700">Active watchers</h2>
                  {coverage.isLoading ? (
                    <p className="text-sm text-gray-500">Loading watchers…</p>
                  ) : (coverage.data?.watchers ?? []).length === 0 ? (
                    <p className="text-sm text-gray-500">No scheduled packs yet. Open a pack and create a schedule.</p>
                  ) : (
                    <div className="space-y-1.5">
                      {(coverage.data?.watchers ?? []).map((w) => (
                        <div key={w.task_id} className="flex items-center gap-3 rounded-xl border border-gray-200 bg-white px-4 py-2.5">
                          <span className="text-xl">{w.pack_icon}</span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="truncate text-sm font-medium text-gray-900">{w.pack_name}</span>
                              <CoverageDot status={w.status} />
                              {w.last_verdict && <VerdictBadge verdict={w.last_verdict} />}
                            </div>
                            <div className="truncate text-xs text-gray-500">{w.schedule_label} · {w.scope_label}{w.enabled && w.next_run_at ? ` · next run ${timeUntil(w.next_run_at)}` : ""}</div>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <button onClick={() => taskRunNow.mutate(w.task_id)} disabled={taskRunNow.isPending && taskRunNow.variables === w.task_id} className="rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">{taskRunNow.isPending && taskRunNow.variables === w.task_id ? "Starting…" : "Run now"}</button>
                            <button onClick={() => taskToggle.mutate(w.task_id)} disabled={taskToggle.isPending} className="rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">{w.enabled ? "Pause" : "Resume"}</button>
                            <button onClick={() => navigate("/automations/tasks")} className="rounded-lg px-2.5 py-1 text-xs text-gray-500 hover:bg-gray-100">Edit</button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div>
                  <h2 className="mb-2 text-sm font-semibold text-gray-700">Next 7 days</h2>
                  {upcoming.isLoading ? (
                    <p className="text-sm text-gray-500">Loading schedule…</p>
                  ) : upcomingSections.length === 0 ? (
                    <p className="text-sm text-gray-500">No runs scheduled in the next 7 days.</p>
                  ) : (
                    <div className="space-y-3">
                      {upcomingSections.map((sec) => (
                        <div key={sec.day} className="space-y-1.5">
                          <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">{sec.day}</div>
                          {sec.items.map((o, i) => (
                            <div key={`${o.task_id}-${i}`} className="flex items-center gap-3 rounded-xl border border-gray-200 bg-white px-4 py-2.5">
                              <span className="w-12 shrink-0 text-xs font-medium text-gray-500">{new Date(o.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                              <span className="text-lg">{o.pack_icon}</span>
                              <div className="min-w-0 flex-1">
                                <div className="truncate text-sm font-medium text-gray-900">{o.pack_name}</div>
                                <div className="truncate text-xs text-gray-500">{o.schedule_label}{o.scope_label ? ` · ${o.scope_label}` : ""}</div>
                              </div>
                              <button onClick={() => taskRunNow.mutate(o.task_id)} disabled={taskRunNow.isPending && taskRunNow.variables === o.task_id} className="shrink-0 rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">{taskRunNow.isPending && taskRunNow.variables === o.task_id ? "Starting…" : "Run now"}</button>
                            </div>
                          ))}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <CoverageMatrix data={coverage.data} loading={coverage.isLoading} onOpenWatcher={(w) => { if (w.last_run_id) openRunById(w.last_run_id); }} />
            )}
          </div>
        )}
      </div>

      {wizardOpen && library && (
        <GeneratorWizard
          library={library}
          onClose={() => setWizardOpen(false)}
          onDraft={(draft) => { setWizardOpen(false); setEditing(draft); }}
        />
      )}
      {editing && library && (
        <PackForm initial={editing} library={library} onClose={() => setEditing(null)} onSaved={() => setEditing(null)} />
      )}
      {running && <RunScheduleDialog pack={running} initialWorkloadId={preAnchor?.id} initialWorkloadName={preAnchor?.name} onClose={() => setRunning(null)} />}
      {viewRun && (() => {
        const idx = navList.findIndex((r) => r.id === viewRun.id);
        const go = (d: number) => { const n = navList[idx + d]; if (n) openRun(n); };
        const vp = packs.find((p) => p.id === viewRun.pack_id) ?? null;
        const vh = healthByPack[viewRun.pack_id];
        const sn = snoozeInfo(vp);
        const wid = viewRun.scope.workload_ids?.[0] ?? viewRun.scope.workload_id ?? "";
        return (
          <Modal title="Insight digest" subtitle={`${viewRun.pack_name} · ${viewRun.scope_label}`} onClose={() => { setViewRun(null); setCaseCreated(null); }} wide>
            <div className="space-y-4">
              {vh?.suggest_raise_threshold && (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  <span>🔊 This pack notified {vh.notified}× and {vh.false_positive} were flagged false-positive. Consider raising its notify threshold.</span>
                  {vp && <button onClick={() => { setViewRun(null); setEditing(vp); }} className="ml-auto shrink-0 rounded-lg border border-amber-300 bg-white px-2.5 py-1 font-medium hover:bg-amber-100">Tune pack</button>}
                </div>
              )}
              {sn.active && (
                <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-700">
                  😴 Notifications for this pack are snoozed until {new Date(sn.until!).toLocaleString()}.
                  {vp && <button onClick={() => snooze.mutate({ id: vp.id, days: 0 })} className="ml-2 underline">Unsnooze</button>}
                </div>
              )}
              {caseCreated && (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">
                  <span>✓ Case created: {caseCreated.title}</span>
                  <button onClick={() => navigate(`/cases/${caseCreated.id}`)} className="ml-auto shrink-0 rounded-lg border border-green-300 bg-white px-2.5 py-1 font-medium hover:bg-green-100">Open case</button>
                </div>
              )}

              <DigestView run={viewRun} />
              {rerun.isRunning && (
                <RunProgress job={rerun.job} starting={rerun.starting} />
              )}
              <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-4">
                {viewRun.acknowledged_at ? (
                  <span className="inline-flex items-center gap-1 rounded-lg bg-green-50 px-3 py-1.5 text-xs text-green-700">
                    ✓ Acknowledged{viewRun.acknowledged_by ? ` · ${viewRun.acknowledged_by}` : ""}
                  </span>
                ) : (
                  <button onClick={() => runState.mutate({ id: viewRun.id, body: { acknowledged: true } })} disabled={runState.isPending}
                    className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                    Acknowledge
                  </button>
                )}
                <button onClick={() => runState.mutate({ id: viewRun.id, body: { false_positive: !viewRun.false_positive } })} disabled={runState.isPending}
                  className={`rounded-lg border px-3 py-1.5 text-xs ${viewRun.false_positive ? "border-gray-400 bg-gray-100 text-gray-700" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                  {viewRun.false_positive ? "Unflag false positive" : "Flag false positive"}
                </button>
                <button onClick={() => rerun.start({ pack_id: viewRun.pack_id, scope: viewRun.scope, notify: false })} disabled={rerun.isRunning}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {rerun.isRunning ? "Re-running…" : "Re-run now"}
                </button>
                {idx >= 0 && navList.length > 1 && (
                  <div className="ml-auto flex items-center gap-1">
                    <button onClick={() => go(-1)} disabled={idx <= 0} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-40">← Prev</button>
                    <span className="text-[11px] text-gray-400">{idx + 1} / {navList.length}</span>
                    <button onClick={() => go(1)} disabled={idx >= navList.length - 1} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-40">Next →</button>
                  </div>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <SnoozeMenu disabled={snooze.isPending || !vp} onPick={(days) => vp && snooze.mutate({ id: vp.id, days })} />
                <button onClick={() => createCase.mutate(viewRun)} disabled={createCase.isPending}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {createCase.isPending ? "Creating…" : "Create case"}
                </button>
                <button onClick={() => exportRunPdf(viewRun)} disabled={pdfBusy}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {pdfBusy ? "Exporting…" : "Export PDF"}
                </button>
                {wid && <button onClick={() => navigate(`/change-explorer?workload_id=${encodeURIComponent(wid)}`)} className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">Change Explorer ↗</button>}
                {wid && <button onClick={() => navigate(`/radar?workload_id=${encodeURIComponent(wid)}`)} className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50">Radar ↗</button>}
              </div>
            </div>
          </Modal>
        );
      })()}
      {historyPack && (
        <PackHistoryDrawer
          pack={historyPack}
          health={healthByPack[historyPack.id]}
          runs={packHistory.data?.runs ?? []}
          loading={packHistory.isLoading}
          onClose={() => setHistoryPack(null)}
          onOpenRun={(r) => { setHistoryPack(null); openRun(r); }}
          onTune={() => { setHistoryPack(null); setEditing(historyPack); }}
        />
      )}
    </div>
  );
}
