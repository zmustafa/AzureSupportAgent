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
import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type InsightPack,
  type InsightPackLibrary,
  type InsightRun,
  type InsightScope,
  type InsightVerdict,
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

function RunScheduleDialog({ pack, onClose }: { pack: InsightPack; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: wlData } = useQuery({ queryKey: ["changeWorkloads"], queryFn: () => api.changeExplorerWorkloads() });
  const workloads: ChangeWorkload[] = wlData?.workloads ?? [];

  const supported = pack.supported_scopes.length ? pack.supported_scopes : ["workload"];
  const [mode, setMode] = useState<ScopeMode>(supported[0] as ScopeMode);
  const [workloadId, setWorkloadId] = useState("");
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
      connection_id: selectedWl?.connection_id,
    };
  }

  const run = useMutation({
    mutationFn: () => {
      const scope = buildScope();
      if (!scope) throw new Error("Pick a workload to anchor the scope.");
      return api.runInsightPack({ pack_id: pack.id, scope, notify });
    },
    onSuccess: ({ run }) => { setError(""); setRunResult(run); qc.invalidateQueries({ queryKey: ["insightRuns"] }); },
    onError: (e) => setError(formatError(e)),
  });

  const schedule = useMutation({
    mutationFn: () => {
      const scope = buildScope();
      if (!scope) throw new Error("Pick a workload to anchor the scope.");
      return api.upsertTask({
        name: `${pack.name} — ${selectedWl?.name ?? "scope"}`,
        target_type: "insight_pack",
        target_config: { pack_id: pack.id, mode, workload_ids: [workloadId], connection_id: scope.connection_id },
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
function PackCard({ pack, onRun, onEdit, onClone, onDelete, onToggle, latest }: {
  pack: InsightPack;
  onRun: () => void;
  onEdit: () => void;
  onClone: () => void;
  onDelete: () => void;
  onToggle: () => void;
  latest?: InsightRun;
}) {
  return (
    <div className="flex flex-col rounded-2xl border border-gray-200 bg-white p-4 shadow-sm transition hover:shadow-md">
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="text-2xl leading-none">{pack.icon || "🧠"}</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-900">{pack.name}</span>
              {pack.builtin && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-500">Built-in</span>}
            </div>
            <span className="mt-0.5 inline-block rounded bg-brand/5 px-1.5 py-0.5 text-[11px] font-medium text-brand">{pack.category}</span>
          </div>
        </div>
        <button onClick={onToggle} title={pack.enabled ? "Enabled" : "Disabled"}
          className={`relative h-5 w-9 rounded-full transition ${pack.enabled ? "bg-brand" : "bg-gray-300"}`}>
          <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${pack.enabled ? "left-4" : "left-0.5"}`} />
        </button>
      </div>

      <p className="mt-2 line-clamp-2 text-xs text-gray-500">{pack.description}</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {pack.sources.map((s) => <span key={s} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{s}</span>)}
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">last {pack.lookback_hours}h</span>
        {pack.materiality.always_notify_if.length > 0 && (
          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] text-red-600">{pack.materiality.always_notify_if.length} always-notify</span>
        )}
      </div>

      {latest && (
        <div className="mt-3 flex items-center gap-2 rounded-lg bg-gray-50 px-2.5 py-1.5">
          <VerdictBadge verdict={latest.verdict} />
          <span className="truncate text-[11px] text-gray-500">{latest.headline}</span>
        </div>
      )}

      <div className="mt-4 flex items-center gap-1.5 border-t border-gray-100 pt-3">
        <button onClick={onRun} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90">Run / schedule</button>
        <button onClick={onEdit} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100">Edit</button>
        <button onClick={onClone} className="rounded-lg px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100">Clone</button>
        {!pack.builtin && <button onClick={onDelete} className="ml-auto rounded-lg px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50">Delete</button>}
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
  const [tab, setTab] = useState<"library" | "runs" | "schedule">("library");
  // Deep-links (e.g. from insight notifications) can target a tab via /insights/:section.
  useEffect(() => {
    if (section === "runs" || section === "schedule" || section === "library") setTab(section);
  }, [section]);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [editing, setEditing] = useState<InsightPack | null>(null);
  const [running, setRunning] = useState<InsightPack | null>(null);
  const [viewRun, setViewRun] = useState<InsightRun | null>(null);

  const lib = useQuery({ queryKey: ["insightPacks"], queryFn: () => api.insightPacks() });
  const templates = useQuery({ queryKey: ["insightTemplates"], queryFn: () => api.insightTemplates() });
  const latest = useQuery({ queryKey: ["insightLatest"], queryFn: () => api.insightLatest() });
  const runs = useQuery({ queryKey: ["insightRuns"], queryFn: () => api.insightRuns(), enabled: tab === "runs" });
  const upcoming = useQuery({ queryKey: ["insightUpcoming"], queryFn: () => api.insightUpcoming(7), enabled: tab === "schedule" });

  const latestByPack = useMemo(() => {
    const m: Record<string, InsightRun> = {};
    for (const r of latest.data?.latest ?? []) m[r.pack_id] = r;
    return m;
  }, [latest.data]);

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

  const library = lib.data;
  const packs = library?.packs ?? [];
  const existingIds = new Set(packs.map((p) => p.id));
  const availableTemplates = (templates.data?.templates ?? []).filter((t) => !existingIds.has(t.id));

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
          {(["library", "runs", "schedule"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`rounded-lg px-3 py-1.5 text-sm capitalize transition ${tab === t ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"}`}>
              {t === "runs" ? "Recent runs" : t === "schedule" ? "Next 7 days" : "Library"}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        {tab === "library" && (
          <>
            {lib.isLoading ? (
              <p className="text-sm text-gray-500">Loading packs…</p>
            ) : packs.length === 0 ? (
              <p className="text-sm text-gray-500">No packs yet. Generate one with AI or start from a template below.</p>
            ) : (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                {packs.map((p) => (
                  <PackCard key={p.id} pack={p} latest={latestByPack[p.id]}
                    onRun={() => setRunning(p)}
                    onEdit={() => setEditing(p)}
                    onClone={() => clone.mutate(p.id)}
                    onDelete={() => { if (confirm(`Delete "${p.name}"?`)) del.mutate(p.id); }}
                    onToggle={() => toggle.mutate(p)} />
                ))}
              </div>
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
          <div className="space-y-2">
            {runs.isLoading ? (
              <p className="text-sm text-gray-500">Loading runs…</p>
            ) : (runs.data?.runs ?? []).length === 0 ? (
              <p className="text-sm text-gray-500">No runs yet. Run a pack to see digests here.</p>
            ) : (
              (runs.data?.runs ?? []).map((r) => (
                <button key={r.id} onClick={() => setViewRun(r)}
                  className="flex w-full items-center gap-3 rounded-xl border border-gray-200 bg-white px-4 py-3 text-left transition hover:shadow-sm">
                  <span className="text-xl">{r.pack_icon}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-900">{r.pack_name}</span>
                      <VerdictBadge verdict={r.verdict} />
                      {r.notified && <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[10px] text-brand">Notified</span>}
                    </div>
                    <div className="truncate text-xs text-gray-500">{r.headline}</div>
                  </div>
                  <div className="shrink-0 text-right text-xs text-gray-400">
                    <div>{r.scope_label}</div>
                    <div>{timeAgo(r.created_at)}</div>
                  </div>
                </button>
              ))
            )}
          </div>
        )}

        {tab === "schedule" && (
          <div className="space-y-2">
            {upcoming.isLoading ? (
              <p className="text-sm text-gray-500">Loading schedule…</p>
            ) : (upcoming.data?.occurrences ?? []).length === 0 ? (
              <p className="text-sm text-gray-500">No scheduled packs in the next 7 days. Open a pack and create a schedule.</p>
            ) : (
              (upcoming.data?.occurrences ?? []).map((o, i) => (
                <div key={`${o.task_id}-${i}`} className="flex items-center gap-3 rounded-xl border border-gray-200 bg-white px-4 py-3">
                  <span className="text-xl">{o.pack_icon}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-gray-900">{o.pack_name}</div>
                    <div className="truncate text-xs text-gray-500">{o.task_name} · {o.schedule_label}</div>
                  </div>
                  <div className="shrink-0 text-right text-xs text-gray-500">
                    {new Date(o.at).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </div>
                </div>
              ))
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
      {running && <RunScheduleDialog pack={running} onClose={() => setRunning(null)} />}
      {viewRun && (
        <Modal title="Insight digest" onClose={() => setViewRun(null)} wide>
          <DigestView run={viewRun} />
        </Modal>
      )}
    </div>
  );
}
