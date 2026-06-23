// Ownership: AI-based owner import, owner→tag apply, and the shared tag-revisions (recovery +
// revert) UI. Mounted from the Ownership Directory tab and reused (TagRevisionsPanel) by Tag
// Intelligence.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  downloadBlob,
  type OwnerImportPreview,
  type OwnerTagPlan,
  type TagRevisionDiffRow,
  type Workload,
} from "../../api";
import { formatError } from "../../utils/format";

const btn = "rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-600 transition hover:bg-gray-50";
const btnPrimary = "rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-brand-dark disabled:opacity-50";

// ============================================================ Export buttons
export function OwnerExportButtons() {
  const [busy, setBusy] = useState("");
  const dl = async (format: "csv" | "xlsx") => {
    setBusy(format);
    try {
      const blob = await api.ownersExport(format);
      downloadBlob(blob, `owners.${format}`);
    } catch {
      /* ignore */
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="flex items-center gap-1">
      <button className={btn} disabled={busy === "csv"} onClick={() => dl("csv")}>⬇ CSV</button>
      <button className={btn} disabled={busy === "xlsx"} onClick={() => dl("xlsx")}>⬇ Excel</button>
    </div>
  );
}

// ============================================================ Import modal (AI column mapping)
export function OwnerImportModal({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [preview, setPreview] = useState<OwnerImportPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [createAssignments, setCreateAssignments] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<string>("");

  const onFile = async (file: File) => {
    setBusy(true);
    setErr("");
    try {
      const p = await api.ownersImportPreview(file);
      setPreview(p);
      setMapping(p.mapping);
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!preview) return;
    setBusy(true);
    setErr("");
    try {
      const res = await api.ownersImportConfirm(preview.rows, mapping, createAssignments);
      setDone(`Imported: ${res.created} created, ${res.updated} updated, ${res.assignments} assignments` + (res.skipped ? `, ${res.skipped} skipped` : ""));
      onImported();
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy(false);
    }
  };

  const downloadTemplate = async () => {
    try {
      const blob = await api.ownersTemplate();
      downloadBlob(blob, "owners-template.csv");
    } catch {
      /* ignore */
    }
  };

  const pv = preview?.preview;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-4xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">Import owners</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Upload any CSV or Excel file. The AI infers which columns map to owner fields. Review
          the mapping and preview below, then confirm. A blank list (just names/emails) is fine —
          you can map owners to resources later.
        </p>

        {!preview && (
          <div className="mt-4 rounded-xl border-2 border-dashed p-8 text-center">
            <input
              type="file"
              accept=".csv,.tsv,.txt,.xlsx,.xlsm"
              id="owner-import-file"
              className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void onFile(f); }}
            />
            <label htmlFor="owner-import-file" className="cursor-pointer">
              <div className="text-3xl">📥</div>
              <div className="mt-2 text-sm font-medium text-brand">{busy ? "Reading…" : "Choose a CSV / Excel file"}</div>
              <div className="mt-1 text-xs text-gray-400">or drag &amp; drop — name, email, department + optional workload / subscription / resource ids</div>
            </label>
            <div className="mt-3">
              <button onClick={downloadTemplate} className="text-xs text-gray-500 underline hover:text-gray-700">Download a blank template</button>
            </div>
          </div>
        )}

        {err && <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
        {done && <div className="mt-3 rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700">{done}</div>}

        {preview && !done && (
          <div className="mt-4 space-y-4">
            {/* AI mapping */}
            <div className="rounded-xl border p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">Column mapping</span>
                <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${preview.ai ? "bg-violet-100 text-violet-700" : "bg-gray-100 text-gray-500"}`}>
                  {preview.ai ? `AI · ${Math.round(preview.confidence * 100)}%` : "heuristic"}
                </span>
              </div>
              <p className="mt-1 text-xs text-gray-500">{preview.explanation}</p>
              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {preview.target_fields.map((f) => (
                  <label key={f} className="flex items-center gap-2 text-xs">
                    <span className="w-24 shrink-0 capitalize text-gray-600">{f.replace("_", " ")}</span>
                    <select
                      value={mapping[f] || ""}
                      onChange={(e) => setMapping({ ...mapping, [f]: e.target.value })}
                      className="min-w-0 flex-1 rounded border px-1.5 py-1 text-xs"
                    >
                      <option value="">— none —</option>
                      {preview.columns.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </label>
                ))}
              </div>
              {!mapping.display_name && <div className="mt-2 text-xs text-red-600">A “display name” column is required.</div>}
            </div>

            {/* Stats + preview table */}
            {pv && (
              <div className="rounded-xl border">
                <div className="flex flex-wrap items-center gap-3 border-b px-3 py-2 text-xs">
                  <span><b className="text-gray-700">{preview.row_count}</b> rows</span>
                  <span className="text-green-600">{pv.valid} valid</span>
                  {pv.invalid > 0 && <span className="text-red-600">{pv.invalid} without a name</span>}
                  <span className="text-gray-500">{pv.with_subject} with a workload/subscription/resource</span>
                </div>
                <div className="max-h-64 overflow-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-gray-50 text-left text-gray-400">
                      <tr>
                        <th className="px-2 py-1 font-medium">Name</th>
                        <th className="px-2 py-1 font-medium">Email</th>
                        <th className="px-2 py-1 font-medium">Dept</th>
                        <th className="px-2 py-1 font-medium">Kind</th>
                        <th className="px-2 py-1 font-medium">Subject</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {pv.rows.slice(0, 50).map((r, i) => (
                        <tr key={i} className={r.valid ? "" : "bg-red-50/40"}>
                          <td className="px-2 py-1 text-gray-800">{r.display_name || <span className="text-red-500">(missing)</span>}</td>
                          <td className="px-2 py-1 text-gray-500">{r.email}</td>
                          <td className="px-2 py-1 text-gray-500">{r.department}</td>
                          <td className="px-2 py-1 text-gray-500">{r.kind}</td>
                          <td className="px-2 py-1 text-gray-500">{r.workload || r.subscription || (r.resource_ids[0] ?? "")}{r.resource_ids.length > 1 ? ` +${r.resource_ids.length - 1}` : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <label className="flex items-center gap-2 text-xs text-gray-600">
              <input type="checkbox" checked={createAssignments} onChange={(e) => setCreateAssignments(e.target.checked)} />
              Create ownership assignments from the workload / subscription / resource columns
            </label>

            <div className="flex items-center gap-2">
              <button className={btnPrimary} disabled={busy || !mapping.display_name} onClick={confirm}>
                {busy ? "Importing…" : `Import ${pv?.valid ?? 0} owner(s)`}
              </button>
              <button className={btn} onClick={() => { setPreview(null); setErr(""); }}>Choose a different file</button>
            </div>
            {preview.row_count > preview.rows.length && (
              <div className="text-[11px] text-amber-600">
                Note: this import covers the first {preview.rows.length} rows. For very large
                sheets, split the file or import in batches.
              </div>
            )}
          </div>
        )}

        {done && (
          <div className="mt-4 flex justify-end">
            <button className={btnPrimary} onClick={onClose}>Done</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================ Owner → tag apply
export function OwnerTagApplyModal({ onClose, onApplied }: { onClose: () => void; onApplied: () => void }) {
  const workloadsQ = useQuery({ queryKey: ["workloads", "list"], queryFn: api.workloads });
  const workloads: Workload[] = workloadsQ.data?.workloads ?? [];
  const [scopeKind, setScopeKind] = useState<"workload" | "subscription">("workload");
  const [workloadId, setWorkloadId] = useState("");
  const [subscriptionId, setSubscriptionId] = useState("");
  const [tagKey, setTagKey] = useState("owner");
  const [valueSource, setValueSource] = useState<"display_name" | "email">("display_name");
  const [overwrite, setOverwrite] = useState(false);
  const [plan, setPlan] = useState<OwnerTagPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<string>("");

  const req = () => ({
    scope_kind: scopeKind, workload_id: workloadId, subscription_id: subscriptionId,
    tag_key: tagKey, value_source: valueSource, overwrite,
  });

  const doPreview = async () => {
    setBusy(true); setErr(""); setResult("");
    try {
      setPlan(await api.ownerTagApplyPreview(req()));
    } catch (e) { setErr(formatError(e)); } finally { setBusy(false); }
  };
  const doApply = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api.ownerTagApply({ ...req(), approved: true });
      if (r.ok || r.applied) {
        setResult(`Applied to ${r.applied} resource(s)${r.failed ? `, ${r.failed} failed` : ""}. A recovery revision was saved — you can revert it.`);
        onApplied();
      } else {
        setErr(r.error || "Apply failed.");
      }
    } catch (e) { setErr(formatError(e)); } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">Apply owner as Azure tag</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Stamp each resource's resolved owner into an Azure tag. A recovery copy of the current
          tags is saved first, so the change can be reverted.
        </p>

        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-xs">
            <span className="mb-1 block font-medium text-gray-600">Scope</span>
            <select value={scopeKind} onChange={(e) => { setScopeKind(e.target.value as "workload" | "subscription"); setPlan(null); }} className="w-full rounded border px-2 py-1.5">
              <option value="workload">Workload</option>
              <option value="subscription">Subscription</option>
            </select>
          </label>
          {scopeKind === "workload" ? (
            <label className="text-xs">
              <span className="mb-1 block font-medium text-gray-600">Workload</span>
              <select value={workloadId} onChange={(e) => { setWorkloadId(e.target.value); setPlan(null); }} className="w-full rounded border px-2 py-1.5">
                <option value="">Choose…</option>
                {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </label>
          ) : (
            <label className="text-xs">
              <span className="mb-1 block font-medium text-gray-600">Subscription ID</span>
              <input value={subscriptionId} onChange={(e) => { setSubscriptionId(e.target.value); setPlan(null); }} placeholder="GUID" className="w-full rounded border px-2 py-1.5" />
            </label>
          )}
          <label className="text-xs">
            <span className="mb-1 block font-medium text-gray-600">Tag key</span>
            <input value={tagKey} onChange={(e) => { setTagKey(e.target.value); setPlan(null); }} className="w-full rounded border px-2 py-1.5" />
          </label>
          <label className="text-xs">
            <span className="mb-1 block font-medium text-gray-600">Tag value</span>
            <select value={valueSource} onChange={(e) => { setValueSource(e.target.value as "display_name" | "email"); setPlan(null); }} className="w-full rounded border px-2 py-1.5">
              <option value="display_name">Owner display name</option>
              <option value="email">Owner email</option>
            </select>
          </label>
        </div>
        <label className="mt-2 flex items-center gap-2 text-xs text-gray-600">
          <input type="checkbox" checked={overwrite} onChange={(e) => { setOverwrite(e.target.checked); setPlan(null); }} />
          Overwrite an existing different value of this tag
        </label>

        {err && <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
        {result && <div className="mt-3 rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700">{result}</div>}

        {plan && !result && (
          <div className="mt-4 rounded-xl border">
            <div className="flex flex-wrap items-center gap-3 border-b px-3 py-2 text-xs">
              <span><b className="text-gray-700">{plan.applicable}</b> to apply</span>
              {plan.conflicts > 0 && <span className="text-amber-600">{plan.conflicts} conflict(s){overwrite ? " (will overwrite)" : " (skipped)"}</span>}
              {plan.no_owner > 0 && <span className="text-gray-500">{plan.no_owner} unowned (skipped)</span>}
            </div>
            <div className="max-h-56 overflow-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-gray-50 text-left text-gray-400">
                  <tr><th className="px-2 py-1 font-medium">Resource</th><th className="px-2 py-1 font-medium">{plan.tag_key}</th><th className="px-2 py-1 font-medium">Status</th></tr>
                </thead>
                <tbody className="divide-y">
                  {plan.items.slice(0, 100).map((it) => (
                    <tr key={it.id}>
                      <td className="px-2 py-1 text-gray-800">{it.name}</td>
                      <td className="px-2 py-1"><span className="text-gray-400 line-through">{it.current}</span> {it.skipped ? "" : <span className="text-green-700">{it.owner}</span>}</td>
                      <td className="px-2 py-1">{it.skipped ? <span className="text-amber-600">skipped</span> : <span className="text-green-600">apply</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!result && (
          <div className="mt-4 flex items-center gap-2">
            {!plan ? (
              <button className={btnPrimary} disabled={busy || (scopeKind === "workload" ? !workloadId : !subscriptionId)} onClick={doPreview}>
                {busy ? "Building plan…" : "Preview plan"}
              </button>
            ) : (
              <>
                <button className={btnPrimary} disabled={busy || plan.applicable === 0} onClick={doApply}>
                  {busy ? "Applying…" : `Apply to ${plan.applicable} resource(s)`}
                </button>
                <button className={btn} onClick={() => setPlan(null)}>Edit</button>
              </>
            )}
            <button className={btn} onClick={onClose}>Cancel</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================ Tag revisions (recovery + revert)
export function TagRevisionsPanel({ mode }: { mode: "ownership" | "tagintel" }) {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const listFn = mode === "ownership" ? () => api.ownerTagRevisions() : () => api.tagRevisions();
  const revsQ = useQuery({ queryKey: ["tag-revisions", mode], queryFn: listFn });
  const detailQ = useQuery({
    queryKey: ["tag-revision", mode, openId],
    queryFn: () => (mode === "ownership" ? api.ownerTagRevision(openId) : api.tagRevision(openId)),
    enabled: !!openId,
  });

  const revert = async (id: string) => {
    if (!confirm("Revert this change? Each resource's tags will be restored to the captured prior state.")) return;
    setBusy(id); setMsg("");
    try {
      const r = mode === "ownership" ? await api.revertOwnerTagRevision(id) : await api.revertTagRevision(id);
      setMsg(r.ok ? `Reverted ${r.reverted} resource(s).` : (r.error || `Reverted ${r.reverted}, ${r.failed} failed.`));
      qc.invalidateQueries({ queryKey: ["tag-revisions", mode] });
    } catch (e) { setMsg(formatError(e)); } finally { setBusy(""); }
  };

  const revs = revsQ.data?.revisions ?? [];

  return (
    <div className="rounded-xl border bg-white">
      <div className="border-b px-4 py-2.5">
        <div className="text-sm font-semibold text-gray-800">Tag change history</div>
        <div className="text-xs text-gray-500">Every applied tag change keeps a recovery copy of the prior tags — expand to visualize, or revert to restore them exactly.</div>
      </div>
      {msg && <div className="border-b bg-brand/5 px-4 py-1.5 text-xs text-brand">{msg}</div>}
      {revs.length === 0 ? (
        <div className="p-6 text-center text-xs text-gray-400">No tag changes recorded yet.</div>
      ) : (
        <ul className="divide-y">
          {revs.map((r) => (
            <li key={r.id} className="px-4 py-2.5">
              <div className="flex items-center gap-2">
                <button onClick={() => setOpenId(openId === r.id ? "" : r.id)} className="min-w-0 flex-1 text-left">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-gray-800">{r.description || "(tag change)"}</span>
                    {r.status === "reverted" && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">reverted</span>}
                    {r.source.startsWith("revert") && <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] text-sky-700">revert</span>}
                  </div>
                  <div className="text-[11px] text-gray-400">
                    {new Date(r.created_at).toLocaleString()} · {r.actor || "—"} · {r.resource_count} resource(s) · {r.applied} applied{r.failed ? `, ${r.failed} failed` : ""}
                  </div>
                </button>
                {r.status !== "reverted" && (
                  <button onClick={() => revert(r.id)} disabled={busy === r.id} className={btn}>
                    {busy === r.id ? "Reverting…" : "↩ Revert"}
                  </button>
                )}
              </div>
              {openId === r.id && (
                <div className="mt-2 max-h-64 overflow-auto rounded-lg border bg-gray-50/60 p-2">
                  {detailQ.isLoading ? (
                    <div className="text-xs text-gray-400">Loading…</div>
                  ) : (
                    <RevisionDiff rows={detailQ.data?.diff ?? []} />
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RevisionDiff({ rows }: { rows: TagRevisionDiffRow[] }) {
  if (rows.length === 0) return <div className="text-xs text-gray-400">No per-resource changes.</div>;
  return (
    <table className="w-full text-[11px]">
      <thead className="text-left text-gray-400">
        <tr><th className="px-1 py-0.5 font-medium">Resource</th><th className="px-1 py-0.5 font-medium">Change</th></tr>
      </thead>
      <tbody className="divide-y divide-gray-200">
        {rows.slice(0, 100).map((r) => (
          <tr key={r.id}>
            <td className="px-1 py-1 align-top text-gray-700">{r.name || r.id.split("/").pop()}</td>
            <td className="px-1 py-1">
              {Object.entries(r.added).map(([k, v]) => <div key={"a" + k} className="text-green-700">+ {k}={v}</div>)}
              {Object.entries(r.changed).map(([k, v]) => <div key={"c" + k} className="text-amber-700">~ {k}: <span className="line-through">{v.from}</span> → {v.to}</div>)}
              {Object.entries(r.removed).map(([k, v]) => <div key={"r" + k} className="text-red-600">− {k}={v}</div>)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
