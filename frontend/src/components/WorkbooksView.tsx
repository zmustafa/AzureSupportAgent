import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type Workbook,
  type WorkbookDraft,
  type WorkbookParam,
  type WorkbookRun,
  type Severity,
} from "../api";
import { formatError, formatRelativeFromNow, formatDuration, formatTimestamp } from "../utils/format";
import { AIDesigner } from "./AIDesigner";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

const RUNTIME_LABELS: Record<string, string> = {
  az: "Azure CLI",
  kql: "Resource Graph (KQL)",
  powershell: "PowerShell",
};
const AIFY_MODES: { id: string; label: string; help: string }[] = [
  { id: "summary", label: "Summarize", help: "Plain-English summary of the output." },
  { id: "severity", label: "Classify severity", help: "info / warning / error / critical." },
  { id: "extract", label: "Extract to schema", help: "Pull typed fields from the output." },
  { id: "diff", label: "Diff vs last run", help: "What changed since the previous run." },
];

export function SeverityBadge({ severity }: { severity?: Severity | null }) {
  const s = severity ?? "info";
  const cls: Record<string, string> = {
    info: "bg-gray-100 text-gray-600",
    warning: "bg-amber-100 text-amber-700",
    error: "bg-orange-100 text-orange-700",
    critical: "bg-red-100 text-red-700",
  };
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${cls[s]}`}>
      {s}
    </span>
  );
}

export function WorkbooksSection() {
  const qc = useQueryClient();
  const wbQ = useQuery({ queryKey: ["workbooks"], queryFn: api.workbooks });
  const [editing, setEditing] = useState<Partial<Workbook> | null>(null);
  const [running, setRunning] = useState<Workbook | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [designing, setDesigning] = useState(false);
  const [msg, setMsg] = useState("");
  const importRef = useRef<HTMLInputElement>(null);
  const [importMsg, setImportMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const workbooks = wbQ.data?.workbooks ?? [];

  async function remove(id: string) {
    try {
      await api.deleteWorkbook(id);
      qc.invalidateQueries({ queryKey: ["workbooks"] });
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

  async function exportOne(w: Workbook) {
    try {
      const bundle = await api.exportWorkbook(w.id);
      const safe = w.name.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
      downloadJson(`workbook-${safe || w.id}.json`, bundle);
    } catch (e) {
      setMsg(formatError(e));
    }
  }

  async function onImportFile(file: File) {
    setImportMsg(null);
    try {
      const bundle = JSON.parse(await file.text());
      const res = await api.importWorkbook(bundle);
      setImportMsg({ ok: true, text: `Imported workbook “${res.workbook.name}”.` });
      qc.invalidateQueries({ queryKey: ["workbooks"] });
    } catch (e) {
      setImportMsg({ ok: false, text: formatError(e) });
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Workbooks</h1>
          <p className="mt-1 text-sm text-gray-500">
            Saved Azure operations — az CLI, Resource Graph (KQL), or PowerShell — whose
            output is AI-summarized, severity-classified, and reusable as dashboard tiles,
            alerts, and playbook steps.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {!designing && !editing && (
            <button
              onClick={() => setDesigning(true)}
              className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5"
            >
              ✨ Generate with AI
            </button>
          )}
          <button
            onClick={() => importRef.current?.click()}
            title="Import a workbook from a JSON export file"
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
              setEditing({
                name: "",
                runtime: "kql",
                body: "",
                params: [],
                kind: "read",
                aify: { enabled: true, modes: ["summary", "severity"], schema: "" },
                alert: { enabled: false, min_severity: "warning" },
                tile: { enabled: false, label: "", format: "severity", metric_key: "" },
                enabled: true,
              })
            }
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
          >
            + New workbook
          </button>
        </div>
      </div>

      {designing && (
        <AIDesigner<WorkbookDraft>
          title="Design a workbook with AI"
          goalLabel="What should this workbook check or report?"
          placeholder="e.g. Find storage accounts without a CostCenter tag, summarize how many and which resource groups, and show it as a dashboard tile."
          examples={[
            "List VMs that aren't in an availability zone",
            "Count Key Vaults without purge protection",
            "Find public IPs not attached to anything (cost waste)",
            "Report App Services not enforcing HTTPS",
          ]}
          generatingLabel="Designing your workbook — choosing runtime, writing the query and parameters…"
          onInterview={(goal, answers, step) => api.workbookInterview(goal, answers, step)}
          onGenerate={async (goal, answers) => (await api.workbookGenerate(goal, answers)).draft}
          onDraft={(draft) => {
            setDesigning(false);
            setEditing({
              name: draft.name,
              description: draft.description,
              runtime: draft.runtime,
              body: draft.body,
              params: draft.params,
              kind: draft.kind,
              tags: draft.tags,
              aify: draft.aify,
              alert: draft.alert,
              tile: draft.tile,
              enabled: true,
            });
          }}
          onCancel={() => setDesigning(false)}
        />
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

      {wbQ.isLoading && <div className="text-sm text-gray-500">Loading…</div>}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {workbooks.map((w) => (
          <div key={w.id} className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate font-semibold text-gray-800">{w.name}</span>
                  {w.starter && (
                    <span className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">
                      starter
                    </span>
                  )}
                </div>
                <p className="mt-0.5 line-clamp-2 text-xs text-gray-500">{w.description}</p>
              </div>
              <span className="shrink-0 rounded bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">
                {RUNTIME_LABELS[w.runtime] ?? w.runtime}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {(w.tags ?? []).map((t) => (
                <span key={t} className="rounded bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-500">
                  #{t}
                </span>
              ))}
              {w.tile?.enabled && (
                <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-600">
                  tile
                </span>
              )}
              {w.alert?.enabled && (
                <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">
                  alert ≥ {w.alert.min_severity}
                </span>
              )}
            </div>
            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={() => setRunning(w)}
                className="rounded-lg bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand/90"
              >
                ▶ Run
              </button>
              <button
                onClick={() => setHistoryFor(historyFor === w.id ? null : w.id)}
                className={`rounded-lg border px-2.5 py-1 text-xs ${historyFor === w.id ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
              >
                History
              </button>
              <button
                onClick={() => setEditing(w)}
                className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50"
              >
                Edit
              </button>
              <button
                onClick={() => void exportOne(w)}
                title="Export as JSON"
                className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50"
              >
                Export
              </button>
              <button
                onClick={() => void remove(w.id)}
                className="ml-auto rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50"
              >
                Delete
              </button>
            </div>
            {historyFor === w.id && <WorkbookHistory workbook={w} onRun={() => setRunning(w)} />}
          </div>
        ))}
      </div>

      {workbooks.length === 0 && !wbQ.isLoading && (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-gray-500">
          No workbooks yet. Create one to codify a reusable Azure operation.
        </div>
      )}

      {editing && (
        <WorkbookForm
          value={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            qc.invalidateQueries({ queryKey: ["workbooks"] });
          }}
        />
      )}
      {running && <RunPanel workbook={running} onClose={() => setRunning(null)} />}
    </div>
  );
}

