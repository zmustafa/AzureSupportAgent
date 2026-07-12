import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type AmbaAlertRef, type AmbaReference } from "../api";
import { formatError } from "../utils/format";
import {
  AMBA_CATEGORIES,
  AMBA_OPERATOR_SYMBOL,
  AMBA_OPERATORS,
  AMBA_SEVERITIES,
  AMBA_UNITS,
  AMBA_WINDOWS,
  CATEGORY_COLOR,
  catalogFor,
  KNOWN_ARM_TYPES,
  type CatalogMetric,
} from "./ambaCatalog";

type RefTypes = AmbaReference["types"];

const SEV_TONE: Record<string, string> = {
  critical: "bg-red-100 text-red-700 border-red-200",
  error: "bg-orange-100 text-orange-700 border-orange-200",
  warning: "bg-amber-100 text-amber-700 border-amber-200",
  info: "bg-sky-100 text-sky-700 border-sky-200",
};

function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60) || "alert";
}

function uniqueKey(base: string, existing: Set<string>): string {
  let k = base;
  let i = 2;
  while (existing.has(k)) k = `${base}_${i++}`;
  return k;
}

function blankAlert(existing: Set<string>): AmbaAlertRef {
  return {
    key: uniqueKey("new_alert", existing),
    name: "New alert",
    amba_category: "performance",
    signal: "metric",
    metric: "",
    operator: "GreaterThan",
    threshold: null,
    unit: "%",
    window: "PT5M",
    severity: "warning",
    requires_action_group: true,
    dimension_filter: "",
    aggregation: "",
    deployable: true,
    why: "",
  };
}

function fromCatalog(c: CatalogMetric, existing: Set<string>): AmbaAlertRef {
  return {
    key: uniqueKey(slugify(c.label), existing),
    name: c.label,
    amba_category: c.amba_category,
    signal: "metric",
    metric: c.metric,
    operator: c.operator,
    threshold: c.threshold,
    unit: c.unit,
    window: c.window,
    severity: "warning",
    requires_action_group: true,
    dimension_filter: "",
    aggregation: "",
    deployable: true,
    why: c.why || "",
  };
}

// English sentence read-back for an alert.
function sentence(a: AmbaAlertRef): string {
  const op = AMBA_OPERATOR_SYMBOL[a.operator] || a.operator;
  const thr = a.threshold != null ? `${a.threshold}${a.unit}` : "(exists)";
  const win = a.window.replace("PT", "").replace("P", "").toLowerCase();
  const ag = a.requires_action_group ? " · needs action group" : "";
  if (a.signal === "log") return `${a.name}: log query → ${a.severity}${ag}`;
  return `${a.metric || "metric"} ${op} ${thr} over ${win} → ${a.severity}${ag}`;
}

// Threshold gauge for numeric % metrics (0-100). Returns null when not graphable.
function Gauge({ alert }: { alert: AmbaAlertRef }) {
  if (alert.threshold == null || alert.unit !== "%") return null;
  const pct = Math.max(0, Math.min(100, alert.threshold));
  const color = CATEGORY_COLOR[alert.amba_category] || "#6b7280";
  return (
    <div className="mt-1">
      <div className="relative h-2 w-full rounded-full bg-gray-100">
        <div className="absolute inset-y-0 left-0 rounded-full opacity-30" style={{ width: `${pct}%`, background: color }} />
        <div className="absolute inset-y-[-2px] w-0.5" style={{ left: `${pct}%`, background: color }} />
      </div>
      <div className="mt-0.5 text-[10px] text-gray-400">threshold at {pct}%</div>
    </div>
  );
}

