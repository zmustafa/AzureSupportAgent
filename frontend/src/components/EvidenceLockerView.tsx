import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type EvidenceDiff, type EvidenceSnapshot } from "../api";
import { formatError, formatTimestamp } from "../utils/format";

const INCLUDE_OPTIONS: { key: string; label: string }[] = [
  { key: "inventory", label: "Inventory" },
  { key: "properties", label: "Full properties" },
  { key: "changes", label: "Recent changes" },
  { key: "metrics", label: "Key metrics window" },
  { key: "findings", label: "Active findings" },
  { key: "architecture", label: "Architecture revision" },
  { key: "memory", label: "Memory revision" },
  { key: "activity", label: "Activity feed" },
];

const CONTENT_TABS = ["inventory", "properties", "changes", "metrics", "findings", "architecture", "memory", "activity"];

function shortSha(s: string) { return s ? `${s.slice(0, 12)}…` : ""; }

// ----------------------------------------------------------------- Creator modal
function CreatorModal({ onClose, onCreated, presetScope }: {
  onClose: () => void;
  onCreated: () => void;
  presetScope?: { kind: string; id: string };
}) {
  const [name, setName] = useState("");
  const [scopeKind, setScopeKind] = useState(presetScope?.kind || "workload");
  const [scopeId, setScopeId] = useState(presetScope?.id || "");
  const [resourceIds, setResourceIds] = useState("");
  const [included, setIncluded] = useState<Set<string>>(new Set(["inventory", "findings", "changes"]));
  const [retention, setRetention] = useState("standard");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = workloadsQ.data?.workloads ?? [];

  function toggle(k: string) {
    setIncluded((p) => { const n = new Set(p); n.has(k) ? n.delete(k) : n.add(k); return n; });
  }

  async function create() {
    setBusy(true); setErr("");
    try {
      await api.createEvidence({
        name: name || "Snapshot",
        scope: { kind: scopeKind, id: scopeKind === "resources" ? "" : scopeId, resource_ids: scopeKind === "resources" ? resourceIds.split(/[\s,]+/).filter(Boolean) : [] },
        included: [...included],
        retention_class: retention,
        tags: tags.split(/[\s,]+/).filter(Boolean),
      });
      onCreated();
      onClose();
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-lg flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-semibold text-gray-900">📸 New evidence snapshot</div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4 text-xs">
          <label className="block"><span className="mb-1 block font-medium text-gray-700">Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. RCA evidence — incident 4821" className="w-full rounded border px-2 py-1.5" /></label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block"><span className="mb-1 block font-medium text-gray-700">Scope</span>
              <select value={scopeKind} onChange={(e) => setScopeKind(e.target.value)} className="w-full rounded border px-2 py-1.5">
                <option value="workload">Workload</option>
                <option value="subscription">Subscription</option>
                <option value="resources">Selected resources</option>
              </select></label>
            <label className="block"><span className="mb-1 block font-medium text-gray-700">Retention</span>
              <select value={retention} onChange={(e) => setRetention(e.target.value)} className="w-full rounded border px-2 py-1.5">
                <option value="standard">Standard (90 days)</option>
                <option value="audit">Audit-class (7 years)</option>
              </select></label>
          </div>
          {scopeKind === "workload" ? (
            <select value={scopeId} onChange={(e) => setScopeId(e.target.value)} className="w-full rounded border px-2 py-1.5">
              <option value="">— pick a workload —</option>
              {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          ) : scopeKind === "subscription" ? (
            <input value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder="Subscription GUID" className="w-full rounded border px-2 py-1.5" />
          ) : (
            <textarea value={resourceIds} onChange={(e) => setResourceIds(e.target.value)} placeholder="One ARM id per line" className="h-20 w-full rounded border px-2 py-1.5" />
          )}
          <div>
            <span className="mb-1 block font-medium text-gray-700">Include</span>
            <div className="grid grid-cols-2 gap-1">
              {INCLUDE_OPTIONS.map((o) => (
                <label key={o.key} className="flex items-center gap-1.5 text-gray-600">
                  <input type="checkbox" checked={included.has(o.key)} onChange={() => toggle(o.key)} />{o.label}
                </label>
              ))}
            </div>
          </div>
          <label className="block"><span className="mb-1 block font-medium text-gray-700">Tags</span>
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="comma or space separated" className="w-full rounded border px-2 py-1.5" /></label>
          {err && <div className="rounded-lg border border-red-200 bg-red-50 p-2 text-red-700">{err}</div>}
        </div>
        <div className="flex items-center gap-2 border-t px-4 py-2">
          <button onClick={() => void create()} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">{busy ? "Capturing…" : "Capture snapshot"}</button>
          <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-xs font-medium hover:bg-gray-50">Cancel</button>
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- Detail modal
function DetailModal({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const detailQ = useQuery({ queryKey: ["evidence", id], queryFn: () => api.evidenceDetail(id) });
  const [tab, setTab] = useState("inventory");
  const [search, setSearch] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter((c) => !c.disabled && ["jira", "servicenow"].includes(c.type));
  const [ticketOpen, setTicketOpen] = useState(false);

  const snap = detailQ.data?.snapshot;
  const verified = detailQ.data?.sha_verified;
  const tabsPresent = useMemo(() => CONTENT_TABS.filter((t) => snap?.included.includes(t) || (t === "properties" && snap?.included.includes("inventory"))), [snap]);
  const contentQ = useQuery({ queryKey: ["evidence-content", id, tab], queryFn: () => api.evidenceContent(id, tab === "properties" ? "inventory" : tab), enabled: !!snap });

  function download(text: string, name: string) {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  async function exportBundle() {
    try { const r = await api.exportEvidence(id); download(JSON.stringify(r.bundle, null, 2), `evidence-${id}.json`); }
    catch (e) { setMsg({ text: formatError(e), ok: false }); }
  }
  async function share() {
    try { const r = await api.shareEvidence(id); setMsg({ text: `Share link token: ${r.share.token} (expires ${formatTimestamp(r.share.expires_at)})`, ok: true }); }
    catch (e) { setMsg({ text: formatError(e), ok: false }); }
  }
  async function attachTicket(connectorId: string) {
    try {
      const r = await api.attachEvidence(id, { target: "ticket", connector_id: connectorId });
      setMsg({ text: r.ok ? `Attached to ticket${r.ticket_id ? ` ${r.ticket_id}` : ""} (SHA in body).` : r.detail || "Attach failed.", ok: !!r.ok });
      setTicketOpen(false);
      await qc.invalidateQueries({ queryKey: ["evidence", id] });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); }
  }
  async function attachRca() {
    try { await api.attachEvidence(id, { target: "rca" }); setMsg({ text: "Linked to RCA draft (SHA carried in the body).", ok: true }); await qc.invalidateQueries({ queryKey: ["evidence", id] }); }
    catch (e) { setMsg({ text: formatError(e), ok: false }); }
  }

  const rawContent = contentQ.data?.content;
  const contentStr = useMemo(() => {
    if (!rawContent) return "";
    const s = JSON.stringify(rawContent, null, 2);
    if (!search.trim()) return s;
    return s.split("\n").filter((l) => l.toLowerCase().includes(search.toLowerCase())).join("\n");
  }, [rawContent, search]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex max-h-[90vh] w-full max-w-4xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between border-b px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-gray-900">{snap?.name || "Snapshot"}</div>
            {snap && (
              <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                <span>{snap.scope.kind}:{snap.scope.id || "—"}</span>
                <span>· {formatTimestamp(snap.created_at)} by {snap.created_by}</span>
                <span className={`rounded px-1.5 py-0.5 ${snap.retention_class === "audit" ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-600"}`}>{snap.retention_class}</span>
                <span className="font-mono" title={snap.sha256}>SHA {shortSha(snap.sha256)}</span>
                <span className={verified ? "text-green-600" : "text-red-600"}>{verified ? "✓ verified" : "✗ tampered"}</span>
              </div>
            )}
          </div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="flex items-center gap-1 border-b px-3 text-xs">
          {tabsPresent.map((t) => (
            <button key={t} onClick={() => setTab(t)} className={`-mb-px border-b-2 px-2 py-1.5 capitalize ${tab === t ? "border-brand font-medium text-gray-900" : "border-transparent text-gray-500"}`}>{t}</button>
          ))}
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="search…" className="ml-auto my-1 w-40 rounded border px-2 py-1" />
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-3">
          {contentQ.isLoading ? <div className="py-10 text-center text-xs text-gray-400">Loading…</div> : (
            <pre className="overflow-auto rounded bg-gray-900 p-3 text-[10px] leading-relaxed text-gray-100">{contentStr || "(empty section)"}</pre>
          )}
          {msg && <div className={`mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>}
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t px-4 py-2">
          <button onClick={() => void attachRca()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">📎 Attach to RCA</button>
          {ticketOpen ? (
            ticketConnectors.length > 0 ? (
              <select autoFocus defaultValue="" onChange={(e) => e.target.value && void attachTicket(e.target.value)} className="rounded border px-1.5 py-1 text-xs">
                <option value="" disabled>Pick connector…</option>
                {ticketConnectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.type})</option>)}
              </select>
            ) : <span className="text-xs text-gray-400">No Jira/ServiceNow connector</span>
          ) : (
            <button onClick={() => setTicketOpen(true)} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">🎫 Attach to ticket</button>
          )}
          <button onClick={() => void share()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">🔗 Share read-only link</button>
          <button onClick={() => void exportBundle()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">⬇ Export</button>
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- Diff modal
function DiffModal({ a, b, onClose }: { a: string; b: string; onClose: () => void }) {
  const [typeFilter, setTypeFilter] = useState("");
  const [findingFilter, setFindingFilter] = useState("");
  const diffQ = useQuery({
    queryKey: ["evidence-diff", a, b, typeFilter, findingFilter],
    queryFn: () => api.evidenceDiff({ a, b, type_filter: typeFilter, finding_filter: findingFilter }),
  });
  const d: EvidenceDiff | undefined = diffQ.data?.diff;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-semibold text-gray-900">⇄ Snapshot diff</div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>
        <div className="border-b px-4 py-2 text-[11px] text-gray-500">
          {diffQ.data && <span>{diffQ.data.a.name} ({formatTimestamp(diffQ.data.a.created_at)}) → {diffQ.data.b.name} ({formatTimestamp(diffQ.data.b.created_at)})</span>}
          <div className="mt-1 flex gap-2">
            <input value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} placeholder="filter by resource type" className="rounded border px-2 py-1" />
            <input value={findingFilter} onChange={(e) => setFindingFilter(e.target.value)} placeholder="filter by finding" className="rounded border px-2 py-1" />
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4 text-xs">
          {!d ? <div className="py-8 text-center text-gray-400">Computing…</div> : (
            <>
              <section>
                <div className="mb-1 font-medium text-gray-700">Inventory — +{d.inventory.counts.added} −{d.inventory.counts.removed} ~{d.inventory.counts.changed}</div>
                {d.inventory.added.map((r) => <div key={r.id} className="text-green-700">+ {r.name} <span className="text-gray-400">{r.type}</span></div>)}
                {d.inventory.removed.map((r) => <div key={r.id} className="text-red-600">− {r.name} <span className="text-gray-400">{r.type}</span></div>)}
                {d.inventory.changed.map((r) => (
                  <div key={r.id} className="text-amber-700">~ {r.name} <span className="text-gray-400">({Object.keys(r.fields).join(", ")})</span></div>
                ))}
              </section>
              <section>
                <div className="mb-1 font-medium text-gray-700">Findings — +{d.findings.counts.added} −{d.findings.counts.removed} ~{d.findings.counts.changed}</div>
                {d.findings.changed.map((f) => (
                  <div key={f.check_id} className="text-amber-700">~ {f.title}: <b>{f.from.status}</b> → <b>{f.to.status}</b></div>
                ))}
                {d.findings.added.map((f, i) => <div key={i} className="text-green-700">+ {String((f as { title?: string }).title || (f as { check_id?: string }).check_id)}</div>)}
                {d.findings.removed.map((f, i) => <div key={i} className="text-red-600">− {String((f as { title?: string }).title || (f as { check_id?: string }).check_id)}</div>)}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- Locker page
export function EvidenceLockerPanel() {
  const qc = useQueryClient();
  const [creator, setCreator] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [diffOpen, setDiffOpen] = useState(false);
  const [retentionFilter, setRetentionFilter] = useState("");
  const [showTrash, setShowTrash] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const listQ = useQuery({ queryKey: ["evidence-list", retentionFilter], queryFn: () => api.evidenceList(retentionFilter ? { retention_class: retentionFilter } : {}) });
  const trashQ = useQuery({ queryKey: ["evidence-trash"], queryFn: () => api.evidenceTrash(), enabled: showTrash });
  const snaps = listQ.data?.snapshots ?? [];
  const trashed = trashQ.data?.snapshots ?? [];

  function toggleSel(id: string) {
    setSelected((p) => { if (p.includes(id)) return p.filter((x) => x !== id); return [...p, id].slice(-2); });
  }
  async function seedDemo() {
    await api.seedEvidenceDemo();
    await qc.invalidateQueries({ queryKey: ["evidence-list"] });
  }
  async function refreshAll() {
    await qc.invalidateQueries({ queryKey: ["evidence-list"] });
    await qc.invalidateQueries({ queryKey: ["evidence-trash"] });
  }
  async function deleteSnap(id: string) {
    setBusy(id); setMsg(null);
    try {
      await api.deleteEvidence(id);
      setSelected((p) => p.filter((x) => x !== id));
      await refreshAll();
      setMsg({ text: "Moved to Trash.", ok: true });
    } catch { setMsg({ text: "Delete failed.", ok: false }); } finally { setBusy(""); }
  }
  async function restoreSnap(id: string) {
    setBusy(id); setMsg(null);
    try { await api.restoreEvidence(id); await refreshAll(); setMsg({ text: "Restored.", ok: true }); }
    catch { setMsg({ text: "Restore failed.", ok: false }); } finally { setBusy(""); }
  }
  async function purgeSnap(id: string) {
    if (!window.confirm("Permanently delete this snapshot? This cannot be undone.")) return;
    setBusy(id); setMsg(null);
    try { await api.purgeEvidence(id); await refreshAll(); setMsg({ text: "Permanently deleted.", ok: true }); }
    catch { setMsg({ text: "Delete failed.", ok: false }); } finally { setBusy(""); }
  }
  async function emptyTrash() {
    if (!window.confirm(`Permanently delete all ${trashed.length} snapshot(s) in Trash? This cannot be undone.`)) return;
    setBusy("empty"); setMsg(null);
    try { const r = await api.emptyEvidenceTrash(); await refreshAll(); setMsg({ text: `Emptied Trash (${r.purged}).`, ok: true }); }
    catch { setMsg({ text: "Empty trash failed.", ok: false }); } finally { setBusy(""); }
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-50">
      <div className="border-b bg-white px-6 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Evidence Locker{showTrash ? " — Trash" : ""}</h1>
            <p className="text-xs text-gray-500">Hash-stamped, immutable investigation snapshots — diffable, attachable, auditable.</p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            {!showTrash && (
              <>
                <select value={retentionFilter} onChange={(e) => setRetentionFilter(e.target.value)} className="rounded-lg border px-2 py-1.5 text-xs">
                  <option value="">All retention</option>
                  <option value="standard">Standard</option>
                  <option value="audit">Audit-class</option>
                </select>
                {selected.length === 2 && (
                  <button onClick={() => setDiffOpen(true)} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">⇄ Diff selected</button>
                )}
                <button onClick={() => void seedDemo()} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">Seed demo</button>
                <button onClick={() => setCreator(true)} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90">📸 New snapshot</button>
              </>
            )}
            <button onClick={() => setShowTrash((v) => !v)} className={`rounded-lg border px-3 py-1.5 text-xs font-medium ${showTrash ? "bg-gray-900 text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}>
              🗑 {showTrash ? "Back to locker" : "Trash"}
            </button>
          </div>
        </div>
        {msg && <div className={`mt-2 rounded-md border px-3 py-1.5 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        {showTrash ? (
          trashQ.isLoading ? <div className="py-16 text-center text-sm text-gray-400">Loading…</div> :
          trashed.length === 0 ? <div className="py-16 text-center text-sm text-gray-400">Trash is empty.</div> : (
            <div className="mx-auto max-w-6xl space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-xs text-gray-500">{trashed.length} snapshot(s) in Trash. Restore brings them back; deleting forever removes the hash-stamped blob.</div>
                <button onClick={() => void emptyTrash()} disabled={busy === "empty"} className="rounded-lg border border-red-300 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50">Empty Trash</button>
              </div>
              {trashed.map((s: EvidenceSnapshot) => (
                <div key={s.id} className="flex items-center gap-3 rounded-xl border bg-white px-4 py-3">
                  <span className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-900">{s.name}</span>
                      {s.demo && <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo</span>}
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${s.retention_class === "audit" ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-600"}`}>{s.retention_class}</span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                      <span>{s.scope.kind}:{s.scope.id || "—"}</span>
                      <span className="font-mono" title={s.sha256}>SHA {shortSha(s.sha256)}</span>
                      {s.deleted_at && <span>· trashed {formatTimestamp(s.deleted_at)}{s.deleted_by ? ` by ${s.deleted_by}` : ""}</span>}
                    </div>
                  </span>
                  <button onClick={() => void restoreSnap(s.id)} disabled={busy === s.id} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50 disabled:opacity-50">↩ Restore</button>
                  <button onClick={() => void purgeSnap(s.id)} disabled={busy === s.id} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50">Delete forever</button>
                </div>
              ))}
            </div>
          )
        ) : listQ.isLoading ? <div className="py-16 text-center text-sm text-gray-400">Loading…</div> :
         snaps.length === 0 ? <div className="py-16 text-center text-sm text-gray-400">No snapshots yet — capture one or seed the demo.</div> : (
          <div className="mx-auto max-w-6xl space-y-2">
            {selected.length > 0 && <div className="text-xs text-gray-500">{selected.length} selected {selected.length === 2 ? "(ready to diff)" : "(pick 2 to diff)"}</div>}
            {snaps.map((s: EvidenceSnapshot) => (
              <div key={s.id} className={`flex items-center gap-3 rounded-xl border bg-white px-4 py-3 ${selected.includes(s.id) ? "ring-2 ring-brand" : ""}`}>
                <input type="checkbox" checked={selected.includes(s.id)} onChange={() => toggleSel(s.id)} />
                <button onClick={() => setDetailId(s.id)} className="min-w-0 flex-1 text-left">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-gray-900">{s.name}</span>
                    {s.demo && <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo</span>}
                    <span className={`rounded px-1.5 py-0.5 text-[10px] ${s.retention_class === "audit" ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-600"}`}>{s.retention_class}</span>
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                    <span>{s.scope.kind}:{s.scope.id || "—"}</span>
                    <span>· {formatTimestamp(s.created_at)} by {s.created_by}</span>
                    <span className="font-mono" title={s.sha256}>SHA {shortSha(s.sha256)}</span>
                    {s.included.map((i) => <span key={i} className="rounded bg-gray-100 px-1 py-0.5 text-[10px]">{i}</span>)}
                  </div>
                </button>
                {s.attachments.length > 0 && <span className="text-[11px] text-gray-400">{s.attachments.length} attach</span>}
                <button onClick={() => void deleteSnap(s.id)} disabled={busy === s.id} title="Move to Trash" className="rounded-lg border border-red-200 px-2 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50">🗑</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {creator && <CreatorModal onClose={() => setCreator(false)} onCreated={() => qc.invalidateQueries({ queryKey: ["evidence-list"] })} />}
      {detailId && <DetailModal id={detailId} onClose={() => setDetailId(null)} />}
      {diffOpen && selected.length === 2 && <DiffModal a={selected[0]} b={selected[1]} onClose={() => setDiffOpen(false)} />}
    </div>
  );
}