function WorkbookForm({
  value,
  onClose,
  onSaved,
}: {
  value: Partial<Workbook>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Partial<Workbook>>(value);
  const [error, setError] = useState("");
  const set = (patch: Partial<Workbook>) => setForm((f) => ({ ...f, ...patch }));
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections });
  const params = form.params ?? [];

  const [testValues, setTestValues] = useState<Record<string, string>>({});
  const [testBusy, setTestBusy] = useState(false);
  const [testRun, setTestRun] = useState<WorkbookRun | null>(null);
  const [testError, setTestError] = useState("");

  const aify = form.aify ?? { enabled: true, modes: [], schema: "" };
  const alert = form.alert ?? { enabled: false, min_severity: "warning" as Severity };
  const tile = form.tile ?? { enabled: false, label: "", format: "severity" as const, metric_key: "" };

  function setParam(i: number, patch: Partial<WorkbookParam>) {
    const next = [...params];
    next[i] = { ...next[i], ...patch };
    set({ params: next });
  }

  async function save() {
    if (!form.name?.trim()) {
      setError("Give the workbook a name.");
      return;
    }
    try {
      await api.upsertWorkbook(form);
      onSaved();
    } catch (e) {
      setError(formatError(e));
    }
  }

  async function doTest() {
    if (!form.body?.trim()) {
      setTestError("Add a body to test.");
      return;
    }
    setTestBusy(true);
    setTestError("");
    setTestRun(null);
    try {
      const res = await api.previewWorkbook({
        workbook: form,
        params: testValues,
        connection_id: form.connection_id || null,
        confirm: true,
      });
      setTestRun(res.run);
    } catch (e) {
      setTestError(formatError(e));
    } finally {
      setTestBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-800">
            {form.id ? "Edit workbook" : "New workbook"}
          </h2>
          <button onClick={onClose} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className={label}>Name</label>
              <input className={input} value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
            </div>
            <div>
              <label className={label}>Runtime</label>
              <select className={input} value={form.runtime ?? "kql"} onChange={(e) => set({ runtime: e.target.value as Workbook["runtime"] })}>
                <option value="kql">Resource Graph (KQL)</option>
                <option value="az">Azure CLI (az)</option>
                <option value="powershell">PowerShell</option>
              </select>
            </div>
          </div>
          <div>
            <label className={label}>Description</label>
            <input className={input} value={form.description ?? ""} onChange={(e) => set({ description: e.target.value })} />
          </div>
          <div>
            <label className={label}>
              Body{" "}
              <span className="font-normal text-gray-400">
                — use {"{{param}}"} placeholders for parameters
              </span>
            </label>
            <textarea
              rows={6}
              className={input + " font-mono text-[12px]"}
              value={form.body ?? ""}
              onChange={(e) => set({ body: e.target.value })}
              placeholder={form.runtime === "az" ? "az group list -o json" : "Resources | project name, type | limit 50"}
            />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className={label}>Default Azure connection</label>
              <select className={input} value={form.connection_id ?? ""} onChange={(e) => set({ connection_id: e.target.value })}>
                <option value="">Default tenant</option>
                {(connQ.data?.connections ?? []).map((c) => (
                  <option key={c.id} value={c.id}>{c.display_name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={label}>Tags (comma-separated)</label>
              <input
                className={input}
                value={(form.tags ?? []).join(", ")}
                onChange={(e) => set({ tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })}
              />
            </div>
          </div>

          {/* Parameters */}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <label className={label + " mb-0"}>Parameters</label>
              <button
                onClick={() => set({ params: [...params, { key: "", label: "", type: "text", default: "", required: false, help: "" }] })}
                className="text-xs text-brand hover:underline"
              >
                + Add parameter
              </button>
            </div>
            {params.length === 0 && <p className="text-[11px] text-gray-400">No parameters.</p>}
            <div className="space-y-2">
              {params.map((p, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input className={input + " flex-1"} placeholder="key" value={p.key} onChange={(e) => setParam(i, { key: e.target.value })} />
                  <input className={input + " flex-1"} placeholder="label" value={p.label} onChange={(e) => setParam(i, { label: e.target.value })} />
                  <input className={input + " w-28"} placeholder="default" value={String(p.default ?? "")} onChange={(e) => setParam(i, { default: e.target.value })} />
                  <label className="flex items-center gap-1 text-[11px] text-gray-500">
                    <input type="checkbox" checked={!!p.required} onChange={(e) => setParam(i, { required: e.target.checked })} />
                    req
                  </label>
                  <button onClick={() => set({ params: params.filter((_, j) => j !== i) })} className="text-gray-400 hover:text-red-500">✕</button>
                </div>
              ))}
            </div>
          </div>

          {/* AI'fication */}
          <div className="rounded-lg border bg-gray-50 p-3">
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
              <input type="checkbox" checked={aify.enabled} onChange={(e) => set({ aify: { ...aify, enabled: e.target.checked } })} />
              AI'fy the output
            </label>
            {aify.enabled && (
              <div className="mt-2 space-y-2">
                <div className="flex flex-wrap gap-1.5">
                  {AIFY_MODES.map((m) => {
                    const on = (aify.modes ?? []).includes(m.id);
                    return (
                      <button
                        key={m.id}
                        title={m.help}
                        onClick={() =>
                          set({
                            aify: {
                              ...aify,
                              modes: on ? aify.modes.filter((x) => x !== m.id) : [...aify.modes, m.id],
                            },
                          })
                        }
                        className={`rounded-lg border px-2.5 py-1 text-xs transition ${
                          on ? "border-brand bg-brand/10 font-medium text-brand" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-100"
                        }`}
                      >
                        {on ? "✓ " : "+ "}{m.label}
                      </button>
                    );
                  })}
                </div>
                {(aify.modes ?? []).includes("extract") && (
                  <input
                    className={input}
                    placeholder="What to extract (e.g. count of accounts and their names)"
                    value={aify.schema ?? ""}
                    onChange={(e) => set({ aify: { ...aify, schema: e.target.value } })}
                  />
                )}
              </div>
            )}
          </div>

          {/* Alert + Tile */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="rounded-lg border p-3">
              <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
                <input type="checkbox" checked={alert.enabled} onChange={(e) => set({ alert: { ...alert, enabled: e.target.checked } })} />
                Emit alert event
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
            <div className="rounded-lg border p-3">
              <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
                <input type="checkbox" checked={tile.enabled} onChange={(e) => set({ tile: { ...tile, enabled: e.target.checked } })} />
                Dashboard tile
              </label>
              {tile.enabled && (
                <div className="mt-2 space-y-2">
                  <input className={input} placeholder="Tile label" value={tile.label} onChange={(e) => set({ tile: { ...tile, label: e.target.value } })} />
                  <div className="flex gap-2">
                    <select className={input} value={tile.format} onChange={(e) => set({ tile: { ...tile, format: e.target.value as "severity" | "number" | "text" } })}>
                      <option value="severity">Severity</option>
                      <option value="number">Number</option>
                      <option value="text">Text</option>
                    </select>
                    {tile.format === "number" && (
                      <input className={input} placeholder="metric key" value={tile.metric_key} onChange={(e) => set({ tile: { ...tile, metric_key: e.target.value } })} />
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Test run (preview without saving) */}
          <div className="rounded-lg border border-dashed bg-gray-50 p-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium text-gray-700">Test run</div>
                <p className="text-[11px] text-gray-500">
                  Execute the code and see the output before saving. Nothing is persisted.
                </p>
              </div>
              <button
                onClick={() => void doTest()}
                disabled={testBusy}
                className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand/90 disabled:opacity-60"
              >
                {testBusy ? "Running…" : "▶ Test run"}
              </button>
            </div>

            {params.length > 0 && (
              <div className="mt-2 space-y-2">
                {params.map((p) => (
                  <div key={p.key}>
                    <label className={label}>
                      {p.label || p.key}
                      {p.required && <span className="text-red-500"> *</span>}
                    </label>
                    <input
                      className={input}
                      value={testValues[p.key] ?? String(p.default ?? "")}
                      onChange={(e) => setTestValues((v) => ({ ...v, [p.key]: e.target.value }))}
                      placeholder={p.help}
                    />
                  </div>
                ))}
              </div>
            )}

            {testError && (
              <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{testError}</div>
            )}

            {testRun && (
              <div className="mt-2 space-y-2 rounded-lg border bg-white p-3">
                <div className="flex items-center gap-2">
                  <SeverityBadge severity={testRun.severity} />
                  <span className={`text-xs font-medium ${testRun.status === "succeeded" ? "text-green-600" : "text-red-600"}`}>
                    {testRun.status}
                  </span>
                  {testRun.duration_ms != null && (
                    <span className="text-xs text-gray-400">{testRun.duration_ms} ms</span>
                  )}
                </div>
                {testRun.narrative && <p className="text-sm text-gray-700">{testRun.narrative}</p>}
                {testRun.structured != null && (
                  <details className="text-xs" open>
                    <summary className="cursor-pointer text-gray-500">Structured result</summary>
                    <pre className="mt-1 max-h-52 overflow-auto rounded bg-gray-900 p-2 text-[11px] text-gray-100">
                      {JSON.stringify(testRun.structured, null, 2)}
                    </pre>
                  </details>
                )}
                {testRun.output && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-gray-500">Raw output</summary>
                    <pre className="mt-1 max-h-52 overflow-auto rounded bg-gray-50 p-2 text-[11px] text-gray-700">{testRun.output}</pre>
                  </details>
                )}
                {testRun.error && <div className="text-xs text-red-600">{testRun.error}</div>}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
          {error && <div className="mr-auto text-xs text-red-600">{error}</div>}
          <button
            onClick={() => void doTest()}
            disabled={testBusy}
            className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-60"
          >
            {testBusy ? "Running…" : "▶ Test run"}
          </button>
          <button onClick={onClose} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
        </div>
      </div>
    </div>
  );
}

function RunPanel({ workbook, onClose }: { workbook: Workbook; onClose: () => void }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const p of workbook.params ?? []) init[p.key] = String(p.default ?? "");
    return init;
  });
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<WorkbookRun | null>(null);
  const [error, setError] = useState("");
  const runsQ = useQuery({
    queryKey: ["workbookRuns", workbook.id],
    queryFn: () => api.workbookRuns(workbook.id),
  });

  async function doRun() {
    setBusy(true);
    setError("");
    try {
      const res = await api.runWorkbook(workbook.id, { params: values });
      setRun(res.run);
      qc.invalidateQueries({ queryKey: ["workbookRuns", workbook.id] });
      qc.invalidateQueries({ queryKey: ["workbookTiles"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  const structured = run?.structured;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-800">Run: {workbook.name}</h2>
            <p className="text-xs text-gray-500">{RUNTIME_LABELS[workbook.runtime] ?? workbook.runtime}</p>
          </div>
          <button onClick={onClose} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {(workbook.params ?? []).length > 0 && (
            <div className="space-y-2">
              {(workbook.params ?? []).map((p) => (
                <div key={p.key}>
                  <label className={label}>
                    {p.label || p.key}
                    {p.required && <span className="text-red-500"> *</span>}
                  </label>
                  <input
                    className={input}
                    value={values[p.key] ?? ""}
                    onChange={(e) => setValues((v) => ({ ...v, [p.key]: e.target.value }))}
                    placeholder={p.help}
                  />
                </div>
              ))}
            </div>
          )}

          <button
            onClick={() => void doRun()}
            disabled={busy}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-60"
          >
            {busy ? "Running…" : "▶ Run workbook"}
          </button>

          {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

          {run && (
            <div className="space-y-3 rounded-lg border p-3">
              <div className="flex items-center gap-2">
                <SeverityBadge severity={run.severity} />
                <span className={`text-xs font-medium ${run.status === "succeeded" ? "text-green-600" : "text-red-600"}`}>
                  {run.status}
                </span>
                {run.duration_ms != null && <span className="text-xs text-gray-400">{run.duration_ms} ms</span>}
              </div>
              {run.narrative && <p className="text-sm text-gray-700">{run.narrative}</p>}
              {run.diff?.has_changes && (
                <div className="rounded bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
                  <span className="font-medium">Changes since last run:</span>{" "}
                  {Object.keys(run.diff.changed).length} changed, {run.diff.added.length} added, {run.diff.removed.length} removed
                </div>
              )}
              {structured != null && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-gray-500">Structured result</summary>
                  <pre className="mt-1 overflow-x-auto rounded bg-gray-900 p-2 text-[11px] text-gray-100">
                    {JSON.stringify(structured, null, 2)}
                  </pre>
                </details>
              )}
              {run.output && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-gray-500">Raw output</summary>
                  <pre className="mt-1 max-h-60 overflow-auto rounded bg-gray-50 p-2 text-[11px] text-gray-700">{run.output}</pre>
                </details>
              )}
              {run.error && <div className="text-xs text-red-600">{run.error}</div>}
            </div>
          )}

          <div>
            <div className="mb-1 text-xs font-medium text-gray-500">Recent runs</div>
            <div className="space-y-1">
              {(runsQ.data?.runs ?? []).slice(0, 8).map((r) => (
                <div key={r.id} className="flex items-center gap-2 rounded border px-2 py-1 text-xs">
                  <SeverityBadge severity={r.severity} />
                  <span className={r.status === "succeeded" ? "text-green-600" : "text-red-600"}>{r.status}</span>
                  <span className="truncate text-gray-500">{r.narrative}</span>
                  <span className="ml-auto shrink-0 text-gray-400">
                    {r.started_at ? formatRelativeFromNow(r.started_at) : ""}
                  </span>
                </div>
              ))}
              {(runsQ.data?.runs ?? []).length === 0 && <p className="text-[11px] text-gray-400">No runs yet.</p>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Inline, collapsible run history for a workbook card — visible without running it.
 *  Polls while a run is in progress so a just-triggered run updates live. */
function WorkbookHistory({ workbook, onRun }: { workbook: Workbook; onRun: () => void }) {
  const q = useQuery({
    queryKey: ["workbookRuns", workbook.id],
    queryFn: () => api.workbookRuns(workbook.id),
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
                  <span className="min-w-0 flex-1 truncate text-gray-600" title={r.narrative ?? ""}>{r.narrative}</span>
                  {r.duration_ms != null && <span className="shrink-0 text-gray-400">{formatDuration(r.duration_ms)}</span>}
                  <span className="shrink-0 text-gray-400" title={r.started_at ? formatTimestamp(r.started_at) : ""}>{r.started_at ? formatRelativeFromNow(r.started_at) : ""}</span>
                </button>
                {open && (
                  <div className="space-y-2 border-t px-2 py-2">
                    {r.narrative && <p className="text-xs text-gray-700">{r.narrative}</p>}
                    {r.diff?.has_changes && (
                      <div className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800">
                        <span className="font-medium">Changes since prior run:</span>{" "}
                        {Object.keys(r.diff.changed).length} changed, {r.diff.added.length} added, {r.diff.removed.length} removed
                      </div>
                    )}
                    {r.structured != null && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-gray-500">Structured result</summary>
                        <pre className="mt-1 max-h-44 overflow-auto rounded bg-gray-900 p-2 text-[11px] text-gray-100">{JSON.stringify(r.structured, null, 2)}</pre>
                      </details>
                    )}
                    {r.output && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-gray-500">Raw output</summary>
                        <pre className="mt-1 max-h-44 overflow-auto rounded bg-gray-50 p-2 text-[11px] text-gray-700">{r.output}</pre>
                      </details>
                    )}
                    {r.command && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-gray-500">Command</summary>
                        <pre className="mt-1 overflow-x-auto rounded bg-gray-50 p-2 font-mono text-[11px] text-gray-700">{r.command}</pre>
                      </details>
                    )}
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

export function useWorkbookOptions(): { id: string; name: string }[] {
  const wbQ = useQuery({ queryKey: ["workbooks"], queryFn: api.workbooks });
  return useMemo(() => (wbQ.data?.workbooks ?? []).map((w) => ({ id: w.id, name: w.name })), [wbQ.data]);}