export function AmbaReferenceEditor() {
  const qc = useQueryClient();
  const refQ = useQuery({ queryKey: ["amba-reference"], queryFn: api.ambaReference });
  const revsQ = useQuery({ queryKey: ["amba-reference-revisions"], queryFn: api.ambaReferenceRevisions });
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });

  const [draft, setDraft] = useState<RefTypes>({});
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<string>("");
  const [search, setSearch] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [rawText, setRawText] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [addTypeOpen, setAddTypeOpen] = useState(false);

  const ref = refQ.data;

  // Load the server reference into the draft once (and after a save resets dirty).
  useEffect(() => {
    if (ref && !dirty) {
      setDraft(JSON.parse(JSON.stringify(ref.types)));
      if (!selected) {
        const first = Object.keys(ref.types).sort()[0];
        if (first) setSelected(first);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref]);

  // Count workload resources per arm type for the "used by" hint.
  const usageByType = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const w of workloadsQ.data?.workloads ?? []) {
      for (const n of (w as { nodes?: { kind?: string; resource_type?: string }[] }).nodes ?? []) {
        if (n.kind === "resource" && n.resource_type) {
          const t = n.resource_type.toLowerCase();
          counts[t] = (counts[t] || 0) + 1;
        }
      }
    }
    return counts;
  }, [workloadsQ.data]);

  const typeList = useMemo(() => {
    const q = search.trim().toLowerCase();
    return Object.entries(draft)
      .filter(([t, spec]) => !q || t.includes(q) || (spec.display || "").toLowerCase().includes(q))
      .sort((a, b) => (a[1].display || a[0]).localeCompare(b[1].display || b[0]));
  }, [draft, search]);

  function mutate(fn: (d: RefTypes) => void) {
    setDraft((prev) => {
      const next = JSON.parse(JSON.stringify(prev)) as RefTypes;
      fn(next);
      return next;
    });
    setDirty(true);
    setMsg(null);
  }

  const cur = selected ? draft[selected] : undefined;

  function updateAlert(key: string, patch: Partial<AmbaAlertRef>) {
    mutate((d) => {
      const spec = d[selected];
      if (!spec) return;
      spec.alerts = spec.alerts.map((a) => (a.key === key ? { ...a, ...patch } : a));
    });
  }
  function deleteAlert(key: string) {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.alerts = spec.alerts.filter((a) => a.key !== key);
    });
  }
  function duplicateAlert(key: string) {
    mutate((d) => {
      const spec = d[selected];
      if (!spec) return;
      const a = spec.alerts.find((x) => x.key === key);
      if (!a) return;
      const keys = new Set(spec.alerts.map((x) => x.key));
      spec.alerts.push({ ...a, key: uniqueKey(`${a.key}_copy`, keys), name: `${a.name} (copy)` });
    });
  }
  function addBlankAlert() {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.alerts.push(blankAlert(new Set(spec.alerts.map((a) => a.key))));
    });
  }
  function addCatalogMetric(c: CatalogMetric) {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.alerts.push(fromCatalog(c, new Set(spec.alerts.map((a) => a.key))));
    });
    setCatalogOpen(false);
  }
  function deleteType(t: string) {
    if (!window.confirm(`Remove resource type "${draft[t]?.display || t}" and its alerts from the reference?`)) return;
    mutate((d) => {
      delete d[t];
    });
    if (selected === t) setSelected(Object.keys(draft).filter((x) => x !== t)[0] || "");
  }
  function addType(armType: string, display: string, category: string) {
    const t = armType.trim().toLowerCase();
    if (!t) return;
    if (draft[t]) {
      setSelected(t);
      setAddTypeOpen(false);
      return;
    }
    mutate((d) => {
      d[t] = { display: display.trim() || armType, category: category || "other", alerts: [] };
    });
    setSelected(t);
    setAddTypeOpen(false);
  }

  function validate(types: RefTypes): string | null {
    for (const [t, spec] of Object.entries(types)) {
      const keys = new Set<string>();
      for (const a of spec.alerts) {
        if (!a.key.trim() || !a.name.trim()) return `${spec.display || t}: every alert needs a key and a name.`;
        if (keys.has(a.key)) return `${spec.display || t}: duplicate alert key "${a.key}".`;
        keys.add(a.key);
        if (a.threshold != null && Number.isNaN(Number(a.threshold))) return `${spec.display || t} / ${a.name}: threshold must be a number or empty.`;
      }
    }
    return null;
  }

  async function save() {
    const err = validate(draft);
    if (err) {
      setMsg({ text: err, ok: false });
      return;
    }
    setBusy(true);
    try {
      await api.updateAmbaReference({ types: draft, reason: "Edited in rich editor" });
      await qc.invalidateQueries({ queryKey: ["amba-reference"] });
      await qc.invalidateQueries({ queryKey: ["amba-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Saved a new reference version.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }
  function discard() {
    if (ref) setDraft(JSON.parse(JSON.stringify(ref.types)));
    setDirty(false);
    setEditingKey(null);
    setMsg(null);
  }
  async function reset() {
    if (!window.confirm("Reset the AMBA reference set back to the built-in seed (as a new version)?")) return;
    setBusy(true);
    try {
      await api.resetAmbaReference();
      await qc.invalidateQueries({ queryKey: ["amba-reference"] });
      await qc.invalidateQueries({ queryKey: ["amba-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Reset to built-in seed.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }
  async function restore(id: string) {
    setBusy(true);
    try {
      await api.restoreAmbaReference(id);
      await qc.invalidateQueries({ queryKey: ["amba-reference"] });
      await qc.invalidateQueries({ queryKey: ["amba-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Restored revision as a new version.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy(false);
    }
  }
  function openRaw() {
    setRawText(JSON.stringify(draft, null, 2));
    setShowRaw(true);
  }
  function applyRaw() {
    try {
      const parsed = JSON.parse(rawText);
      const err = validate(parsed);
      if (err) {
        setMsg({ text: err, ok: false });
        return;
      }
      setDraft(parsed);
      setDirty(true);
      setShowRaw(false);
      setMsg({ text: "Applied raw JSON to the draft — review and Save.", ok: true });
    } catch {
      setMsg({ text: "Invalid JSON.", ok: false });
    }
  }

  const alertsByCat = useMemo(() => {
    const groups: Record<string, AmbaAlertRef[]> = { availability: [], performance: [], security: [] };
    for (const a of cur?.alerts ?? []) (groups[a.amba_category] || (groups[a.amba_category] = [])).push(a);
    return groups;
  }, [cur]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-50">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3 border-b bg-white px-5 py-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-gray-900">AMBA Reference Set — Editor</h1>
          <p className="text-xs text-gray-500">
            Resource-type → recommended baseline alerts. Versioned; everything saves as JSON behind the scenes.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="text-gray-500">v{ref?.version ?? 0} · {Object.keys(draft).length} types · {Object.values(draft).reduce((a, t) => a + t.alerts.length, 0)} alerts</span>
          {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-700">● Unsaved</span>}
          <button onClick={() => setShowHistory(true)} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">History</button>
          <button onClick={openRaw} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">Advanced: JSON</button>
          <button onClick={reset} disabled={busy} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50 disabled:opacity-50">Reset to built-in</button>
          {dirty && <button onClick={discard} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">Discard</button>}
          <button onClick={save} disabled={busy || !dirty} className="rounded-md bg-brand px-3 py-1.5 font-medium text-white hover:opacity-90 disabled:opacity-50">
            {busy ? "Saving…" : "Save new version"}
          </button>
        </div>
      </div>
      {msg && (
        <div className={`px-5 py-2 text-xs ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}

      {/* Two-pane body */}
      <div className="flex min-h-0 flex-1">
        {/* Left: type list */}
        <div className="flex w-72 shrink-0 flex-col border-r bg-white">
          <div className="border-b p-2">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter types…" className="w-full rounded border px-2 py-1.5 text-xs" />
            <button onClick={() => setAddTypeOpen(true)} className="mt-2 w-full rounded-md border bg-white px-2 py-1.5 text-xs font-medium hover:bg-gray-50">+ Add resource type</button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {typeList.map(([t, spec]) => {
              const used = usageByType[t] || 0;
              return (
                <button
                  key={t}
                  onClick={() => { setSelected(t); setEditingKey(null); }}
                  className={`flex w-full items-center gap-2 border-b px-3 py-2 text-left text-xs hover:bg-gray-50 ${selected === t ? "bg-blue-50" : ""}`}
                >
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[spec.alerts[0]?.amba_category || "availability"] || "#9ca3af" }} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-gray-800">{spec.display || t}</span>
                    <span className="block truncate font-mono text-[10px] text-gray-400">{t}</span>
                  </span>
                  {used > 0 && <span className="rounded bg-emerald-100 px-1 py-0.5 text-[10px] text-emerald-700" title={`${used} resource(s) in your workloads`}>{used}↗</span>}
                  <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{spec.alerts.length}</span>
                </button>
              );
            })}
            {typeList.length === 0 && <div className="p-4 text-center text-xs text-gray-400">No types match.</div>}
          </div>
        </div>

        {/* Right: alerts for the selected type */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {!cur ? (
            <div className="p-8 text-center text-sm text-gray-400">Select a resource type on the left.</div>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <div>
                  <input
                    value={cur.display}
                    onChange={(e) => mutate((d) => { if (d[selected]) d[selected].display = e.target.value; })}
                    className="rounded border px-2 py-1 text-sm font-semibold"
                  />
                  <div className="mt-0.5 font-mono text-[11px] text-gray-400">{selected}{usageByType[selected] ? ` · used by ${usageByType[selected]} resource(s) in your workloads` : ""}</div>
                </div>
                <div className="ml-auto flex gap-2">
                  <button onClick={() => setCatalogOpen(true)} className="rounded-md border bg-white px-2.5 py-1.5 text-xs hover:bg-gray-50">+ Add from catalog</button>
                  <button onClick={addBlankAlert} className="rounded-md border bg-white px-2.5 py-1.5 text-xs hover:bg-gray-50">+ Blank metric</button>
                  <button onClick={() => deleteType(selected)} className="rounded-md border border-red-200 bg-white px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50">Remove type</button>
                </div>
              </div>

              {cur.alerts.length === 0 && <div className="rounded border bg-white p-4 text-center text-xs text-gray-400">No alerts yet — add one from the catalog.</div>}

              {(["availability", "performance", "security"] as const).map((catg) =>
                alertsByCat[catg]?.length ? (
                  <div key={catg} className="mb-4">
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide" style={{ color: CATEGORY_COLOR[catg] }}>
                      <span className="inline-block h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[catg] }} />
                      {catg} ({alertsByCat[catg].length})
                    </div>
                    <div className="space-y-2">
                      {alertsByCat[catg].map((a) => (
                        <AlertCard
                          key={a.key}
                          alert={a}
                          editing={editingKey === a.key}
                          armType={selected}
                          onToggleEdit={() => setEditingKey(editingKey === a.key ? null : a.key)}
                          onChange={(patch) => updateAlert(a.key, patch)}
                          onDelete={() => { deleteAlert(a.key); if (editingKey === a.key) setEditingKey(null); }}
                          onDuplicate={() => duplicateAlert(a.key)}
                        />
                      ))}
                    </div>
                  </div>
                ) : null,
              )}
            </>
          )}
        </div>
      </div>

      {/* Add-from-catalog popover */}
      {catalogOpen && cur && (
        <Modal title={`Add metric · ${cur.display}`} onClose={() => setCatalogOpen(false)}>
          {catalogFor(selected).length === 0 ? (
            <p className="text-sm text-gray-500">No catalog metrics for this type — use "+ Blank metric" and enter a metric name.</p>
          ) : (
            <div className="space-y-1.5">
              {catalogFor(selected).map((c) => {
                const already = (cur.alerts || []).some((a) => a.metric === c.metric);
                return (
                  <button
                    key={c.metric}
                    onClick={() => addCatalogMetric(c)}
                    disabled={already}
                    className="flex w-full items-center gap-2 rounded border bg-white px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:opacity-40"
                  >
                    <span className="inline-block h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c.amba_category] }} />
                    <span className="font-medium text-gray-800">{c.label}</span>
                    <span className="font-mono text-[11px] text-gray-400">{c.metric}</span>
                    <span className="ml-auto text-[11px] text-gray-500">
                      {AMBA_OPERATOR_SYMBOL[c.operator]} {c.threshold != null ? `${c.threshold}${c.unit}` : "exists"}
                    </span>
                    {already && <span className="text-[10px] text-gray-400">added</span>}
                  </button>
                );
              })}
            </div>
          )}
        </Modal>
      )}

      {/* Add-resource-type popover */}
      {addTypeOpen && <AddTypeModal existing={draft} onClose={() => setAddTypeOpen(false)} onAdd={addType} />}

      {/* Raw JSON */}
      {showRaw && (
        <Modal title="Advanced — raw JSON (the types map)" onClose={() => setShowRaw(false)} wide>
          <textarea value={rawText} onChange={(e) => setRawText(e.target.value)} spellCheck={false} className="h-[60vh] w-full rounded border bg-gray-900 p-3 font-mono text-[11px] text-gray-100" />
          <div className="mt-2 flex justify-end gap-2">
            <button onClick={() => setShowRaw(false)} className="rounded-md border px-3 py-1.5 text-sm">Cancel</button>
            <button onClick={applyRaw} className="rounded-md bg-brand px-3 py-1.5 text-sm text-white">Apply to draft</button>
          </div>
        </Modal>
      )}

      {/* Version history */}
      {showHistory && (
        <Modal title="Version history" onClose={() => setShowHistory(false)}>
          <div className="space-y-1 text-xs">
            {(revsQ.data?.revisions ?? []).length === 0 && <p className="text-gray-400">No revisions yet.</p>}
            {(revsQ.data?.revisions ?? []).map((r) => (
              <div key={r.id} className="flex items-center gap-2 rounded border bg-white px-2 py-1.5">
                <span className="font-medium">v{r.version}</span>
                <span className="text-gray-500">{r.reason}</span>
                <span className="text-gray-400">{r.type_count} types · {r.alert_count} alerts</span>
                <span className="ml-auto text-gray-400">{r.by}</span>
                <button onClick={() => { void restore(r.id); setShowHistory(false); }} disabled={busy} className="rounded border px-2 py-0.5 hover:bg-gray-50 disabled:opacity-50">Restore</button>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}

function AlertCard({
  alert, editing, armType, onToggleEdit, onChange, onDelete, onDuplicate,
}: {
  alert: AmbaAlertRef;
  editing: boolean;
  armType: string;
  onToggleEdit: () => void;
  onChange: (patch: Partial<AmbaAlertRef>) => void;
  onDelete: () => void;
  onDuplicate: () => void;
}) {
  const metricOptions = catalogFor(armType);
  return (
    <div className={`rounded-lg border bg-white ${editing ? "ring-1 ring-brand" : ""}`}>
      <div className="flex items-start gap-2 px-3 py-2">
        <button onClick={onToggleEdit} className="min-w-0 flex-1 text-left">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-800">{alert.name}</span>
            <span className={`rounded border px-1.5 py-0.5 text-[10px] ${SEV_TONE[alert.severity] || ""}`}>{alert.severity}</span>
          </div>
          <div className="mt-0.5 text-[11px] text-gray-500">{sentence(alert)}</div>
          <Gauge alert={alert} />
        </button>
        <div className="flex shrink-0 gap-1">
          <button onClick={onToggleEdit} className="rounded border px-1.5 py-0.5 text-[11px] hover:bg-gray-50">{editing ? "Done" : "Edit"}</button>
          <button onClick={onDuplicate} className="rounded border px-1.5 py-0.5 text-[11px] hover:bg-gray-50" title="Duplicate">⧉</button>
          <button onClick={onDelete} className="rounded border border-red-200 px-1.5 py-0.5 text-[11px] text-red-600 hover:bg-red-50" title="Delete">✕</button>
        </div>
      </div>

      {editing && (
        <div className="grid grid-cols-2 gap-2 border-t bg-gray-50 p-3 text-xs sm:grid-cols-3">
          <label className="col-span-2 sm:col-span-3">
            <span className="text-gray-500">Name</span>
            <input value={alert.name} onChange={(e) => onChange({ name: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1" />
          </label>
          <label>
            <span className="text-gray-500">Signal</span>
            <select value={alert.signal} onChange={(e) => onChange({ signal: e.target.value as AmbaAlertRef["signal"] })} className="mt-0.5 w-full rounded border px-2 py-1">
              <option value="metric">metric</option>
              <option value="log">log</option>
            </select>
          </label>
          <label className="col-span-2">
            <span className="text-gray-500">Metric</span>
            <input
              list={`metriclist-${armType}`}
              value={alert.metric}
              onChange={(e) => {
                const hit = metricOptions.find((m) => m.metric === e.target.value);
                if (hit) {
                  onChange({ metric: hit.metric, unit: hit.unit, operator: hit.operator, window: hit.window, threshold: hit.threshold, amba_category: hit.amba_category, name: alert.name === "New alert" ? hit.label : alert.name });
                } else {
                  onChange({ metric: e.target.value });
                }
              }}
              className="mt-0.5 w-full rounded border px-2 py-1 font-mono"
            />
            <datalist id={`metriclist-${armType}`}>
              {metricOptions.map((m) => <option key={m.metric} value={m.metric}>{m.label}</option>)}
            </datalist>
          </label>
          <label>
            <span className="text-gray-500">Operator</span>
            <select value={alert.operator} onChange={(e) => onChange({ operator: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1">
              {AMBA_OPERATORS.map((o) => <option key={o} value={o}>{AMBA_OPERATOR_SYMBOL[o]} {o}</option>)}
            </select>
          </label>
          <label>
            <span className="text-gray-500">Threshold</span>
            <input
              type="number"
              value={alert.threshold ?? ""}
              onChange={(e) => onChange({ threshold: e.target.value === "" ? null : Number(e.target.value) })}
              placeholder="(exists)"
              className="mt-0.5 w-full rounded border px-2 py-1"
            />
          </label>
          <label>
            <span className="text-gray-500">Unit</span>
            <input list="amba-units" value={alert.unit} onChange={(e) => onChange({ unit: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1" />
            <datalist id="amba-units">{AMBA_UNITS.map((u) => <option key={u} value={u} />)}</datalist>
          </label>
          <label>
            <span className="text-gray-500">Window</span>
            <select value={alert.window} onChange={(e) => onChange({ window: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1">
              {AMBA_WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
            </select>
          </label>
          <label>
            <span className="text-gray-500">Aggregation</span>
            <select value={alert.aggregation || ""} onChange={(e) => onChange({ aggregation: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1">
              <option value="">automatic</option>
              {['Average', 'Minimum', 'Maximum', 'Total', 'Count'].map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label>
            <span className="text-gray-500">Category</span>
            <select value={alert.amba_category} onChange={(e) => onChange({ amba_category: e.target.value as AmbaAlertRef["amba_category"] })} className="mt-0.5 w-full rounded border px-2 py-1">
              {AMBA_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <div className="col-span-2 sm:col-span-3">
            <span className="text-gray-500">Severity</span>
            <div className="mt-0.5 inline-flex overflow-hidden rounded border">
              {AMBA_SEVERITIES.map((s) => (
                <button key={s} onClick={() => onChange({ severity: s })} className={`px-2.5 py-1 text-[11px] ${alert.severity === s ? "bg-gray-900 text-white" : "bg-white hover:bg-gray-50"}`}>{s}</button>
              ))}
            </div>
          </div>
          <label className="col-span-2 flex items-center gap-2 sm:col-span-3">
            <input type="checkbox" checked={alert.requires_action_group} onChange={(e) => onChange({ requires_action_group: e.target.checked })} />
            <span className="text-gray-600">Requires a wired action group to count as "present"</span>
          </label>
          <label className="col-span-2 flex items-center gap-2 sm:col-span-3">
            <input type="checkbox" checked={alert.deployable !== false} onChange={(e) => onChange({ deployable: e.target.checked })} />
            <span className="text-gray-600">Deployable as a native Azure Monitor alert</span>
          </label>
          <label className="col-span-2 sm:col-span-3">
            <span className="text-gray-500">Dimension filter</span>
            <input value={alert.dimension_filter || ""} onChange={(e) => onChange({ dimension_filter: e.target.value })} placeholder="StatusCode eq '429'" className="mt-0.5 w-full rounded border px-2 py-1 font-mono" />
          </label>
          <label className="col-span-2 sm:col-span-3">
            <span className="text-gray-500">Why it matters</span>
            <textarea value={alert.why} onChange={(e) => onChange({ why: e.target.value })} rows={2} className="mt-0.5 w-full rounded border px-2 py-1" />
          </label>
        </div>
      )}
    </div>
  );
}

function AddTypeModal({ existing, onClose, onAdd }: { existing: RefTypes; onClose: () => void; onAdd: (t: string, d: string, c: string) => void }) {
  const [armType, setArmType] = useState("");
  const [display, setDisplay] = useState("");
  const [category, setCategory] = useState("other");
  const known = KNOWN_ARM_TYPES.filter((k) => !existing[k.type]);
  return (
    <Modal title="Add resource type" onClose={onClose}>
      <div className="mb-3">
        <div className="mb-1 text-xs font-medium text-gray-500">Pick a known type</div>
        <div className="max-h-48 space-y-1 overflow-auto">
          {known.map((k) => (
            <button key={k.type} onClick={() => onAdd(k.type, k.label, k.category)} className="flex w-full items-center gap-2 rounded border bg-white px-2 py-1.5 text-left text-xs hover:bg-gray-50">
              <span className="font-medium text-gray-800">{k.label}</span>
              <span className="font-mono text-[10px] text-gray-400">{k.type}</span>
            </button>
          ))}
          {known.length === 0 && <p className="text-xs text-gray-400">All known types are already in the reference.</p>}
        </div>
      </div>
      <div className="border-t pt-3">
        <div className="mb-1 text-xs font-medium text-gray-500">…or a custom ARM type</div>
        <input value={armType} onChange={(e) => setArmType(e.target.value)} placeholder="microsoft.provider/resourcetype" className="mb-1 w-full rounded border px-2 py-1.5 font-mono text-xs" />
        <input value={display} onChange={(e) => setDisplay(e.target.value)} placeholder="Display name" className="mb-1 w-full rounded border px-2 py-1.5 text-xs" />
        <input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="category (e.g. data)" className="mb-2 w-full rounded border px-2 py-1.5 text-xs" />
        <button onClick={() => onAdd(armType, display, category)} disabled={!armType.trim()} className="rounded-md bg-brand px-3 py-1.5 text-xs text-white disabled:opacity-50">Add type</button>
      </div>
    </Modal>
  );
}

function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6" onClick={onClose}>
      <div className={`flex max-h-[85vh] w-full ${wide ? "max-w-3xl" : "max-w-md"} flex-col rounded-lg bg-white shadow-xl`} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-2.5">
          <h3 className="text-sm font-semibold">{title}</h3>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>
        <div className="overflow-auto p-4">{children}</div>
      </div>
    </div>
  );
}
