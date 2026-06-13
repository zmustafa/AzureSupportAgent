import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";import {
  api,
  streamTeleintelAsk,
  type TeleIntelOverview,
  type TeleIntelTimeline,
  type TeleIntelTriage,
  type TeleIntelTransaction,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";

const SEV_TONE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  error: "bg-red-100 text-red-700",
  warning: "bg-amber-100 text-amber-700",
  info: "bg-sky-100 text-sky-700",
};

function ResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return <div className="px-3 py-2 text-xs text-gray-400">No rows.</div>;
  const cols = Object.keys(rows[0]);
  return (
    <div className="max-h-72 overflow-auto rounded border">
      <table className="w-full text-[12px]">
        <thead className="sticky top-0 bg-gray-50 text-left text-gray-500">
          <tr>{cols.map((c) => <th key={c} className="px-2 py-1 font-medium">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((r, i) => (
            <tr key={i} className="border-t">
              {cols.map((c) => <td key={c} className="px-2 py-1 text-gray-700">{String((r as Record<string, unknown>)[c] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// A compact multi-series sparkline-style timeline using inline SVG (no chart dep).
function CorrelationTimeline({ tl }: { tl: TeleIntelTimeline }) {
  const W = 720, H = 150, pad = 28;
  const pts = tl.points ?? [];
  if (pts.length < 2) return <div className="text-xs text-gray-400">Not enough points to plot.</div>;
  const xs = pts.map((_, i) => pad + (i / (pts.length - 1)) * (W - 2 * pad));
  const series: { key: string; color: string; label: string }[] = [
    { key: "failure_rate_pct", color: "#ef4444", label: "Failure %" },
    { key: "dep_failure_pct", color: "#f59e0b", label: "Dep fail %" },
    { key: "p95_ms", color: "#3b82f6", label: "p95 ms" },
    { key: "exceptions", color: "#a855f7", label: "Exceptions" },
  ];
  const t0 = new Date(pts[0].timestamp as string).getTime();
  const t1 = new Date(pts[pts.length - 1].timestamp as string).getTime();
  const xForTs = (ts: string) => {
    const t = new Date(ts).getTime();
    if (!t1 || t1 === t0) return pad;
    return pad + ((t - t0) / (t1 - t0)) * (W - 2 * pad);
  };
  return (
    <div className="overflow-x-auto">
      <svg width={W} height={H} className="rounded border bg-white">
        {series.map((s) => {
          const vals = pts.map((p) => Number(p[s.key] ?? 0));
          const max = Math.max(1, ...vals);
          const path = vals
            .map((v, i) => `${i === 0 ? "M" : "L"}${xs[i].toFixed(1)},${(H - pad - (v / max) * (H - 2 * pad)).toFixed(1)}`)
            .join(" ");
          return <path key={s.key} d={path} fill="none" stroke={s.color} strokeWidth="1.5" />;
        })}
        {(tl.change_events ?? []).map((e, i) => {
          const x = xForTs(e.timestamp);
          return (
            <g key={i}>
              <line x1={x} y1={6} x2={x} y2={H - pad} stroke="#10b981" strokeWidth="1.5" strokeDasharray="3 2" />
              <text x={x + 2} y={16} fontSize="9" fill="#047857">⚙ {e.target}</text>
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex flex-wrap gap-3 text-[11px]">
        {series.map((s) => (
          <span key={s.key} className="flex items-center gap-1">
            <span className="inline-block h-2 w-3 rounded" style={{ background: s.color }} /> {s.label}
          </span>
        ))}
        <span className="flex items-center gap-1 text-emerald-700">⚙ deploy/config change</span>
      </div>
    </div>
  );
}

export function TelemetryIntelligencePanel() {
  const navigate = useNavigate();
  const [scopeKind, setScopeKind] = usePersistedState<"workload" | "subscription">("azsup.teleintel.scopeKind", "workload");
  const [workloadId, setWorkloadId] = usePersistedState("azsup.teleintel.workloadId", "");
  const [subId, setSubId] = usePersistedState("azsup.teleintel.subId", "");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // Ask box state
  const [question, setQuestion] = useState("");
  const [askKql, setAskKql] = useState("");
  const [askRows, setAskRows] = useState<Record<string, unknown>[]>([]);
  const [askAnswer, setAskAnswer] = useState("");
  const [asking, setAsking] = useState(false);

  // Transaction state
  const [opId, setOpId] = useState("");
  const [txn, setTxn] = useState<TeleIntelTransaction | null>(null);
  const [txnBusy, setTxnBusy] = useState(false);

  const [busy, setBusy] = useState("");
  const [ticketOpen, setTicketOpen] = useState(false);

  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const connectorsQ = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const ticketConnectors = (connectorsQ.data?.connectors ?? []).filter(
    (c) => !c.disabled && ["jira", "servicenow"].includes(c.type),
  );
  const workloads = workloadsQ.data?.workloads ?? [];

  // Canvas handoff: "Analyze telemetry" stores the workload to pre-select.
  useEffect(() => {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem("azsup.teleintelHandoff"); } catch { return; }
    if (!raw) return;
    try { sessionStorage.removeItem("azsup.teleintelHandoff"); } catch { /* ignore */ }
    try {
      const h = JSON.parse(raw) as { workloadId?: string };
      if (h.workloadId) {
        setScopeKind("workload");
        setWorkloadId(h.workloadId);
      }
    } catch {
      /* ignore */
    }
  }, []);
  // No default selection: do NOT auto-fetch on page visit. Telemetry Intelligence only loads
  // once the user explicitly picks a workload (or enters a subscription). The canvas handoff
  // above may set workloadId to pre-select one.
  const effWorkloadId = scopeKind === "workload" ? workloadId : "";
  const params = scopeKind === "workload" ? { workload_id: effWorkloadId } : { subscription_id: subId };
  const enabled = scopeKind === "workload" ? !!effWorkloadId : !!subId;

  const overviewQ = useQuery({
    queryKey: ["teleintel-overview", scopeKind, effWorkloadId, subId],
    queryFn: () => api.teleintelOverview(params),
    enabled,
  });
  const triageQ = useQuery({
    queryKey: ["teleintel-triage", scopeKind, effWorkloadId, subId],
    queryFn: () => api.teleintelTriage(params),
    enabled,
  });
  const timelineQ = useQuery({
    queryKey: ["teleintel-timeline", scopeKind, effWorkloadId, subId],
    queryFn: () => api.teleintelTimeline(params),
    enabled,
  });
  const smartQ = useQuery({
    queryKey: ["teleintel-smart", scopeKind, effWorkloadId, subId],
    queryFn: () => api.teleintelSmartDetection(params),
    enabled,
  });
  const codeOptQ = useQuery({
    queryKey: ["teleintel-codeopt", scopeKind, effWorkloadId, subId],
    queryFn: () => api.teleintelCodeOptimizations(params),
    enabled,
  });

  const overview: TeleIntelOverview | undefined = overviewQ.data;
  const triage: TeleIntelTriage | undefined = triageQ.data;

  async function ask(q?: string) {
    const question_ = (q ?? question).trim();
    if (!question_) return;
    setAsking(true);
    setAskKql("");
    setAskRows([]);
    setAskAnswer("");
    setMsg(null);
    try {
      await streamTeleintelAsk(
        { question: question_, ...params },
        {
          onKql: (d) => setAskKql(d.kql),
          onRows: (d) => setAskRows(d.rows),
          onAnswer: (d) => setAskAnswer(d.answer),
          onError: (m) => setMsg({ text: m, ok: false }),
        },
      );
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setAsking(false);
    }
  }

  async function rerunKql() {
    if (!askKql.trim()) return;
    setAsking(true);
    setMsg(null);
    try {
      const r = await api.teleintelQuery({ kql: askKql, ...params });
      if (!r.ok) {
        setMsg({ text: r.error || "Query failed.", ok: false });
      } else {
        setAskRows(r.rows);
        setAskAnswer("");
      }
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setAsking(false);
    }
  }

  async function explainTxn() {
    if (!opId.trim()) return;
    setTxnBusy(true);
    setMsg(null);
    try {
      const r = await api.teleintelTransaction({ operation_id: opId.trim(), ...params });
      setTxn(r);
      if (!r.ok) setMsg({ text: r.error || "Reconstruction failed.", ok: false });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setTxnBusy(false);
    }
  }

  async function registerFinding() {
    if (!triage || scopeKind !== "workload" || !effWorkloadId) {
      setMsg({ text: "Switch to a workload scope to register a finding.", ok: false });
      return;
    }
    setBusy("finding");
    setMsg(null);
    try {
      const r = await api.registerTeleintelFinding({ workload_id: effWorkloadId, workload_name: overview?.scope_name ?? "", triage });
      setMsg({ text: `Registered finding (run ${r.run_id.slice(0, 8)}).`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function createTicket(connectorId: string) {
    if (!triage) return;
    setBusy("ticket");
    setMsg(null);
    try {
      const r = await api.createTeleintelTicket({ connector_id: connectorId, triage });
      setMsg({ text: r.ok ? `Ticket created${r.ticket_id ? ` (${r.ticket_id})` : ""}.` : r.detail || "Ticket failed.", ok: !!r.ok });
      setTicketOpen(false);
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  function openWarRoom() {
    if (!triage) return;
    const s = triage.summary;
    const kqlBlock = (triage.evidence ?? []).slice(0, 3).map((e) => `-- ${e.label}\n${e.kql}`).join("\n\n");
    const prompt =
      `War Room: investigate the telemetry failure spike on ${s.operation} (${s.failure_rate_pct}% failure, ` +
      `${s.failed}/${s.total} requests). Top correlated dependency: ${s.top_dependency} (${s.dependency_correlation_pct}%). ` +
      `Probable trigger: ${s.probable_trigger || "unknown"}.\n\nHypothesis: ${triage.hypothesis}\n\nEvidence KQL:\n${kqlBlock}`;
    try {
      sessionStorage.setItem("azsup.warRoomHandoff", JSON.stringify({ workloadId: effWorkloadId, prompt }));
    } catch {
      /* ignore */
    }
    navigate("/chat");
  }

  const exampleQuestions = useMemo(
    () => [
      "why were checkout requests slow yesterday afternoon?",
      "top 5 slowest dependencies in the last day",
      "which operations have the highest failure rate?",
      "exception count by type over the last 24 hours",
    ],
    [],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b bg-white px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-gray-900">📈 Telemetry Intelligence</h1>
            <p className="text-xs text-gray-500">
              AI correlation &amp; triage over Application Insights — ask in plain English, auto-join
              requests↔exceptions↔dependencies, and stitch failures to the deploy that caused them. Read-only.
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <select value={scopeKind} onChange={(e) => setScopeKind(e.target.value as never)} className="rounded-md border px-2 py-1.5 text-sm">
              <option value="workload">By workload</option>
              <option value="subscription">By subscription</option>
            </select>
            {scopeKind === "workload" ? (
              <select value={effWorkloadId} onChange={(e) => setWorkloadId(e.target.value)} className="max-w-[240px] rounded-md border px-2 py-1.5 text-sm">
                <option value="">Select a workload…</option>
                {workloads.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            ) : (
              <input value={subId} onChange={(e) => setSubId(e.target.value)} placeholder="Subscription GUID" className="w-[260px] rounded-md border px-2 py-1.5 text-sm" />
            )}
          </div>
        </div>
        {overview && (
          <div className="mt-1 text-[11px] text-gray-400">
            {overview.demo ? "Demo data · " : overview.connection_configured ? "" : "No Azure connection · "}
            {overview.components.length} App Insights component(s){overview.error ? ` · ${overview.error}` : ""}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        {msg && (
          <div className={`mb-3 rounded-md border px-3 py-2 text-sm ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}

        {!enabled ? (
          <div className="p-8 text-center text-sm text-gray-500">
            {scopeKind === "workload"
              ? "Select a workload to load telemetry intelligence."
              : "Enter a subscription to load telemetry intelligence."}
          </div>
        ) : (
        <>
        {/* Ask your telemetry */}
        <div className="mb-5 rounded-lg border bg-white p-4">
          <div className="mb-2 text-sm font-semibold text-gray-900">Ask your telemetry</div>
          <div className="flex gap-2">
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
              placeholder="e.g. why were checkout requests slow yesterday afternoon?"
              className="flex-1 rounded-md border px-3 py-2 text-sm"
            />
            <button onClick={() => ask()} disabled={asking || !enabled} className="rounded-md bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50">
              {asking ? "Asking…" : "Ask"}
            </button>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {exampleQuestions.map((q) => (
              <button key={q} onClick={() => { setQuestion(q); ask(q); }} className="rounded-full border bg-gray-50 px-2.5 py-1 text-[11px] text-gray-600 hover:bg-gray-100">{q}</button>
            ))}
          </div>
          {(askKql || askAnswer || askRows.length > 0) && (
            <div className="mt-3 space-y-2">
              {askAnswer && <div className="rounded-md bg-sky-50 px-3 py-2 text-sm text-gray-800">{askAnswer}</div>}
              {askKql && (
                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <span className="text-[11px] font-medium uppercase text-gray-500">Generated KQL (editable — transparency)</span>
                    <button onClick={rerunKql} disabled={asking} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">▶ Run</button>
                  </div>
                  <textarea value={askKql} onChange={(e) => setAskKql(e.target.value)} spellCheck={false} rows={Math.min(8, Math.max(3, askKql.split("\n").length))}
                    className="w-full rounded border bg-gray-900 p-2 font-mono text-[11px] text-gray-100" />
                </div>
              )}
              {askRows.length > 0 && <ResultTable rows={askRows} />}
            </div>
          )}
        </div>

        {/* AI Failure Triage */}
        <div className="mb-5 rounded-lg border bg-white p-4">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-semibold text-gray-900">🔬 AI Failure Triage</div>
            {triage?.has_spike && (
              <div className="flex gap-2">
                <button onClick={registerFinding} disabled={busy === "finding"} className="rounded-md border px-2.5 py-1 text-xs hover:bg-gray-50 disabled:opacity-50">🛡️ Create finding</button>
                <button onClick={openWarRoom} className="rounded-md border px-2.5 py-1 text-xs hover:bg-gray-50">🔎 Open War Room</button>
                <div className="relative">
                  <button onClick={() => setTicketOpen(!ticketOpen)} disabled={ticketConnectors.length === 0} className="rounded-md border px-2.5 py-1 text-xs hover:bg-gray-50 disabled:opacity-50">🎫 Create ticket</button>
                  {ticketOpen && (
                    <div className="absolute right-0 z-10 mt-1 w-48 rounded-md border bg-white shadow-lg">
                      {ticketConnectors.map((c) => (
                        <button key={c.id} onClick={() => createTicket(c.id)} className="block w-full px-3 py-1.5 text-left text-sm hover:bg-gray-50">{c.name} ({c.type})</button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
          {triageQ.isLoading ? (
            <div className="text-sm text-gray-400">Running correlated triage…</div>
          ) : !triage || !triage.has_spike ? (
            <div className="text-sm text-gray-400">{triage?.error || "No failure spike detected in the current window."}</div>
          ) : (
            <>
              <div className="rounded-md bg-amber-50 px-3 py-2 text-sm text-gray-800">{triage.hypothesis}</div>
              <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="Operation" value={triage.summary.operation} />
                <Stat label="Failure rate" value={`${triage.summary.failure_rate_pct}%`} tone="text-red-600" />
                <Stat label="Top dependency" value={`${triage.summary.top_dependency} (${triage.summary.dependency_correlation_pct}%)`} />
                <Stat label="Probable trigger" value={triage.summary.probable_trigger ? "deploy/config" : "—"} />
              </div>
              <div className="mt-3 space-y-2">
                <div className="text-[11px] font-medium uppercase text-gray-500">Cited evidence (every claim links to its query)</div>
                {(triage.evidence ?? []).map((e, i) => (
                  <details key={i} className="rounded border">
                    <summary className="cursor-pointer px-2 py-1 text-xs text-gray-700">{e.label} {e.ok ? "" : "⚠"} <span className="text-gray-400">({e.rows.length} rows)</span></summary>
                    <pre className="overflow-auto border-t bg-gray-900 p-2 text-[10px] text-gray-100">{e.kql}</pre>
                    <div className="border-t"><ResultTable rows={e.rows} /></div>
                  </details>
                ))}
              </div>
            </>
          )}
        </div>

        {/* Correlation timeline */}
        <div className="mb-5 rounded-lg border bg-white p-4">
          <div className="mb-2 text-sm font-semibold text-gray-900">Cross-signal correlation timeline</div>
          {timelineQ.isLoading ? (
            <div className="text-sm text-gray-400">Building timeline…</div>
          ) : timelineQ.data && timelineQ.data.points.length > 1 ? (
            <>
              <CorrelationTimeline tl={timelineQ.data} />
              <div className="mt-1 text-[11px] text-gray-400">{timelineQ.data.signal_count} signals · {timelineQ.data.bin_minutes}m bins{timelineQ.data.notes ? ` · ${timelineQ.data.notes}` : ""}</div>
            </>
          ) : (
            <div className="text-sm text-gray-400">No timeline data in the current window.</div>
          )}
        </div>

        <div className="grid gap-5 lg:grid-cols-2">
          {/* Smart Detection inbox */}
          <div className="rounded-lg border bg-white p-4">
            <div className="mb-2 text-sm font-semibold text-gray-900">Smart Detection inbox <span className="text-[11px] font-normal text-gray-400">(aggregated + ranked)</span></div>
            {smartQ.isLoading ? (
              <div className="text-sm text-gray-400">Loading…</div>
            ) : (smartQ.data?.items ?? []).length === 0 ? (
              <div className="text-sm text-gray-400">{smartQ.data?.note || "No Smart Detection results."}</div>
            ) : (
              <div className="space-y-1.5">
                {smartQ.data!.items.map((it, i) => (
                  <div key={i} className="flex items-center gap-2 rounded border px-2 py-1.5 text-sm">
                    <span className={`rounded px-1.5 py-0.5 text-[10px] ${SEV_TONE[it.severity] || SEV_TONE.info}`}>{it.severity}</span>
                    <span className="text-gray-800">{it.display_name}</span>
                    <span className="ml-auto text-[11px] text-gray-400">{it.components.length} component(s)</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Explain this transaction */}
          <div className="rounded-lg border bg-white p-4">
            <div className="mb-2 text-sm font-semibold text-gray-900">Explain this transaction</div>
            <div className="flex gap-2">
              <input value={opId} onChange={(e) => setOpId(e.target.value)} placeholder="Paste an operation_Id" className="flex-1 rounded-md border px-3 py-1.5 text-sm font-mono" />
              <button onClick={explainTxn} disabled={txnBusy || !opId.trim()} className="rounded-md bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">{txnBusy ? "…" : "Explain"}</button>
            </div>
            {txn?.ok && txn.spans.length > 0 && (
              <div className="mt-3">
                <div className="rounded-md bg-sky-50 px-3 py-2 text-sm text-gray-800">{txn.narration}</div>
                <div className="mt-2 text-[11px] text-gray-500">Total ~{txn.total_ms}ms · failing step: <b>{txn.failing_step || "none"}</b></div>
                <div className="mt-2 space-y-1">
                  {txn.spans.map((s, i) => (
                    <div key={i} className={`flex items-center gap-2 rounded border px-2 py-1 text-[12px] ${s.failed ? "border-red-200 bg-red-50" : ""}`}>
                      <span className="w-20 shrink-0 text-gray-400">{s.kind}</span>
                      <span className="text-gray-800">{s.name}</span>
                      {s.target && <span className="text-gray-400">→ {s.target}</span>}
                      <span className="ml-auto text-gray-500">{s.duration_ms != null ? `${s.duration_ms}ms` : ""} {s.result_code}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Code Optimizations */}
        {(codeOptQ.data?.items ?? []).length > 0 && (
          <div className="mt-5 rounded-lg border bg-white p-4">
            <div className="mb-2 text-sm font-semibold text-gray-900">⚡ Code Optimizations <span className="text-[11px] font-normal text-gray-400">(Profiler-based .NET)</span></div>
            <div className="space-y-1.5">
              {codeOptQ.data!.items.map((it, i) => (
                <div key={i} className="flex items-center gap-2 rounded border px-2 py-1.5 text-sm">
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{it.type}</span>
                  <span className="text-gray-800">{it.issue}</span>
                  <span className="ml-auto text-[11px] text-gray-400">{it.impact}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        </>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-3 py-2">
      <div className={`truncate text-sm font-semibold ${tone ?? "text-gray-900"}`} title={value}>{value}</div>
      <div className="truncate text-[11px] text-gray-500">{label}</div>
    </div>
  );
}
