import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Playbook, type PlaybookDraft, type PlaybookStep, type PlaybookRunResult, type Severity } from "../api";
import { formatError, formatRelativeFromNow, formatDuration, formatTimestamp } from "../utils/format";
import { SeverityBadge } from "./WorkbooksView";
import { AIDesigner } from "./AIDesigner";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

export function PlaybooksSection() {
  const qc = useQueryClient();
  const pbQ = useQuery({ queryKey: ["playbooks"], queryFn: api.playbooks });
  const [editing, setEditing] = useState<Partial<Playbook> | null>(null);
  const [result, setResult] = useState<PlaybookRunResult | null>(null);
  const [busyId, setBusyId] = useState("");
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [designing, setDesigning] = useState(false);
  const [proposed, setProposed] = useState<{ title: string; purpose: string }[]>([]);
  const [msg, setMsg] = useState("");
  const importRef = useRef<HTMLInputElement>(null);
  const [importMsg, setImportMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const playbooks = pbQ.data?.playbooks ?? [];

  async function run(id: string) {
    setBusyId(id);
    setMsg("");
    try {
      const res = await api.runPlaybook(id);
      setResult(res.result);
      qc.invalidateQueries({ queryKey: ["playbookRuns", id] });
    } catch (e) {
      setMsg(formatError(e));
    } finally {
      setBusyId("");
    }
  }

  async function remove(id: string) {
    try {
      await api.deletePlaybook(id);
      qc.invalidateQueries({ queryKey: ["playbooks"] });
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  function downloadJson(filename: string, data: unknown) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function exportOne(p: Playbook) {
    try {
      const bundle = await api.exportPlaybook(p.id);
      const safe = p.name.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
      downloadJson(`playbook-${safe || p.id}.json`, bundle);
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function onImportFile(file: File) {
    setImportMsg(null);
    try {
      const bundle = JSON.parse(await file.text());
      const res = await api.importPlaybook(bundle);
      const bits = [`Imported playbook “${res.playbook.name}”`];
      if (res.workbooks_imported) bits.push(`${res.workbooks_imported} workbook(s)`);
      if (res.steps_dropped) bits.push(`${res.steps_dropped} step(s) dropped (unresolved)`);
      setImportMsg({ ok: true, text: bits.join(" · ") + "." });
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      qc.invalidateQueries({ queryKey: ["workbooks"] });
    } catch (e) {
      setImportMsg({ ok: false, text: formatError(e) });
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Playbooks</h1>
          <p className="mt-1 text-sm text-gray-500">
            Chain workbooks into a multi-step flow. Each step can run conditionally on the
            running severity and map a previous step's extracted output into its parameters.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {!designing && !editing && (
            <button onClick={() => setDesigning(true)} className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5">
              ✨ Generate with AI
            </button>
          )}
          <button
            onClick={() => importRef.current?.click()}
            title="Import a playbook bundle (includes its workbooks) from JSON"
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50"
          >
            ⬆ Import
          </button>
          <input
            ref={importRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void onImportFile(f);
              e.target.value = "";
            }}
          />
          <button
            onClick={() =>
              setEditing({ name: "", description: "", steps: [], alert: { enabled: false, min_severity: "warning" }, enabled: true })
            }
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
          >
            + New playbook
          </button>
        </div>
      </div>

      {designing && (
        <AIDesigner<PlaybookDraft>
          title="Design a playbook with AI"
          goalLabel="What investigation or operation should this playbook automate?"
          placeholder="e.g. Audit expiring Key Vault certificates, then if any are critical, check which apps reference that vault and report the impact."
          examples={[
            "Security posture sweep: public storage, open NSGs, then drill into exposed apps",
            "Cost cleanup: find unattached disks and idle public IPs",
            "Reliability check: single-zone VMs, then their dependencies",
          ]}
          generatingLabel="Designing your playbook — ordering steps and wiring severity gates…"
          onInterview={(goal, answers, step) => api.playbookInterview(goal, answers, step)}
          onGenerate={async (goal, answers) => (await api.playbookGenerate(goal, answers)).draft}
          onDraft={(draft) => {
            setDesigning(false);
            setProposed(draft.proposed_workbooks ?? []);
            setEditing({
              name: draft.name,
              description: draft.description,
              steps: draft.steps,
              alert: draft.alert,
              enabled: true,
            });
          }}
          onCancel={() => setDesigning(false)}
        />
      )}

      {proposed.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <div className="font-medium">The AI suggested {proposed.length} new workbook(s) this playbook could use — generate them in the Workbooks tab, then add as steps:</div>
          <ul className="mt-1 list-disc pl-4">
            {proposed.map((p) => <li key={p.title}><span className="font-medium">{p.title}</span> — {p.purpose}</li>)}
          </ul>
          <button onClick={() => setProposed([])} className="mt-1 text-amber-600 hover:underline">Dismiss</button>
        </div>
      )}

      {msg && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{msg}</div>}
      {importMsg && (
        <div
          className={`flex items-center justify-between rounded-lg px-3 py-2 text-sm ${importMsg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}
        >
          <span>{importMsg.text}</span>
          <button onClick={() => setImportMsg(null)} className="text-xs opacity-70 hover:opacity-100">Dismiss</button>
        </div>
      )}

      <div className="space-y-3">
        {playbooks.map((p) => (
          <div key={p.id} className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <span className="font-semibold text-gray-800">{p.name}</span>
                <p className="mt-0.5 text-xs text-gray-500">{p.description}</p>
                <p className="mt-1 text-[11px] text-gray-400">{p.steps.length} step(s)</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  onClick={() => void run(p.id)}
                  disabled={busyId === p.id}
                  className="rounded-lg bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-60"
                >
                  {busyId === p.id ? "Running…" : "▶ Run"}
                </button>
                <button
                  onClick={() => setHistoryFor(historyFor === p.id ? null : p.id)}
                  className={`rounded-lg border px-2.5 py-1 text-xs ${historyFor === p.id ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
                >
                  History
                </button>
                <button onClick={() => setEditing(p)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Edit</button>
                <button onClick={() => void exportOne(p)} title="Export bundle (with its workbooks) as JSON" className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Export</button>
                <button onClick={() => void remove(p.id)} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
              </div>
            </div>
            {historyFor === p.id && <PlaybookHistory playbook={p} onRun={() => void run(p.id)} />}
          </div>
        ))}
        {playbooks.length === 0 && (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
            No playbooks yet. Create one to orchestrate several workbooks together.
          </div>
        )}
      </div>

      {editing && (
        <PlaybookForm
          value={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["playbooks"] });
          }}
        />
      )}

      {result && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setResult(null)}>
          <div className="max-h-[88vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="font-semibold text-gray-800">Run result: {result.name}</h3>
              <SeverityBadge severity={result.severity} />
            </div>
            <div className="space-y-2">
              {result.steps.map((s, i) => (
                <div key={i} className="rounded-lg border p-2 text-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-700">{s.name || s.step_id}</span>
                    {s.skipped ? (
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">skipped</span>
                    ) : (
                      <SeverityBadge severity={s.severity} />
                    )}
                  </div>
                  {s.narrative && <p className="mt-1 text-xs text-gray-600">{s.narrative}</p>}
                  {s.reason && <p className="mt-1 text-xs text-gray-400">{s.reason}</p>}
                  {s.error && <p className="mt-1 text-xs text-red-600">{s.error}</p>}
                </div>
              ))}
            </div>
            <button onClick={() => setResult(null)} className="mt-4 rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Close</button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Inline, collapsible run history for a playbook card — visible without running it. */
function PlaybookHistory({ playbook, onRun }: { playbook: Playbook; onRun: () => void }) {
  const q = useQuery({
    queryKey: ["playbookRuns", playbook.id],
    queryFn: () => api.playbookRuns(playbook.id),
    refetchInterval: (query) => {
      const runs = query.state.data?.runs ?? [];
      return runs.some((r) => r.status === "running") ? 3000 : false;
    },
  });
  const [openId, setOpenId] = useState<string | null>(null);
  const runs = q.data?.runs ?? [];

  return (
    <div className="mt-3 rounded-lg border bg-gray-50/60 p-2">
      <div className="mb-1.5 flex items-center justify-between px-1">
        <span className="text-[11px] font-semibold text-gray-600">Run history</span>
        <span className="text-[10px] text-gray-400">{runs.length} run{runs.length === 1 ? "" : "s"}</span>
      </div>
      {q.isLoading ? (
        <div className="px-1 py-2 text-[11px] text-gray-400">Loading…</div>
      ) : runs.length === 0 ? (
        <div className="flex items-center justify-between px-1 py-2">
          <span className="text-[11px] text-gray-400">No runs yet.</span>
          <button onClick={onRun} className="rounded border border-brand/40 px-2 py-0.5 text-[11px] text-brand hover:bg-brand/5">▶ Run now</button>
        </div>
      ) : (
        <div className="max-h-72 space-y-1 overflow-y-auto">
          {runs.slice(0, 25).map((r) => {
            const open = openId === r.id;
            const done = r.steps.filter((s) => !s.skipped).length;
            const skipped = r.steps.filter((s) => s.skipped).length;
            return (
              <div key={r.id} className="rounded border bg-white">
                <button
                  onClick={() => setOpenId(open ? null : r.id)}
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11px]"
                >
                  <span className={`text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
                  <SeverityBadge severity={r.severity} />
                  <span className={`shrink-0 font-medium ${r.status === "succeeded" ? "text-green-600" : r.status === "running" ? "text-blue-600" : "text-red-600"}`}>{r.status}</span>
                  {r.trigger && r.trigger !== "manual" && <span className="shrink-0 rounded bg-gray-100 px-1 text-[9px] text-gray-500">{r.trigger}</span>}
                  <span className="min-w-0 flex-1 truncate text-gray-500">{done} step{done === 1 ? "" : "s"}{skipped > 0 ? ` · ${skipped} skipped` : ""}</span>
                  {r.duration_ms != null && <span className="shrink-0 text-gray-400">{formatDuration(r.duration_ms)}</span>}
                  <span className="shrink-0 text-gray-400" title={r.started_at ? formatTimestamp(r.started_at) : ""}>{r.started_at ? formatRelativeFromNow(r.started_at) : ""}</span>
                </button>
                {open && (
                  <div className="space-y-1 border-t px-2 py-2">
                    {r.steps.map((s, i) => (
                      <div key={i} className="rounded border bg-gray-50/60 p-1.5 text-[11px]">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-gray-700">{s.name || s.step_id}</span>
                          {s.skipped ? (
                            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-500">skipped</span>
                          ) : (
                            <>
                              <SeverityBadge severity={s.severity} />
                              {s.status && <span className={s.status === "succeeded" ? "text-green-600" : "text-red-600"}>{s.status}</span>}
                            </>
                          )}
                        </div>
                        {s.narrative && <p className="mt-0.5 text-gray-600">{s.narrative}</p>}
                        {s.reason && <p className="mt-0.5 text-gray-400">{s.reason}</p>}
                        {s.error && <p className="mt-0.5 text-red-600">{s.error}</p>}
                      </div>
                    ))}
                    {r.error && <div className="text-[11px] text-red-600">{r.error}</div>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function PlaybookForm({ value, onClose, onSaved }: { value: Partial<Playbook>; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<Partial<Playbook>>(value);
  const [error, setError] = useState("");
  const set = (patch: Partial<Playbook>) => setForm((f) => ({ ...f, ...patch }));
  const wbQ = useQuery({ queryKey: ["workbooks"], queryFn: api.workbooks });
  const workbooks = wbQ.data?.workbooks ?? [];
  const steps = form.steps ?? [];
  const alert = form.alert ?? { enabled: false, min_severity: "warning" as Severity };

  function setStep(i: number, patch: Partial<PlaybookStep>) {
    const next = [...steps];
    next[i] = { ...next[i], ...patch };
    set({ steps: next });
  }

  async function save() {
    if (!form.name?.trim()) {
      setError("Give the playbook a name.");
      return;
    }
    try {
      await api.upsertPlaybook(form);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">{form.id ? "Edit playbook" : "New playbook"}</h2>
          <button onClick={onClose} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-5">
          <div>
            <label className={label}>Name</label>
            <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
          </div>
          <div>
            <label className={label}>Description</label>
            <input className={input} value={form.description ?? ""} onChange={(e) => set({ description: e.target.value })} />
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <label className={label + " mb-0"}>Steps</label>
              <button
                onClick={() => set({ steps: [...steps, { id: `s${steps.length + 1}`, name: "", workbook_id: "", params: {}, param_map: {}, run_if: "always" }] })}
                className="text-xs text-brand hover:underline"
              >
                + Add step
              </button>
            </div>
            <div className="space-y-2">
              {steps.map((s, i) => (
                <div key={i} className="rounded-lg border p-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-medium text-gray-400">#{i + 1}</span>
                    <input className={input + " flex-1"} placeholder="Step name" value={s.name} onChange={(e) => setStep(i, { name: e.target.value })} />
                    <button onClick={() => set({ steps: steps.filter((_, j) => j !== i) })} className="text-gray-400 hover:text-red-500">✕</button>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <select className={input} value={s.workbook_id} onChange={(e) => setStep(i, { workbook_id: e.target.value })}>
                      <option value="">Select workbook…</option>
                      {workbooks.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                    </select>
                    <select className={input} value={s.run_if} onChange={(e) => setStep(i, { run_if: e.target.value as PlaybookStep["run_if"] })}>
                      <option value="always">Always run</option>
                      <option value="warning">Only if running severity ≥ warning</option>
                      <option value="error">Only if running severity ≥ error</option>
                      <option value="critical">Only if running severity ≥ critical</option>
                    </select>
                  </div>
                </div>
              ))}
              {steps.length === 0 && <p className="text-[11px] text-gray-400">No steps yet.</p>}
            </div>
          </div>

          <div className="rounded-lg border p-3">
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
              <input type="checkbox" checked={alert.enabled} onChange={(e) => set({ alert: { ...alert, min_severity: alert.min_severity, enabled: e.target.checked } })} />
              Emit a notification event when finished
            </label>
            {alert.enabled && (
              <div className="mt-2">
                <label className={label}>Minimum severity</label>
                <select className={input} value={alert.min_severity} onChange={(e) => set({ alert: { ...alert, min_severity: e.target.value as Severity } })}>
                  {["info", "warning", "error", "critical"].map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
          {error && <div className="mr-auto text-xs text-red-600">{error}</div>}
          <button onClick={onClose} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
        </div>
      </div>
    </div>
  );
}
