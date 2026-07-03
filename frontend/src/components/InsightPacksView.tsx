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
import { useEffect, useMemo, useState } from "react";
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
  type InsightScope,
  type InsightVerdict,
  type InsightWatcher,
  type ChangeWorkload,
  type AgentAnswer,
  type AgentWizardQuestion,
} from "../api";

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
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500">{r.time}</td>
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
function Modal({ title, subtitle, onClose, children, wide }: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/30 p-4 sm:p-8" onClick={onClose}>
      <div
        className={`my-4 w-full ${wide ? "max-w-3xl" : "max-w-xl"} rounded-2xl bg-white shadow-xl`}
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
type WizStage = "intent" | "interview" | "generating" | "error";

function GeneratorWizard({ onDraft, onClose }: {
  onDraft: (draft: InsightPack, summary: string) => void;
  onClose: () => void;
}) {
  const [stage, setStage] = useState<WizStage>("intent");
  const [goal, setGoal] = useState("");
  const [step, setStep] = useState(0);
  const [questions, setQuestions] = useState<AgentWizardQuestion[]>([]);
  const [note, setNote] = useState("");
  const [answers, setAnswers] = useState<AgentAnswer[]>([]);
  const [current, setCurrent] = useState<Record<string, string | string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function start() {
    if (!goal.trim()) { setError("Describe what the pack should watch for."); return; }
    setError(""); setBusy(true);
    try {
      const res = await api.insightInterview(goal.trim(), [], 0);
      if (res.done || res.questions.length === 0) { await generate([]); return; }
      setQuestions(res.questions); setNote(res.note); setStep(1);
      setCurrent({}); setCustom({}); setStage("interview");
    } catch (e) { setError(formatError(e)); } finally { setBusy(false); }
  }

  function toggleMulti(qid: string, opt: string) {
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      return { ...c, [qid]: prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt] };
    });
  }

  async function submitStep() {
    const merged: AgentAnswer[] = questions.map((q) => {
      let value: string | string[] = current[q.id] ?? (q.kind === "multi" ? [] : "");
      const extra = (custom[q.id] ?? "").trim();
      if (extra) value = q.kind === "multi" ? [...(Array.isArray(value) ? value : []), extra] : extra;
      return { id: q.id, prompt: q.prompt, answer: value };
    });
    const all = [...answers, ...merged];
    setAnswers(all); setBusy(true); setError("");
    try {
      const res = await api.insightInterview(goal.trim(), all, step);
      if (res.done || res.questions.length === 0) { await generate(all); return; }
      setQuestions(res.questions); setNote(res.note); setStep((s) => s + 1);
      setCurrent({}); setCustom({});
    } catch (e) { setError(formatError(e)); } finally { setBusy(false); }
  }

  async function generate(all: AgentAnswer[]) {
    setStage("generating"); setBusy(true); setError("");
    try {
      const { draft, summary } = await api.insightGenerate(goal.trim(), all);
      onDraft(draft, summary);
    } catch (e) { setError(formatError(e)); setStage("error"); } finally { setBusy(false); }
  }

  const stepLabels = ["Goal", "AI interview", "Generate", "Review & save"];
  const activeStepIdx = stage === "intent" ? 0 : stage === "interview" ? 1 : 2;

  return (
    <Modal title="Generate an insight pack with AI" subtitle="Describe what you want to watch — the AI designs the pack." onClose={onClose} wide>
      <div className="mb-4 flex items-center gap-2 text-xs text-gray-400">
        {stepLabels.map((l, i) => (
          <span key={l} className="flex items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 ${i <= activeStepIdx ? "bg-brand/10 font-medium text-brand" : ""}`}>
              {i + 1}. {l}
            </span>
            {i < stepLabels.length - 1 && <span>→</span>}
          </span>
        ))}
      </div>

      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      {stage === "intent" && (
        <div className="space-y-3">
          <label className="block text-sm font-medium text-gray-700">What should this pack watch for?</label>
          <textarea
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={4}
            placeholder="e.g. Watch for anything that exposes a workload to the public internet, or grants someone privileged access."
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
          />
          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Cancel</button>
            <button onClick={start} disabled={busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
              {busy ? "Thinking…" : "Start"}
            </button>
          </div>
        </div>
      )}

      {stage === "interview" && (
        <div className="space-y-4">
          {note && <p className="text-sm text-gray-500">{note}</p>}
          {questions.map((q) => (
            <div key={q.id} className="space-y-2">
              <label className="block text-sm font-medium text-gray-700">{q.prompt}</label>
              {q.kind === "text" ? (
                <input
                  value={(current[q.id] as string) ?? ""}
                  onChange={(e) => setCurrent((c) => ({ ...c, [q.id]: e.target.value }))}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                />
              ) : (
                <div className="flex flex-wrap gap-2">
                  {q.options.map((opt) => {
                    const selected = q.kind === "multi"
                      ? Array.isArray(current[q.id]) && (current[q.id] as string[]).includes(opt)
                      : current[q.id] === opt;
                    return (
                      <button
                        key={opt}
                        onClick={() => q.kind === "multi" ? toggleMulti(q.id, opt) : setCurrent((c) => ({ ...c, [q.id]: opt }))}
                        className={`rounded-full border px-3 py-1.5 text-sm transition ${selected ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}
                      >
                        {opt}
                      </button>
                    );
                  })}
                </div>
              )}
              {q.allow_custom && q.kind !== "text" && (
                <input
                  value={custom[q.id] ?? ""}
                  onChange={(e) => setCustom((c) => ({ ...c, [q.id]: e.target.value }))}
                  placeholder="Add your own…"
                  className="w-full rounded-lg border border-gray-200 px-3 py-1.5 text-sm focus:border-brand focus:outline-none"
                />
              )}
            </div>
          ))}
          <div className="flex justify-between">
            <button onClick={() => generate(answers)} disabled={busy} className="rounded-lg px-3 py-2 text-sm text-gray-500 hover:bg-gray-100">
              Skip & generate
            </button>
            <button onClick={submitStep} disabled={busy} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
              {busy ? "Thinking…" : "Continue"}
            </button>
          </div>
        </div>
      )}

      {stage === "generating" && (
        <div className="flex flex-col items-center gap-3 py-10 text-gray-500">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          <p className="text-sm">Designing your insight pack…</p>
        </div>
      )}

      {stage === "error" && (
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Close</button>
          <button onClick={() => generate(answers)} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90">Retry</button>
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

function PackForm({ initial, library, onClose, onSaved }: {
  initial: InsightPack;
  library: InsightPackLibrary;
  onClose: () => void;
  onSaved: (p: InsightPack) => void;
}) {
  const [pack, setPack] = useState<InsightPack>(initial);
  const [error, setError] = useState("");
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: () => api.upsertInsightPack(pack),
    onSuccess: ({ pack: saved }) => {
      qc.invalidateQueries({ queryKey: ["insightPacks"] });
      onSaved(saved);
    },
    onError: (e) => setError(formatError(e)),
  });

  function upd<K extends keyof InsightPack>(k: K, v: InsightPack[K]) { setPack((p) => ({ ...p, [k]: v })); }
  function toggleArr(list: string[], v: string): string[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }

  return (
    <Modal title={initial.id ? "Edit pack" : "New insight pack"} subtitle="Scope-agnostic definition. Scope and schedule are chosen when you run or schedule it." onClose={onClose} wide>
      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      <div className="space-y-4">
        <div className="flex gap-3">
          <div className="w-20">
            <label className="block text-xs font-medium text-gray-500">Icon</label>
            <input value={pack.icon} onChange={(e) => upd("icon", e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-center text-lg focus:border-brand focus:outline-none" />
          </div>
          <div className="flex-1">
            <label className="block text-xs font-medium text-gray-500">Name</label>
            <input value={pack.name} onChange={(e) => upd("name", e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none" />
          </div>
          <div className="w-40">
            <label className="block text-xs font-medium text-gray-500">Category</label>
            <select value={pack.category} onChange={(e) => upd("category", e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">Description</label>
          <input value={pack.description} onChange={(e) => upd("description", e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none" />
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">Data sources</label>
          <div className="mt-1 flex flex-wrap gap-2">
            {library.sources.map((s) => {
              const on = pack.sources.includes(s.id);
              return (
                <button key={s.id} title={s.description} onClick={() => upd("sources", toggleArr(pack.sources, s.id))}
                  className={`rounded-full border px-3 py-1.5 text-sm ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                  {s.icon} {s.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-gray-500">Lookback (hours)</label>
            <input type="number" min={1} value={pack.lookback_hours} onChange={(e) => upd("lookback_hours", Number(e.target.value) || 24)} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500">Minimum risk to include</label>
            <select value={pack.filters.min_risk ?? "low"} onChange={(e) => upd("filters", { ...pack.filters, min_risk: e.target.value })} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
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
                <button key={s} onClick={() => upd("supported_scopes", toggleArr(pack.supported_scopes, s))}
                  className={`rounded-full border px-3 py-1.5 text-xs ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                  {s}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">Notify threshold</label>
          <select value={pack.materiality.notify_threshold} onChange={(e) => upd("materiality", { ...pack.materiality, notify_threshold: e.target.value as InsightVerdict })} className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
            {THRESHOLDS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">Always notify if these are detected</label>
          <div className="mt-1 flex flex-wrap gap-2">
            {library.flag_codes.map((f) => {
              const on = pack.materiality.always_notify_if.includes(f.code);
              return (
                <button key={f.code} title={f.code} onClick={() => upd("materiality", { ...pack.materiality, always_notify_if: toggleArr(pack.materiality.always_notify_if, f.code) })}
                  className={`rounded-full border px-2.5 py-1 text-xs ${on ? "border-red-300 bg-red-50 text-red-700" : "border-gray-300 text-gray-600 hover:bg-gray-50"}`}>
                  {f.label}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500">AI instructions</label>
          <p className="mb-1 text-[11px] text-gray-400">Use <code>{"{{scope_label}}"}</code> and <code>{"{{lookback_hours}}"}</code> as placeholders.</p>
          <textarea value={pack.instructions} onChange={(e) => upd("instructions", e.target.value)} rows={8} className="w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-xs focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand" />
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-100 pt-4">
          <button onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100">Cancel</button>
          <button onClick={() => save.mutate()} disabled={save.isPending || !pack.name.trim()} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
            {save.isPending ? "Saving…" : "Save pack"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- run / schedule dialog
type ScopeMode = InsightScope["mode"];

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
  const [scheduleKind, setScheduleKind] = useState<"daily" | "weekly">("daily");
  const [time, setTime] = useState("08:00");
  const [weekday, setWeekday] = useState(1);

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

  const run = useMutation({
    mutationFn: () => {
      const scope = buildScope();
      if (!scope) throw new Error("Pick a workload to anchor the scope.");
      return api.runInsightPack({ pack_id: pack.id, scope, notify });
    },
    onSuccess: ({ run }) => {
      setError(""); setRunResult(run);
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
      qc.invalidateQueries({ queryKey: ["insightLatest"] });
    },
    onError: (e) => setError(formatError(e)),
  });

  const schedule = useMutation({
    mutationFn: () => {
      const scope = buildScope();
      if (!scope) throw new Error("Pick a workload to anchor the scope.");
      return api.upsertTask({
        name: `${pack.name} — ${selectedWl?.name ?? "scope"}`,
        target_type: "insight_pack",
        target_config: { pack_id: pack.id, scope },
        schedule_kind: scheduleKind,
        time_of_day: time,
        weekday: scheduleKind === "weekly" ? weekday : null,
        timezone: "UTC",
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
              <button onClick={() => run.mutate()} disabled={run.isPending} className="rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                {run.isPending ? "Running…" : "Run"}
              </button>
            </div>
          </div>
          {runResult && (
            <div className="mt-4 border-t border-gray-100 pt-4">
              <DigestView run={runResult} />
            </div>
          )}
        </div>

        {/* Schedule */}
        <div className="rounded-xl border border-gray-200 p-4">
          <div className="text-sm font-medium text-gray-800">Schedule</div>
          <div className="mt-2 flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-xs text-gray-500">Cadence</label>
              <select value={scheduleKind} onChange={(e) => setScheduleKind(e.target.value as "daily" | "weekly")} className="mt-1 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
              </select>
            </div>
            {scheduleKind === "weekly" && (
              <div>
                <label className="block text-xs text-gray-500">Day</label>
                <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))} className="mt-1 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none">
                  {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
                </select>
              </div>
            )}
            <div>
              <label className="block text-xs text-gray-500">Time (UTC)</label>
              <input type="time" value={time} onChange={(e) => setTime(e.target.value)} className="mt-1 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none" />
            </div>
            <button onClick={() => schedule.mutate()} disabled={schedule.isPending} className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
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
  const rerun = useMutation({
    mutationFn: (r: InsightRun) => api.runInsightPack({ pack_id: r.pack_id, scope: r.scope, notify: false }),
    onSuccess: ({ run }) => {
      setViewRun(run);
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
      qc.invalidateQueries({ queryKey: ["insightLatest"] });
    },
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insightUpcoming"] });
      qc.invalidateQueries({ queryKey: ["insightCoverageAll"] });
      qc.invalidateQueries({ queryKey: ["insightRuns"] });
    },
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
                            <button onClick={() => taskRunNow.mutate(w.task_id)} disabled={taskRunNow.isPending} className="rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">Run now</button>
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
                              <button onClick={() => taskRunNow.mutate(o.task_id)} disabled={taskRunNow.isPending} className="shrink-0 rounded-lg border border-gray-300 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">Run now</button>
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
                <button onClick={() => rerun.mutate(viewRun)} disabled={rerun.isPending}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">
                  {rerun.isPending ? "Re-running…" : "Re-run now"}
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
