import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamDnsDebug,
  type DnsDebugDiff,
  type DnsDebugRun,
  type DnsDebugSource,
  type DnsSourceResult,
  type DnsStep,
  type DnsZoneFacts,
} from "../api";
import { formatError } from "../utils/format";

const STEP_LABEL: Record<string, string> = {
  effective_dns: "Effective DNS",
  resolver: "Resolver",
  resolve: "Resolve FQDN",
  cname: "CNAME chain",
  classify: "Public / private",
  hosts: "Hosts shadow",
  gate: "Gate",
};
const MARK: Record<string, string> = { ok: "✓", fail: "✗", warn: "⚠", skip: "–" };
const CLS: Record<string, string> = { ok: "text-green-600", fail: "text-red-500", warn: "text-amber-500", skip: "text-gray-300" };
const CLASS_CLS: Record<string, string> = {
  private: "bg-green-100 text-green-700",
  public: "bg-red-100 text-red-700",
  nxdomain: "bg-amber-100 text-amber-700",
};

export function DnsDebugModal({
  architectureId,
  preset,
  onClose,
}: {
  architectureId: string;
  preset?: { fqdn?: string };
  onClose: () => void;
}) {
  const [allSources, setAllSources] = useState<DnsDebugSource[]>([]);
  const [sourcesFallback, setSourcesFallback] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [fqdn, setFqdn] = useState(preset?.fqdn ?? "");
  const [vnetId, setVnetId] = useState("");
  const [running, setRunning] = useState(false);
  // Live per-source steps keyed by source label.
  const [liveSteps, setLiveSteps] = useState<Record<string, DnsStep[]>>({});
  const [sourceResults, setSourceResults] = useState<DnsSourceResult[]>([]);
  const [zoneFacts, setZoneFacts] = useState<DnsZoneFacts | null>(null);
  const [run, setRun] = useState<DnsDebugRun | null>(null);
  const [diff, setDiff] = useState<DnsDebugDiff[]>([]);
  const [err, setErr] = useState("");
  const [iac, setIac] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    api.dnsdebugSources(architectureId)
      .then((r) => {
        setAllSources(r.sources);
        setSourcesFallback(!!r.fallback);
        const firstEnabled = r.sources.find((s) => !s.disabled);
        if (firstEnabled) setSelectedIds([firstEnabled.id]);
      })
      .catch(() => setAllSources([]));
  }, [architectureId]);

  function toggleSource(id: string) {
    setSelectedIds((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  }

  function start() {
    setRunning(true);
    setLiveSteps({});
    setSourceResults([]);
    setZoneFacts(null);
    setRun(null);
    setDiff([]);
    setErr("");
    setIac(null);
    setMsg(null);
    const ac = new AbortController();
    abortRef.current = ac;
    void streamDnsDebug(
      { architecture_id: architectureId, source_vm_ids: selectedIds, fqdn, source_vnet_id: vnetId || undefined },
      {
        onEvidence: (e) => setZoneFacts(e),
        onStep: (s) => setLiveSteps((prev) => {
          const src = s.source || "";
          return { ...prev, [src]: [...(prev[src] || []), s] };
        }),
        onSourceDone: (s) => setSourceResults((prev) => [...prev, s]),
        onDone: (d) => { setRun(d.run); setDiff(d.diff); setRunning(false); },
        onError: (m) => { setErr(m); setRunning(false); },
      },
      ac.signal,
    );
  }

  useEffect(() => () => abortRef.current?.abort(), []);

  async function genIac() {
    if (!run) return;
    try {
      const r = await api.dnsdebugIac(run.id);
      setIac(r.iac);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  function download(text: string, name: string) {
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  async function pin(toWarRoom: boolean) {
    if (!run) return;
    try {
      const r = await api.pinDnsdebug({ run_id: run.id, to_war_room: toWarRoom });
      setMsg({ text: r.detail || "Pinned.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  async function exportReport() {
    if (!run) return;
    try {
      const r = await api.dnsdebugReport(run.id);
      download(r.markdown, `dns-resolution-${run.fqdn}.md`);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  // Columns to render: prefer final source results; during run, the live steps.
  const columns = useMemo(() => {
    if (sourceResults.length > 0) {
      return sourceResults.map((s) => ({ source: s.source, steps: s.steps, result: s }));
    }
    return Object.entries(liveSteps).map(([source, steps]) => ({ source, steps, result: undefined as DnsSourceResult | undefined }));
  }, [sourceResults, liveSteps]);

  const diffBySource = useMemo(() => Object.fromEntries(diff.map((d) => [d.source, d])), [diff]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-semibold text-gray-900">🧭 Debug Private Endpoint resolution</div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {/* Target + sources */}
          <div className="text-xs">
            <div className="mb-1 font-medium text-gray-700">Target FQDN</div>
            <input value={fqdn} onChange={(e) => setFqdn(e.target.value)} placeholder="e.g. shopassets.blob.core.windows.net"
              className="w-full rounded border px-2 py-1.5" />
          </div>

          <div className="mt-3 text-xs">
            <div className="mb-1 flex items-center gap-2">
              <span className="font-medium text-gray-700">Resolve from (pick 1 or more for side-by-side)</span>
            </div>
            {allSources.length === 0 ? (
              <p className="text-[11px] text-amber-600">No sandbox VM onboarded — add one in Settings → Sandbox VMs.</p>
            ) : (
              <>
                {sourcesFallback && (
                  <p className="mb-1 text-[11px] text-amber-600">No sandbox VM is linked to this architecture's workload — showing all sandbox VMs. Link it in Settings → Sandbox VMs to make this automatic.</p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  {allSources.map((s) => (
                    <button
                      key={s.id}
                      onClick={() => toggleSource(s.id)}
                      disabled={s.disabled}
                      className={`rounded-full border px-2.5 py-1 ${selectedIds.includes(s.id) ? "border-brand bg-brand/10 text-brand" : "text-gray-600 hover:bg-gray-50"} disabled:opacity-40`}
                    >
                      {s.display_name}{s.vnet_label ? ` · ${s.vnet_label}` : ""}{s.linked === false ? " · not linked" : ""}
                    </button>
                  ))}
                </div>
              </>
            )}
            <input value={vnetId} onChange={(e) => setVnetId(e.target.value)} placeholder="Source VNet resource id (optional, for zone-link check)"
              className="mt-2 w-full rounded border px-2 py-1.5" />
          </div>

          <div className="mt-3 flex items-center gap-2">
            <button onClick={start} disabled={running || !fqdn} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">
              {running ? "Resolving…" : "Debug resolution"}
            </button>
            {run && !running && <button onClick={start} className="rounded-lg border px-3 py-1.5 text-xs font-medium hover:bg-gray-50">↻ Re-run</button>}
            {err && <span className="text-xs text-red-600">{err}</span>}
          </div>

          {/* Verdict */}
          {run && (
            <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-2 text-xs">
              <span className={`mr-2 rounded px-2 py-0.5 font-medium ${CLASS_CLS[run.overall_classification] ?? ""}`}>{run.overall_classification?.toUpperCase()}</span>
              <span className="text-gray-700">{run.verdict}</span>
            </div>
          )}

          {/* Multi-source side-by-side chains */}
          {columns.length > 0 && (
            <div className={`mt-3 grid gap-3 ${columns.length > 1 ? "grid-cols-2" : "grid-cols-1"}`}>
              {columns.map((col) => (
                <div key={col.source} className="rounded-lg border bg-white">
                  <div className="flex items-center justify-between border-b px-3 py-1.5">
                    <span className="text-xs font-medium text-gray-800">{col.source}</span>
                    {col.result && (
                      <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${CLASS_CLS[col.result.classification] ?? ""}`}>
                        {col.result.classification}{col.result.resolved_ip ? ` · ${col.result.resolved_ip}` : ""}
                      </span>
                    )}
                    {diffBySource[col.source] && (
                      <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">{diffBySource[col.source].from} → {diffBySource[col.source].to}</span>
                    )}
                  </div>
                  <div className="space-y-1 p-2">
                    {col.steps.map((s, i) => (
                      <div key={i} className="flex items-start gap-2 text-[11px]">
                        <span className={`${CLS[s.status]}`}>{MARK[s.status]}</span>
                        <div className="min-w-0">
                          <span className="font-medium text-gray-700">{STEP_LABEL[s.step] || s.step}</span>
                          <span className="ml-1 text-gray-500">{s.evidence}</span>
                        </div>
                      </div>
                    ))}
                    {col.result?.verdict && col.result.classification !== "private" && (
                      <div className="mt-1 rounded border border-amber-200 bg-amber-50 p-1.5 text-[10px] text-amber-800">{col.result.verdict}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Azure zone facts */}
          {zoneFacts && (
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer font-medium text-gray-600">Azure DNS facts</summary>
              {!zoneFacts.available && <p className="mt-1 text-gray-500">{zoneFacts.notes}</p>}
              {zoneFacts.available && (
                <div className="mt-1 space-y-0.5 text-[11px] text-gray-600">
                  <div>Expected zone: <b>{zoneFacts.expected_zone || "—"}</b></div>
                  <div>Zone exists: {String(zoneFacts.zone_exists)}</div>
                  <div>Linked to source VNet: {String(zoneFacts.linked_to_source_vnet)}</div>
                </div>
              )}
            </details>
          )}

          {/* IaC */}
          {iac && (
            <div className="mt-2">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-gray-700">Bicep remediation</span>
                <button onClick={() => download(iac, "dns-fix.bicep")} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">⬇ Download</button>
              </div>
              <pre className="max-h-56 overflow-auto rounded bg-gray-900 p-2 text-[10px] leading-tight text-gray-100">{iac}</pre>
            </div>
          )}

          {msg && (
            <div className={`mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
          )}
        </div>

        {run && (
          <div className="flex flex-wrap items-center gap-2 border-t px-4 py-2">
            {run.misconfig_kind && run.misconfig_kind !== "no_resolution" && (
              <button onClick={() => void genIac()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">🛠 Generate Bicep</button>
            )}
            <button onClick={() => void pin(false)} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">📌 Pin to activity</button>
            <button onClick={() => void pin(true)} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">🚨 Send to War Room</button>
            <button onClick={() => void exportReport()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">⬇ Export report</button>
          </div>
        )}
      </div>
    </div>
  );
}
