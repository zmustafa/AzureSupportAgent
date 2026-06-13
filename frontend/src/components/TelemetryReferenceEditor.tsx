import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type TelemetryCategory, type TelemetryReference } from "../api";
import { formatError } from "../utils/format";
import {
  KNOWN_ARM_TYPES,
  TELEMETRY_GROUP_COLOR,
  TELEMETRY_GROUPS,
  TELEMETRY_KINDS,
  telemetryCatalogFor,
  type TelemetryCatalogCategory,
} from "./referenceCatalogs";

type RefTypes = TelemetryReference["types"];

function uniqueKey(base: string, existing: Set<string>): string {
  let k = base;
  let i = 2;
  while (existing.has(k)) k = `${base}${i++}`;
  return k;
}

function blankCat(existing: Set<string>): TelemetryCategory {
  return { key: uniqueKey("NewCategory", existing), name: "New category", kind: "log", group: "operational", recommended: true, why: "" };
}
function fromCatalog(c: TelemetryCatalogCategory, existing: Set<string>): TelemetryCategory {
  return { key: existing.has(c.key) ? uniqueKey(c.key, existing) : c.key, name: c.name, kind: c.kind, group: c.group, recommended: true, why: c.why || "" };
}

export function TelemetryReferenceEditor() {
  const qc = useQueryClient();
  const refQ = useQuery({ queryKey: ["telemetry-reference"], queryFn: api.telemetryReference });
  const revsQ = useQuery({ queryKey: ["telemetry-reference-revisions"], queryFn: api.telemetryReferenceRevisions });
  const wsQ = useQuery({ queryKey: ["telemetry-workspaces"], queryFn: api.telemetryWorkspaces });
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });

  const [draft, setDraft] = useState<RefTypes>({});
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [rawText, setRawText] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [addTypeOpen, setAddTypeOpen] = useState(false);
  const [wsOpen, setWsOpen] = useState(false);
  const [approvedDraft, setApprovedDraft] = useState("");

  const ref = refQ.data;

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

  function updateCat(key: string, patch: Partial<TelemetryCategory>) {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.categories = spec.categories.map((c) => (c.key === key ? { ...c, ...patch } : c));
    });
  }
  function deleteCat(key: string) {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.categories = spec.categories.filter((c) => c.key !== key);
    });
  }
  function duplicateCat(key: string) {
    mutate((d) => {
      const spec = d[selected];
      if (!spec) return;
      const c = spec.categories.find((x) => x.key === key);
      if (!c) return;
      const keys = new Set(spec.categories.map((x) => x.key));
      spec.categories.push({ ...c, key: uniqueKey(`${c.key}Copy`, keys), name: `${c.name} (copy)` });
    });
  }
  function addBlank() {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.categories.push(blankCat(new Set(spec.categories.map((c) => c.key))));
    });
  }
  function addCatalog(c: TelemetryCatalogCategory) {
    mutate((d) => {
      const spec = d[selected];
      if (spec) spec.categories.push(fromCatalog(c, new Set(spec.categories.map((x) => x.key))));
    });
    setCatalogOpen(false);
  }
  function deleteType(t: string) {
    if (!window.confirm(`Remove resource type "${draft[t]?.display || t}"?`)) return;
    mutate((d) => { delete d[t]; });
    if (selected === t) setSelected(Object.keys(draft).filter((x) => x !== t)[0] || "");
  }
  function addType(armType: string, display: string) {
    const t = armType.trim().toLowerCase();
    if (!t) return;
    if (draft[t]) { setSelected(t); setAddTypeOpen(false); return; }
    mutate((d) => { d[t] = { display: display.trim() || armType, note: "", categories: [] }; });
    setSelected(t);
    setAddTypeOpen(false);
  }

  function validate(types: RefTypes): string | null {
    for (const [t, spec] of Object.entries(types)) {
      const keys = new Set<string>();
      for (const c of spec.categories) {
        if (!c.key.trim()) return `${spec.display || t}: every category needs a key.`;
        if (keys.has(c.key)) return `${spec.display || t}: duplicate category key "${c.key}".`;
        keys.add(c.key);
      }
    }
    return null;
  }

  async function save() {
    const err = validate(draft);
    if (err) { setMsg({ text: err, ok: false }); return; }
    setBusy(true);
    try {
      await api.updateTelemetryReference({ types: draft, reason: "Edited in rich editor" });
      await qc.invalidateQueries({ queryKey: ["telemetry-reference"] });
      await qc.invalidateQueries({ queryKey: ["telemetry-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Saved a new reference version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  function discard() { if (ref) setDraft(JSON.parse(JSON.stringify(ref.types))); setDirty(false); setEditingKey(null); setMsg(null); }
  async function reset() {
    if (!window.confirm("Reset the telemetry reference set to the built-in seed (as a new version)?")) return;
    setBusy(true);
    try {
      await api.resetTelemetryReference();
      await qc.invalidateQueries({ queryKey: ["telemetry-reference"] });
      await qc.invalidateQueries({ queryKey: ["telemetry-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Reset to built-in seed.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  async function restore(id: string) {
    setBusy(true);
    try {
      await api.restoreTelemetryReference(id);
      await qc.invalidateQueries({ queryKey: ["telemetry-reference"] });
      await qc.invalidateQueries({ queryKey: ["telemetry-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Restored revision as a new version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  function openRaw() { setRawText(JSON.stringify(draft, null, 2)); setShowRaw(true); }
  function applyRaw() {
    try {
      const parsed = JSON.parse(rawText);
      const err = validate(parsed);
      if (err) { setMsg({ text: err, ok: false }); return; }
      setDraft(parsed); setDirty(true); setShowRaw(false);
      setMsg({ text: "Applied raw JSON to the draft — review and Save.", ok: true });
    } catch { setMsg({ text: "Invalid JSON.", ok: false }); }
  }
  function openWs() { setApprovedDraft((wsQ.data?.approved ?? []).join("\n")); setWsOpen(true); }
  async function saveWs() {
    const list = approvedDraft.split("\n").map((s) => s.trim()).filter(Boolean);
    setBusy(true);
    try {
      await api.setTelemetryApprovedWorkspaces(list);
      await qc.invalidateQueries({ queryKey: ["telemetry-workspaces"] });
      setWsOpen(false);
      setMsg({ text: "Saved approved workspaces.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }

  const catsByGroup = useMemo(() => {
    const g: Record<string, TelemetryCategory[]> = { audit: [], security: [], operational: [], performance: [] };
    for (const c of cur?.categories ?? []) (g[c.group] || (g[c.group] = [])).push(c);
    return g;
  }, [cur]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-50">
      <div className="flex flex-wrap items-center gap-3 border-b bg-white px-5 py-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-gray-900">Telemetry Reference Set — Editor</h1>
          <p className="text-xs text-gray-500">Resource-type → recommended diagnostic-setting categories used to compute Telemetry Coverage. Versioned; saves as JSON.</p>
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="text-gray-500">v{ref?.version ?? 0} · {Object.keys(draft).length} types · {Object.values(draft).reduce((a, t) => a + t.categories.length, 0)} categories</span>
          {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-700">● Unsaved</span>}
          <button onClick={openWs} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">Approved workspaces</button>
          <button onClick={() => setShowHistory(true)} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">History</button>
          <button onClick={openRaw} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">Advanced: JSON</button>
          <button onClick={reset} disabled={busy} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50 disabled:opacity-50">Reset to built-in</button>
          {dirty && <button onClick={discard} className="rounded-md border bg-white px-2.5 py-1.5 hover:bg-gray-50">Discard</button>}
          <button onClick={save} disabled={busy || !dirty} className="rounded-md bg-brand px-3 py-1.5 font-medium text-white hover:opacity-90 disabled:opacity-50">{busy ? "Saving…" : "Save new version"}</button>
        </div>
      </div>
      {msg && <div className={`px-5 py-2 text-xs ${msg.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{msg.text}</div>}

      <div className="flex min-h-0 flex-1">
        {/* Left */}
        <div className="flex w-72 shrink-0 flex-col border-r bg-white">
          <div className="border-b p-2">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter types…" className="w-full rounded border px-2 py-1.5 text-xs" />
            <button onClick={() => setAddTypeOpen(true)} className="mt-2 w-full rounded-md border bg-white px-2 py-1.5 text-xs font-medium hover:bg-gray-50">+ Add resource type</button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {typeList.map(([t, spec]) => {
              const used = usageByType[t] || 0;
              const audit = spec.categories.some((c) => c.group === "audit" || c.group === "security");
              return (
                <button key={t} onClick={() => { setSelected(t); setEditingKey(null); }} className={`flex w-full items-center gap-2 border-b px-3 py-2 text-left text-xs hover:bg-gray-50 ${selected === t ? "bg-blue-50" : ""}`}>
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: audit ? TELEMETRY_GROUP_COLOR.security : TELEMETRY_GROUP_COLOR.operational }} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-gray-800">{spec.display || t}</span>
                    <span className="block truncate font-mono text-[10px] text-gray-400">{t}</span>
                  </span>
                  {used > 0 && <span className="rounded bg-emerald-100 px-1 py-0.5 text-[10px] text-emerald-700" title={`${used} resource(s) in your workloads`}>{used}↗</span>}
                  <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{spec.categories.length}</span>
                </button>
              );
            })}
            {typeList.length === 0 && <div className="p-4 text-center text-xs text-gray-400">No types match.</div>}
          </div>
        </div>

        {/* Right */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {!cur ? (
            <div className="p-8 text-center text-sm text-gray-400">Select a resource type on the left.</div>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <div className="min-w-0">
                  <input value={cur.display} onChange={(e) => mutate((d) => { if (d[selected]) d[selected].display = e.target.value; })} className="rounded border px-2 py-1 text-sm font-semibold" />
                  <div className="mt-0.5 font-mono text-[11px] text-gray-400">{selected}{usageByType[selected] ? ` · used by ${usageByType[selected]} resource(s)` : ""}</div>
                </div>
                <div className="ml-auto flex gap-2">
                  <button onClick={() => setCatalogOpen(true)} className="rounded-md border bg-white px-2.5 py-1.5 text-xs hover:bg-gray-50">+ Add from catalog</button>
                  <button onClick={addBlank} className="rounded-md border bg-white px-2.5 py-1.5 text-xs hover:bg-gray-50">+ Blank category</button>
                  <button onClick={() => deleteType(selected)} className="rounded-md border border-red-200 bg-white px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50">Remove type</button>
                </div>
              </div>
              <input value={cur.note} onChange={(e) => mutate((d) => { if (d[selected]) d[selected].note = e.target.value; })} placeholder="Note / guidance shown in the UI" className="mb-3 w-full rounded border px-2 py-1.5 text-xs" />

              {cur.categories.length === 0 && <div className="rounded border bg-white p-4 text-center text-xs text-gray-400">No categories — add one from the catalog.</div>}

              {TELEMETRY_GROUPS.map((grp) =>
                catsByGroup[grp]?.length ? (
                  <div key={grp} className="mb-4">
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide" style={{ color: TELEMETRY_GROUP_COLOR[grp] }}>
                      <span className="inline-block h-2 w-2 rounded-full" style={{ background: TELEMETRY_GROUP_COLOR[grp] }} />
                      {grp} ({catsByGroup[grp].length}){(grp === "audit" || grp === "security") ? " · high-importance" : ""}
                    </div>
                    <div className="space-y-2">
                      {catsByGroup[grp].map((c) => (
                        <CatCard key={c.key} cat={c} editing={editingKey === c.key}
                          onToggleEdit={() => setEditingKey(editingKey === c.key ? null : c.key)}
                          onChange={(p) => updateCat(c.key, p)} onDelete={() => { deleteCat(c.key); if (editingKey === c.key) setEditingKey(null); }} onDuplicate={() => duplicateCat(c.key)} />
                      ))}
                    </div>
                  </div>
                ) : null,
              )}
            </>
          )}
        </div>
      </div>

      {catalogOpen && cur && (
        <Modal title={`Add category · ${cur.display}`} onClose={() => setCatalogOpen(false)}>
          {telemetryCatalogFor(selected).length === 0 ? (
            <p className="text-sm text-gray-500">No catalog categories for this type — use "+ Blank category".</p>
          ) : (
            <div className="space-y-1.5">
              {telemetryCatalogFor(selected).map((c) => {
                const already = (cur.categories || []).some((x) => x.key === c.key);
                return (
                  <button key={c.key} onClick={() => addCatalog(c)} disabled={already} className="flex w-full items-center gap-2 rounded border bg-white px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:opacity-40">
                    <span className="inline-block h-2 w-2 rounded-full" style={{ background: TELEMETRY_GROUP_COLOR[c.group] }} />
                    <span className="font-medium text-gray-800">{c.name}</span>
                    <span className="font-mono text-[11px] text-gray-400">{c.key}</span>
                    <span className="ml-auto text-[11px] text-gray-500">{c.group} · {c.kind}</span>
                    {already && <span className="text-[10px] text-gray-400">added</span>}
                  </button>
                );
              })}
            </div>
          )}
        </Modal>
      )}

      {addTypeOpen && <AddTypeModal existing={draft} onClose={() => setAddTypeOpen(false)} onAdd={addType} />}

      {wsOpen && (
        <Modal title="Approved Log Analytics Workspaces" onClose={() => setWsOpen(false)}>
          <p className="mb-2 text-xs text-gray-500">Diagnostic settings shipping to a workspace not on this list are flagged as destination <b>drift</b>. One workspace resource id per line; empty = drift detection off.</p>
          <textarea value={approvedDraft} onChange={(e) => setApprovedDraft(e.target.value)} spellCheck={false} className="h-40 w-full rounded border p-2 font-mono text-[11px]" placeholder="/subscriptions/.../workspaces/prod-law" />
          {(wsQ.data?.workspaces ?? []).length > 0 && (
            <details className="mt-2 text-xs"><summary className="cursor-pointer text-gray-500">Discovered workspaces ({wsQ.data?.workspaces.length})</summary>
              <div className="mt-1 space-y-1">{(wsQ.data?.workspaces ?? []).map((w) => <div key={w.id} className="truncate text-[10px] text-gray-500" title={w.id}>{w.name} — {w.resourceGroup}</div>)}</div>
            </details>
          )}
          <div className="mt-2 flex justify-end gap-2">
            <button onClick={() => setWsOpen(false)} className="rounded-md border px-3 py-1.5 text-sm">Cancel</button>
            <button onClick={() => void saveWs()} disabled={busy} className="rounded-md bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50">Save</button>
          </div>
        </Modal>
      )}

      {showRaw && (
        <Modal title="Advanced — raw JSON (the types map)" onClose={() => setShowRaw(false)} wide>
          <textarea value={rawText} onChange={(e) => setRawText(e.target.value)} spellCheck={false} className="h-[60vh] w-full rounded border bg-gray-900 p-3 font-mono text-[11px] text-gray-100" />
          <div className="mt-2 flex justify-end gap-2">
            <button onClick={() => setShowRaw(false)} className="rounded-md border px-3 py-1.5 text-sm">Cancel</button>
            <button onClick={applyRaw} className="rounded-md bg-brand px-3 py-1.5 text-sm text-white">Apply to draft</button>
          </div>
        </Modal>
      )}

      {showHistory && (
        <Modal title="Version history" onClose={() => setShowHistory(false)}>
          <div className="space-y-1 text-xs">
            {(revsQ.data?.revisions ?? []).length === 0 && <p className="text-gray-400">No revisions yet.</p>}
            {(revsQ.data?.revisions ?? []).map((r) => (
              <div key={r.id} className="flex items-center gap-2 rounded border bg-white px-2 py-1.5">
                <span className="font-medium">v{r.version}</span><span className="text-gray-500">{r.reason}</span>
                <span className="text-gray-400">{r.type_count} types · {r.category_count} categories</span>
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

function CatCard({ cat, editing, onToggleEdit, onChange, onDelete, onDuplicate }: {
  cat: TelemetryCategory; editing: boolean; onToggleEdit: () => void;
  onChange: (p: Partial<TelemetryCategory>) => void; onDelete: () => void; onDuplicate: () => void;
}) {
  return (
    <div className={`rounded-lg border bg-white ${editing ? "ring-1 ring-brand" : ""} ${cat.recommended ? "" : "opacity-60"}`}>
      <div className="flex items-start gap-2 px-3 py-2">
        <button onClick={onToggleEdit} className="min-w-0 flex-1 text-left">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-800">{cat.name}</span>
            <span className="rounded border bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-500">{cat.kind}</span>
            {!cat.recommended && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">optional</span>}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-gray-400">{cat.key}</div>
          {cat.why && <div className="mt-0.5 text-[11px] text-gray-500">{cat.why}</div>}
        </button>
        <div className="flex shrink-0 gap-1">
          <button onClick={onToggleEdit} className="rounded border px-1.5 py-0.5 text-[11px] hover:bg-gray-50">{editing ? "Done" : "Edit"}</button>
          <button onClick={onDuplicate} className="rounded border px-1.5 py-0.5 text-[11px] hover:bg-gray-50" title="Duplicate">⧉</button>
          <button onClick={onDelete} className="rounded border border-red-200 px-1.5 py-0.5 text-[11px] text-red-600 hover:bg-red-50" title="Delete">✕</button>
        </div>
      </div>
      {editing && (
        <div className="grid grid-cols-2 gap-2 border-t bg-gray-50 p-3 text-xs sm:grid-cols-3">
          <label className="col-span-2 sm:col-span-3"><span className="text-gray-500">Name</span>
            <input value={cat.name} onChange={(e) => onChange({ name: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1" /></label>
          <label className="col-span-2"><span className="text-gray-500">Category key (Azure diagnostic category)</span>
            <input value={cat.key} onChange={(e) => onChange({ key: e.target.value })} className="mt-0.5 w-full rounded border px-2 py-1 font-mono" /></label>
          <label><span className="text-gray-500">Kind</span>
            <select value={cat.kind} onChange={(e) => onChange({ kind: e.target.value as TelemetryCategory["kind"] })} className="mt-0.5 w-full rounded border px-2 py-1">
              {TELEMETRY_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select></label>
          <label className="col-span-2 sm:col-span-1"><span className="text-gray-500">Group</span>
            <select value={cat.group} onChange={(e) => onChange({ group: e.target.value as TelemetryCategory["group"] })} className="mt-0.5 w-full rounded border px-2 py-1">
              {TELEMETRY_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select></label>
          <label className="col-span-2 flex items-center gap-2 sm:col-span-3">
            <input type="checkbox" checked={cat.recommended} onChange={(e) => onChange({ recommended: e.target.checked })} />
            <span className="text-gray-600">Recommended (counts toward coverage; audit/security groups flag amber when missing)</span></label>
          <label className="col-span-2 sm:col-span-3"><span className="text-gray-500">Why it matters</span>
            <textarea value={cat.why} onChange={(e) => onChange({ why: e.target.value })} rows={2} className="mt-0.5 w-full rounded border px-2 py-1" /></label>
        </div>
      )}
    </div>
  );
}

function AddTypeModal({ existing, onClose, onAdd }: { existing: RefTypes; onClose: () => void; onAdd: (t: string, d: string) => void }) {
  const [armType, setArmType] = useState("");
  const [display, setDisplay] = useState("");
  const known = KNOWN_ARM_TYPES.filter((k) => !existing[k.type]);
  return (
    <Modal title="Add resource type" onClose={onClose}>
      <div className="mb-3">
        <div className="mb-1 text-xs font-medium text-gray-500">Pick a known type</div>
        <div className="max-h-48 space-y-1 overflow-auto">
          {known.map((k) => (
            <button key={k.type} onClick={() => onAdd(k.type, k.label)} className="flex w-full items-center gap-2 rounded border bg-white px-2 py-1.5 text-left text-xs hover:bg-gray-50">
              <span className="font-medium text-gray-800">{k.label}</span><span className="font-mono text-[10px] text-gray-400">{k.type}</span>
            </button>
          ))}
          {known.length === 0 && <p className="text-xs text-gray-400">All known types are already present.</p>}
        </div>
      </div>
      <div className="border-t pt-3">
        <div className="mb-1 text-xs font-medium text-gray-500">…or a custom ARM type</div>
        <input value={armType} onChange={(e) => setArmType(e.target.value)} placeholder="microsoft.provider/resourcetype" className="mb-1 w-full rounded border px-2 py-1.5 font-mono text-xs" />
        <input value={display} onChange={(e) => setDisplay(e.target.value)} placeholder="Display name" className="mb-2 w-full rounded border px-2 py-1.5 text-xs" />
        <button onClick={() => onAdd(armType, display)} disabled={!armType.trim()} className="rounded-md bg-brand px-3 py-1.5 text-xs text-white disabled:opacity-50">Add type</button>
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
