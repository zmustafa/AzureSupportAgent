import { useState } from "react";
import type { AgentAnswer, AgentInterviewResult, AgentWizardQuestion } from "../api";
import { formatError } from "../utils/format";

const input = "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

type Stage = "intent" | "interview" | "generating" | "error";

/**
 * Reusable two-phase AI designer: a dynamic interview (the model asks adaptive
 * questions) followed by a one-shot generation. The parent supplies the interview and
 * generate callbacks (so this works for Workbooks, Playbooks, etc.) and receives the
 * final draft via onDraft to populate its own editor.
 */
export function AIDesigner<TDraft>({
  title,
  goalLabel,
  placeholder,
  examples = [],
  generatingLabel,
  onInterview,
  onGenerate,
  onDraft,
  onCancel,
}: {
  title: string;
  goalLabel: string;
  placeholder: string;
  examples?: string[];
  generatingLabel: string;
  onInterview: (goal: string, answers: AgentAnswer[], step: number) => Promise<AgentInterviewResult>;
  onGenerate: (goal: string, answers: AgentAnswer[]) => Promise<TDraft>;
  onDraft: (draft: TDraft) => void;
  onCancel: () => void;
}) {
  const [stage, setStage] = useState<Stage>("intent");
  const [goal, setGoal] = useState("");
  const [step, setStep] = useState(0);
  const [questions, setQuestions] = useState<AgentWizardQuestion[]>([]);
  const [note, setNote] = useState("");
  const [answers, setAnswers] = useState<AgentAnswer[]>([]);
  const [current, setCurrent] = useState<Record<string, string | string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function startInterview() {
    if (!goal.trim()) {
      setError("Describe what you want to build.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const res = await onInterview(goal.trim(), [], 0);
      if (res.done || res.questions.length === 0) {
        await generate([]);
        return;
      }
      setQuestions(res.questions);
      setNote(res.note);
      setStep(1);
      setCurrent({});
      setCustom({});
      setStage("interview");
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  function setAnswer(qid: string, value: string | string[]) {
    setCurrent((c) => ({ ...c, [qid]: value }));
  }
  function toggleMulti(qid: string, opt: string) {
    setCurrent((c) => {
      const prev = Array.isArray(c[qid]) ? (c[qid] as string[]) : [];
      const next = prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt];
      return { ...c, [qid]: next };
    });
  }

  async function submitStep() {
    const merged: AgentAnswer[] = questions.map((q) => {
      let value: string | string[] = current[q.id] ?? (q.kind === "multi" ? [] : "");
      const extra = (custom[q.id] ?? "").trim();
      if (extra) {
        value = q.kind === "multi" ? [...(Array.isArray(value) ? value : []), extra] : extra;
      }
      return { id: q.id, prompt: q.prompt, answer: value };
    });
    const all = [...answers, ...merged];
    setAnswers(all);
    setBusy(true);
    setError("");
    try {
      const res = await onInterview(goal.trim(), all, step);
      if (res.done || res.questions.length === 0) {
        await generate(all);
        return;
      }
      setQuestions(res.questions);
      setNote(res.note);
      setStep((s) => s + 1);
      setCurrent({});
      setCustom({});
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  async function generate(all: AgentAnswer[]) {
    setStage("generating");
    setBusy(true);
    setError("");
    try {
      const draft = await onGenerate(goal.trim(), all);
      onDraft(draft);
    } catch (e) {
      setError(formatError(e));
      setStage("error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 space-y-4 rounded-xl border border-brand/30 bg-brand/5 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-800">✨ {title}</div>
        <button onClick={onCancel} className="text-xs text-gray-400 hover:text-gray-600">✕ Close</button>
      </div>

      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <span className={stage === "intent" ? "font-semibold text-brand" : ""}>1. Goal</span>
        <span>→</span>
        <span className={stage === "interview" ? "font-semibold text-brand" : ""}>2. AI interview{stage === "interview" ? ` (Q${step})` : ""}</span>
        <span>→</span>
        <span className={stage === "generating" ? "font-semibold text-brand" : ""}>3. Generate</span>
        <span>→</span>
        <span>4. Review &amp; save</span>
      </div>

      {stage === "intent" && (
        <div className="space-y-3">
          <div>
            <label className={label}>{goalLabel}</label>
            <textarea rows={3} className={input} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder={placeholder} />
          </div>
          {examples.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {examples.map((ex) => (
                <button key={ex} onClick={() => setGoal(ex)} className="rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[11px] text-gray-600 transition hover:border-brand/40 hover:text-brand">{ex}</button>
              ))}
            </div>
          )}
          {error && <div className="text-xs text-red-600">{error}</div>}
          <div className="flex gap-2">
            <button onClick={() => void startInterview()} disabled={busy} className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-60">{busy ? "Thinking…" : "Start AI interview →"}</button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          </div>
        </div>
      )}

      {stage === "interview" && (
        <div className="space-y-4">
          {note && <div className="text-xs text-gray-500">{note}</div>}
          {questions.map((q) => (
            <div key={q.id} className="space-y-1.5">
              <div className="text-sm font-medium text-gray-800">{q.prompt}</div>
              {q.kind === "text" ? (
                <textarea rows={2} className={input} value={(current[q.id] as string) ?? ""} onChange={(e) => setAnswer(q.id, e.target.value)} placeholder="Type your answer…" />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {q.options.map((opt) => {
                    const sel = q.kind === "multi" ? Array.isArray(current[q.id]) && (current[q.id] as string[]).includes(opt) : current[q.id] === opt;
                    return (
                      <button key={opt} onClick={() => (q.kind === "multi" ? toggleMulti(q.id, opt) : setAnswer(q.id, opt))}
                        className={`rounded-lg border px-2.5 py-1.5 text-sm transition ${sel ? "border-brand bg-brand/10 font-medium text-brand" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"}`}>
                        {q.kind === "multi" && <span className="mr-1">{sel ? "✓" : "+"}</span>}{opt}
                      </button>
                    );
                  })}
                </div>
              )}
              {q.allow_custom && q.kind !== "text" && (
                <input className={input} value={custom[q.id] ?? ""} onChange={(e) => setCustom((c) => ({ ...c, [q.id]: e.target.value }))} placeholder="Or add your own…" />
              )}
            </div>
          ))}
          {error && <div className="text-xs text-red-600">{error}</div>}
          <div className="flex items-center gap-2">
            <button onClick={() => void submitStep()} disabled={busy} className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-60">{busy ? "Thinking…" : "Continue →"}</button>
            <button onClick={() => void generate(answers)} disabled={busy} className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm text-brand transition hover:bg-brand/5 disabled:opacity-60" title="Skip remaining questions and generate now">Generate now</button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          </div>
        </div>
      )}

      {stage === "generating" && (
        <div className="flex items-center gap-3 py-6 text-sm text-gray-600">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
          {generatingLabel}
        </div>
      )}

      {stage === "error" && (
        <div className="space-y-3">
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error || "The AI could not produce a draft."}</div>
          <div className="flex gap-2">
            <button onClick={() => void generate(answers)} className="rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Retry</button>
            <button onClick={onCancel} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
