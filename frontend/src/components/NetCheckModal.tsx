import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamNetcheckRun,
  type NetCheckDiff,
  type NetCheckEvidence,
  type NetCheckRun,
  type NetCheckSource,
  type NetCheckStep,
} from "../api";
import { formatError } from "../utils/format";

const STEP_LABEL: Record<string, string> = {
  dns: "DNS resolve",
  icmp: "ICMP ping",
  tcp: "TCP connect",
  tls: "TLS handshake",
  http: "HTTP probe",
  gate: "Gate",
};
const STATUS_MARK: Record<string, string> = { ok: "✓", fail: "✗", warn: "⚠", skip: "–" };
const STATUS_CLS: Record<string, string> = {
  ok: "text-green-600",
  fail: "text-red-500",
  warn: "text-amber-500",
  skip: "text-gray-300",
};
const VERDICT_CLS: Record<string, string> = {
  reachable: "bg-green-100 text-green-700",
  degraded: "bg-amber-100 text-amber-700",
  blocked: "bg-red-100 text-red-700",
};

export function NetCheckModal({
  architectureId,
  preset,
  onClose,
}: {
  architectureId: string;
  preset?: { targetNodeId?: string; targetHost?: string; sourceNodeId?: string };
  onClose: () => void;
}) {
  const [sources, setSources] = useState<NetCheckSource[]>([]);
  const [sourcesFallback, setSourcesFallback] = useState(false);
  const [sourceVmId, setSourceVmId] = useState("");
  const [sourceHost, setSourceHost] = useState("");
  const [targetHost, setTargetHost] = useState(preset?.targetHost ?? "");
  const [port, setPort] = useState(443);
  const [protocol, setProtocol] = useState("tcp");
  const [httpPath, setHttpPath] = useState("");
  const [sni, setSni] = useState("");

  const [running, setRunning] = useState(false);
  const [steps, setSteps] = useState<NetCheckStep[]>([]);
  const [evidence, setEvidence] = useState<NetCheckEvidence | null>(null);
  const [run, setRun] = useState<NetCheckRun | null>(null);
  const [diff, setDiff] = useState<NetCheckDiff[]>([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const targetNodeId = preset?.targetNodeId ?? "";
  const sourceNodeId = preset?.sourceNodeId ?? "";

  useEffect(() => {
    api.netcheckSources(architectureId)
      .then((r) => {
        setSources(r.sources);
        setSourcesFallback(!!r.fallback);
        const firstEnabled = r.sources.find((s) => !s.disabled);
        if (firstEnabled) setSourceVmId(firstEnabled.id);
      })
      .catch(() => setSources([]));
    // Pre-load the last run for this architecture as a starting point (diff baseline).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [architectureId]);

  function start() {
    setRunning(true);
    setSteps([]);
    setEvidence(null);
    setRun(null);
    setDiff([]);
    setErr("");
    setMsg(null);
    const ac = new AbortController();
    abortRef.current = ac;
    const payload: Record<string, unknown> = {};
    if (httpPath) payload.http_path = httpPath;
    if (sni) payload.sni = sni;
    void streamNetcheckRun(
      {
        architecture_id: architectureId,
        source_vm_id: sourceVmId || undefined,
        source_host: sourceHost || undefined,
        source_node_id: sourceNodeId || undefined,
        target_node_id: targetNodeId || undefined,
        target_host: targetHost || undefined,
        port,
        protocol,
        payload,
      },
      {
        onStep: (s) => setSteps((prev) => [...prev, s]),
        onEvidence: (e) => setEvidence(e),
        onDone: (d) => { setRun(d.run); setDiff(d.diff); setRunning(false); },
        onError: (m) => { setErr(m); setRunning(false); },
      },
      ac.signal,
    );
  }

  function rerun() {
    start();
  }

  useEffect(() => () => abortRef.current?.abort(), []);

  async function pin(toWarRoom: boolean) {
    if (!run) return;
    try {
      const r = await api.pinNetcheck({ run_id: run.id, to_war_room: toWarRoom });
      setMsg({ text: r.detail || "Pinned to activity feed.", ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  async function exportReport() {
    if (!run) return;
    try {
      const r = await api.netcheckReport(run.id);
      const blob = new Blob([r.markdown], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `connectivity-${run.target}-${run.port}.md`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    }
  }

  const deny = evidence?.matched_deny || run?.evidence?.matched_deny;
  const diffByStep = useMemo(() => Object.fromEntries(diff.map((d) => [d.step, d])), [diff]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-semibold text-gray-900">🔌 Test connectivity</div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {/* Source / target picker */}
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div>
              <div className="mb-1 font-medium text-gray-700">Source (sandbox VM)</div>
              <select value={sourceVmId} onChange={(e) => setSourceVmId(e.target.value)} className="w-full rounded border px-2 py-1.5">
                <option value="">— pick a sandbox VM —</option>
                {sources.map((s) => (
                  <option key={s.id} value={s.id} disabled={s.disabled}>
                    {s.display_name} {s.vnet_label ? `(${s.vnet_label})` : ""}{s.disabled ? " — disabled" : ""}{s.linked === false ? " — not linked to this workload" : ""}
                  </option>
                ))}
              </select>
              <input value={sourceHost} onChange={(e) => setSourceHost(e.target.value)} placeholder="…or source FQDN/IP"
                className="mt-1 w-full rounded border px-2 py-1.5" />
              {sources.length === 0 ? (
                <p className="mt-1 text-[10px] text-amber-600">No sandbox VM onboarded — add one in Settings → Sandbox VMs (or enter a source FQDN/IP above).</p>
              ) : sourcesFallback ? (
                <p className="mt-1 text-[10px] text-amber-600">No sandbox VM is linked to this architecture's workload — showing all sandbox VMs. Link it in Settings → Sandbox VMs to make this automatic.</p>
              ) : null}
            </div>
            <div>
              <div className="mb-1 font-medium text-gray-700">Target</div>
              <input value={targetHost} onChange={(e) => setTargetHost(e.target.value)} placeholder="FQDN / private IP"
                className="w-full rounded border px-2 py-1.5" />
              {targetNodeId && !targetHost && <p className="mt-1 text-[10px] text-gray-400">Defaults to the clicked node's private address.</p>}
              <div className="mt-1 flex gap-2">
                <label className="flex-1"><span className="mb-0.5 block text-[10px] text-gray-500">Port</span>
                  <input type="number" value={port} onChange={(e) => setPort(parseInt(e.target.value || "0", 10))} className="w-full rounded border px-2 py-1" /></label>
                <label className="flex-1"><span className="mb-0.5 block text-[10px] text-gray-500">Protocol</span>
                  <select value={protocol} onChange={(e) => setProtocol(e.target.value)} className="w-full rounded border px-1.5 py-1">
                    <option value="tcp">TCP</option>
                    <option value="tls">TLS</option>
                    <option value="http">HTTP</option>
                    <option value="https">HTTPS</option>
                  </select></label>
              </div>
            </div>
          </div>
          {(protocol === "http" || protocol === "https") && (
            <input value={httpPath} onChange={(e) => setHttpPath(e.target.value)} placeholder="HTTP path (e.g. /health)" className="mt-2 w-full rounded border px-2 py-1.5 text-xs" />
          )}
          {(protocol === "tls" || protocol === "https" || port === 443) && (
            <input value={sni} onChange={(e) => setSni(e.target.value)} placeholder="TLS SNI (optional, defaults to target)" className="mt-2 w-full rounded border px-2 py-1.5 text-xs" />
          )}

          <div className="mt-3 flex items-center gap-2">
            <button onClick={start} disabled={running} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">
              {running ? "Probing…" : "Run probe"}
            </button>
            {run && !running && (
              <button onClick={rerun} className="rounded-lg border px-3 py-1.5 text-xs font-medium hover:bg-gray-50">↻ Re-run</button>
            )}
            {err && <span className="text-xs text-red-600">{err}</span>}
          </div>

          {/* Live hop steps */}
          {steps.length > 0 && (
            <div className="mt-4 space-y-1">
              {steps.map((s) => {
                const d = diffByStep[s.step];
                return (
                  <div key={s.step} className="flex items-start gap-2 rounded-lg border bg-white px-3 py-2 text-xs">
                    <span className={`${STATUS_CLS[s.status]} text-sm`}>{STATUS_MARK[s.status]}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-800">{STEP_LABEL[s.step] || s.step}</span>
                        <span className="text-gray-500">{s.evidence}</span>
                        {d && <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">{d.from} → {d.to}</span>}
                        <span className="ml-auto text-[10px] text-gray-400">{s.duration_ms}ms</span>
                      </div>
                      {s.raw && (
                        <pre className="mt-1 max-h-24 overflow-auto rounded bg-gray-900 p-1.5 text-[10px] leading-tight text-gray-100">{s.raw}</pre>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Verdict + diff */}
          {run && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <span className={`rounded px-2 py-0.5 font-medium ${VERDICT_CLS[run.verdict]}`}>{run.verdict.toUpperCase()}</span>
              {run.demo && <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">demo</span>}
              {diffByStep.verdict && <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">changed: {diffByStep.verdict.from} → {diffByStep.verdict.to}</span>}
            </div>
          )}

          {/* Intent mismatch */}
          {run?.mismatch && (
            <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
              <b>Intent mismatch:</b> {run.mismatch.detail}
            </div>
          )}

          {/* Blocked-by */}
          {deny && (
            <div className="mt-2 rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700">
              Blocked at NSG <b>{deny.nsg}</b> rule <b>{deny.name}</b> (priority {deny.priority}, {deny.access} {deny.direction} :{deny.destinationPortRange})
            </div>
          )}

          {/* Evidence */}
          {evidence && (
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer font-medium text-gray-600">Azure control-plane evidence</summary>
              {!evidence.available && <p className="mt-1 text-gray-500">{evidence.notes}</p>}
              {evidence.nsg_rules.length > 0 && (
                <div className="mt-1">
                  <div className="font-medium text-gray-500">Effective NSG rules</div>
                  {evidence.nsg_rules.slice(0, 8).map((r, i) => (
                    <div key={i} className="text-[10px] text-gray-600">{r.access} {r.direction} :{r.destinationPortRange} — {r.nsg}/{r.name} (p{r.priority})</div>
                  ))}
                </div>
              )}
              {evidence.effective_routes.length > 0 && (
                <div className="mt-1 text-[10px] text-gray-500">{evidence.effective_routes.length} effective route(s)</div>
              )}
            </details>
          )}

          {msg && (
            <div className={`mt-2 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
          )}
        </div>

        {/* Footer actions */}
        {run && (
          <div className="flex items-center gap-2 border-t px-4 py-2">
            <button onClick={() => void pin(false)} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">📌 Pin to activity</button>
            <button onClick={() => void pin(true)} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">🚨 Send to War Room</button>
            <button onClick={() => void exportReport()} className="rounded-lg border px-2.5 py-1 text-xs hover:bg-gray-50">⬇ Export report</button>
          </div>
        )}
      </div>
    </div>
  );
}
