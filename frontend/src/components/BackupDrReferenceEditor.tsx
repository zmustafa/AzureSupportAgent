import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BackupDrReference } from "../api";
import { formatError } from "../utils/format";
import { BACKUPDR_CHECKS, KNOWN_ARM_TYPES } from "./referenceCatalogs";

type RefTypes = BackupDrReference["types"];

const PILLAR_COLOR: Record<string, string> = {
  compute: "#7c3aed",
  data: "#dc2626",
  storage: "#ea580c",
  containers: "#0891b2",
  integration: "#16a34a",
  security: "#b91c1c",
  other: "#6b7280",
};

export function BackupDrReferenceEditor() {
  const qc = useQueryClient();
  const refQ = useQuery({ queryKey: ["backupdr-reference"], queryFn: api.backupDrReference });
  const revsQ = useQuery({ queryKey: ["backupdr-reference-revisions"], queryFn: api.backupDrReferenceRevisions });
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });

  const [draft, setDraft] = useState<RefTypes>({});
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [rawText, setRawText] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [addTypeOpen, setAddTypeOpen] = useState(false);

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

  function toggleCheck(key: string) {
    mutate((d) => {
      const spec = d[selected];
      if (!spec) return;
      const set = new Set(spec.checks || []);
      if (set.has(key)) set.delete(key);
      else set.add(key);
      // Preserve the canonical check order.
      spec.checks = BACKUPDR_CHECKS.filter((c) => set.has(c.key)).map((c) => c.key);
    });
  }
  function deleteType(t: string) {
    if (!window.confirm(`Remove resource type "${draft[t]?.display || t}"?`)) return;
    mutate((d) => { delete d[t]; });
    if (selected === t) setSelected(Object.keys(draft).filter((x) => x !== t)[0] || "");
  }
  function addType(armType: string, display: string, category: string) {
    const t = armType.trim().toLowerCase();
    if (!t) return;
    if (draft[t]) { setSelected(t); setAddTypeOpen(false); return; }
    mutate((d) => { d[t] = { display: display.trim() || armType, category: category || "other", note: "", checks: [] }; });
    setSelected(t);
    setAddTypeOpen(false);
  }

  async function save() {
    setBusy(true);
    try {
      await api.updateBackupDrReference({ types: draft, reason: "Edited in rich editor" });
      await qc.invalidateQueries({ queryKey: ["backupdr-reference"] });
      await qc.invalidateQueries({ queryKey: ["backupdr-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Saved a new reference version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  function discard() { if (ref) setDraft(JSON.parse(JSON.stringify(ref.types))); setDirty(false); setMsg(null); }
  async function reset() {
    if (!window.confirm("Reset the backup/DR reference set to the built-in seed (as a new version)?")) return;
    setBusy(true);
    try {
      await api.resetBackupDrReference();
      await qc.invalidateQueries({ queryKey: ["backupdr-reference"] });
      await qc.invalidateQueries({ queryKey: ["backupdr-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Reset to built-in seed.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  async function restore(id: string) {
    setBusy(true);
    try {
      await api.restoreBackupDrReference(id);
      await qc.invalidateQueries({ queryKey: ["backupdr-reference"] });
      await qc.invalidateQueries({ queryKey: ["backupdr-reference-revisions"] });
      setDirty(false);
      setMsg({ text: "Restored revision as a new version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }
  function openRaw() { setRawText(JSON.stringify(draft, null, 2)); setShowRaw(true); }
  function applyRaw() {
    try {
      const parsed = JSON.parse(rawText);
      setDraft(parsed); setDirty(true); setShowRaw(false);
      setMsg({ text: "Applied raw JSON to the draft — review and Save.", ok: true });
    } catch { setMsg({ text: "Invalid JSON.", ok: false }); }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-50">
      <div className="flex flex-wrap items-center gap-3 border-b bg-white px-5 py-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-gray-900">Backup / DR Reference Set — Editor</h1>
          <p className="text-xs text-gray-500">Resource-type → which backup/DR protection checks apply (the matrix columns). Versioned; saves as JSON.</p>
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="text-gray-500">v{ref?.version ?? 0} · {Object.keys(draft).length} types · {Object.values(draft).reduce((a, t) => a + (t.checks?.length ?? 0), 0)} checks</span>
          {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-700">● Unsaved</span>}
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
              return (
                <button key={t} onClick={() => setSelected(t)} className={`flex w-full items-center gap-2 border-b px-3 py-2 text-left text-xs hover:bg-gray-50 ${selected === t ? "bg-blue-50" : ""}`}>
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: PILLAR_COLOR[spec.category] || PILLAR_COLOR.other }} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-gray-800">{spec.display || t}</span>
                    <span className="block truncate font-mono text-[10px] text-gray-400">{t}</span>
                  </span>
                  {used > 0 && <span className="rounded bg-emerald-100 px-1 py-0.5 text-[10px] text-emerald-700" title={`${used} resource(s) in your workloads`}>{used}↗</span>}
                  <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{spec.checks?.length ?? 0}</span>
                </button>
              );
            })}
            {typeList.length === 0 && <div className="p-4 text-center text-xs text-gray-400">No types match.</div>}
          </div>
        </div>

        {/* Right: the checklist */}
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
                <input value={cur.category} onChange={(e) => mutate((d) => { if (d[selected]) d[selected].category = e.target.value; })} placeholder="category" className="w-28 rounded border px-2 py-1 text-xs" />
                <div className="ml-auto">
                  <button onClick={() => deleteType(selected)} className="rounded-md border border-red-200 bg-white px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50">Remove type</button>
                </div>
              </div>
              <input value={cur.note} onChange={(e) => mutate((d) => { if (d[selected]) d[selected].note = e.target.value; })} placeholder="Note / guidance shown in the UI" className="mb-3 w-full rounded border px-2 py-1.5 text-xs" />

              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Applicable protection checks ({cur.checks.length} of {BACKUPDR_CHECKS.length} selected)
              </div>
              <div className="space-y-1.5">
                {BACKUPDR_CHECKS.map((c) => {
                  const on = (cur.checks || []).includes(c.key);
                  return (
                    <label key={c.key} className={`flex cursor-pointer items-start gap-3 rounded-lg border bg-white p-3 ${on ? "border-brand/40 bg-brand/5" : ""}`}>
                      <input type="checkbox" checked={on} onChange={() => toggleCheck(c.key)} className="mt-0.5" />
                      <span className="min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="text-sm font-medium text-gray-800">{c.label}</span>
                          <span className="font-mono text-[10px] text-gray-400">{c.key}</span>
                        </span>
                        <span className="mt-0.5 block text-[11px] text-gray-500">{c.why}</span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>

      {addTypeOpen && <AddTypeModal existing={draft} onClose={() => setAddTypeOpen(false)} onAdd={addType} />}

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
                <span className="text-gray-400">{r.type_count} types · {r.check_count} checks</span>
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

function AddTypeModal({ existing, onClose, onAdd }: { existing: RefTypes; onClose: () => void; onAdd: (t: string, d: string, c: string) => void }) {
  const [armType, setArmType] = useState("");
  const [display, setDisplay] = useState("");
  const [category, setCategory] = useState("data");
  const known = KNOWN_ARM_TYPES.filter((k) => !existing[k.type]);
  return (
    <Modal title="Add resource type" onClose={onClose}>
      <div className="mb-3">
        <div className="mb-1 text-xs font-medium text-gray-500">Pick a known type</div>
        <div className="max-h-48 space-y-1 overflow-auto">
          {known.map((k) => (
            <button key={k.type} onClick={() => onAdd(k.type, k.label, k.category)} className="flex w-full items-center gap-2 rounded border bg-white px-2 py-1.5 text-left text-xs hover:bg-gray-50">
              <span className="font-medium text-gray-800">{k.label}</span><span className="font-mono text-[10px] text-gray-400">{k.type}</span>
            </button>
          ))}
          {known.length === 0 && <p className="text-xs text-gray-400">All known types are already present.</p>}
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
