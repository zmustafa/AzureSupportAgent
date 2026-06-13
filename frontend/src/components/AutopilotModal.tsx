import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  streamAutopilot,
  type TreeNode,
  type TypeCount,
  type WorkloadCandidate,
} from "../api";
import { formatError } from "../utils/format";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

function confidenceTag(c: number): { label: string; cls: string } {
  if (c >= 0.8) return { label: "High confidence", cls: "bg-green-100 text-green-700" };
  if (c >= 0.5) return { label: "Medium confidence", cls: "bg-amber-100 text-amber-700" };
  return { label: "Low confidence", cls: "bg-gray-100 text-gray-600" };
}

export function TypeChips({ types, max = 8 }: { types: TypeCount[]; max?: number }) {
  const shown = types.slice(0, max);
  const extra = types.length - shown.length;
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map((t) => (
        <span key={t.label} className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-700">
          {t.label} <span className="font-semibold">({t.count})</span>
        </span>
      ))}
      {extra > 0 && <span className="text-[11px] text-gray-400">+{extra} more</span>}
    </div>
  );
}

type Stage = "setup" | "running" | "review";

export function AutopilotModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections });
  const connections = connQ.data?.connections ?? [];

  const [connectionId, setConnectionId] = useState("");
  const [scopeKind, setScopeKind] = useState<"subscription" | "mg">("subscription");
  const [scopeId, setScopeId] = useState("");
  const [scopeName, setScopeName] = useState("");
  const [scopeOptions, setScopeOptions] = useState<TreeNode[]>([]);
  const [scopeLoading, setScopeLoading] = useState(false);

  const [stage, setStage] = useState<Stage>("setup");
  const [log, setLog] = useState<{ phase: string; message: string }[]>([]);
  const [candidates, setCandidates] = useState<WorkloadCandidate[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [meta, setMeta] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const logEndRef = useRef<HTMLDivElement>(null);

  // Load scope options (subscriptions or MGs) when connection / kind changes.
  useEffect(() => {
    if (!connectionId) {
      setScopeOptions([]);
      return;
    }
    let cancelled = false;
    setScopeLoading(true);
    setScopeId("");
    setScopeName("");
    api
      .workloadTree({ connection_id: connectionId, group_by: scopeKind })
      .then((r) => {
        if (cancelled) return;
        // In MG mode the top level may contain both mg and subscription nodes; keep matching kind.
        const opts = r.nodes.filter((n) => n.kind === scopeKind || scopeKind === "mg");
        setScopeOptions(opts);
      })
      .catch((e) => !cancelled && setError(formatError(e)))
      .finally(() => !cancelled && setScopeLoading(false));
    return () => {
      cancelled = true;
    };
  }, [connectionId, scopeKind]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [log, candidates]);

  function start() {
    if (!connectionId || !scopeId) {
      setError("Pick a connection and a scope.");
      return;
    }
    setError("");
    setStage("running");
    setLog([]);
    setCandidates([]);
    setSelected(new Set());
    setMeta(null);
    const controller = new AbortController();
    abortRef.current = controller;
    void streamAutopilot(
      { connection_id: connectionId, scope_kind: scopeKind, scope_id: scopeId, scope_name: scopeName },
      {
        onStatus: (d) => setLog((l) => [...l, { phase: d.phase, message: d.message }]),
        onCandidate: (d) => {
          setLog((l) => [...l, { phase: "candidate", message: d.message }]);
          setCandidates((c) => {
            const next = [...c, d.candidate];
            setSelected((s) => new Set(s).add(next.length - 1));
            return next;
          });
        },
        onDone: (d) => {
          setMeta(d.meta);
          setStage("review");
        },
        onError: (m) => {
          setError(m);
          setStage("review");
        },
      },
      controller.signal,
    );
  }

  function cancel() {
    abortRef.current?.abort();
    onClose();
  }

  async function save() {
    const chosen = candidates.filter((_, i) => selected.has(i));
    if (chosen.length === 0) {
      setError("Select at least one workload to save.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.saveAutopilotWorkloads({
        connection_id: connectionId,
        scope_kind: scopeKind,
        scope_id: scopeId,
        scope_name: scopeName,
        candidates: chosen,
      });
      onSaved();
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={cancel}>
      <div
        className="flex h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-800">
              ✨ Workload Autopilot
            </h2>
            <p className="text-xs text-gray-500">
              Point it at a subscription or management group and it discovers the workloads inside.
            </p>
          </div>
          <button onClick={cancel} className="rounded p-1.5 text-gray-400 hover:bg-gray-100">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {/* Setup */}
          {stage === "setup" && (
            <div className="space-y-3">
              <div>
                <label className={label}>Azure connection</label>
                <select className={input} value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
                  <option value="">Select a connection…</option>
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>{c.display_name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className={label}>Discover under</label>
                <div className="flex gap-2">
                  {(["subscription", "mg"] as const).map((k) => (
                    <button
                      key={k}
                      onClick={() => setScopeKind(k)}
                      className={`rounded-lg border px-3 py-1.5 text-sm transition ${
                        scopeKind === k ? "border-brand bg-brand/5 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"
                      }`}
                    >
                      {k === "subscription" ? "Subscription" : "Management group"}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className={label}>{scopeKind === "mg" ? "Management group" : "Subscription"}</label>
                <select
                  className={input}
                  value={scopeId}
                  disabled={!connectionId || scopeLoading}
                  onChange={(e) => {
                    setScopeId(e.target.value);
                    setScopeName(scopeOptions.find((o) => o.id === e.target.value)?.name ?? "");
                  }}
                >
                  <option value="">
                    {scopeLoading ? "Loading…" : !connectionId ? "Pick a connection first" : "Select…"}
                  </option>
                  {scopeOptions.map((o) => (
                    <option key={o.id} value={o.id}>{o.name}</option>
                  ))}
                </select>
              </div>
              {error && <div className="text-xs text-red-600">{error}</div>}
            </div>
          )}

          {/* Running / Review */}
          {stage !== "setup" && (
            <div className="space-y-4">
              {/* Progress log */}
              <div className="rounded-lg border bg-gray-50 p-3">
                <div className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-600">
                  {stage === "running" && (
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                  )}
                  Discovery progress
                </div>
                <div className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-gray-600">
                  {log.map((l, i) => (
                    <div key={i} className={l.phase === "candidate" ? "text-brand" : ""}>
                      <span className="text-gray-400">›</span> {l.message}
                    </div>
                  ))}
                  <div ref={logEndRef} />
                </div>
              </div>

              {/* Candidates */}
              {candidates.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-700">
                      {candidates.length} candidate workload{candidates.length === 1 ? "" : "s"}
                    </span>
                    <div className="flex gap-2 text-xs">
                      <button onClick={() => setSelected(new Set(candidates.map((_, i) => i)))} className="text-brand hover:underline">All</button>
                      <button onClick={() => setSelected(new Set())} className="text-gray-500 hover:underline">None</button>
                    </div>
                  </div>
                  {candidates.map((c, i) => {
                    const ct = confidenceTag(c.confidence);
                    return (
                      <label key={i} className="flex cursor-pointer gap-3 rounded-xl border bg-white p-3 hover:border-brand/40">
                        <input
                          type="checkbox"
                          className="mt-1"
                          checked={selected.has(i)}
                          onChange={(e) => {
                            setSelected((s) => {
                              const n = new Set(s);
                              if (e.target.checked) n.add(i);
                              else n.delete(i);
                              return n;
                            });
                          }}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-gray-800">{c.name}</span>
                            <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${ct.cls}`}>{ct.label}</span>
                            <span className="text-[11px] text-gray-400">{c.resource_count} resources</span>
                          </div>
                          {c.description && <p className="mt-0.5 text-xs text-gray-500">{c.description}</p>}
                          <div className="mt-1.5"><TypeChips types={c.types} /></div>
                          {c.reasoning && (
                            <details className="mt-1.5">
                              <summary className="cursor-pointer text-[11px] text-gray-400 hover:text-gray-600">Why these belong together</summary>
                              <p className="mt-1 text-[11px] text-gray-500">{c.reasoning}</p>
                              {c.resource_groups.length > 0 && (
                                <p className="mt-1 text-[10px] text-gray-400">Resource groups: {c.resource_groups.join(", ")}</p>
                              )}
                            </details>
                          )}
                        </div>
                      </label>
                    );
                  })}
                </div>
              )}

              {stage === "review" && candidates.length === 0 && !error && (
                <div className="rounded-lg border border-dashed p-6 text-center text-sm text-gray-500">
                  No workloads were discovered under this scope.
                </div>
              )}
              {meta && (
                <p className="text-[11px] text-gray-400">
                  Scanned {String(meta.resource_count)} resources
                  {meta.subscriptions ? ` across ${String(meta.subscriptions)} subscription(s)` : ""}.
                  {Number(meta.ungrouped) > 0 ? ` ${String(meta.ungrouped)} resource(s) didn't fit a workload.` : ""}
                  {meta.truncated ? " (Resource limit reached — results may be partial.)" : ""}
                </p>
              )}
              {error && <div className="text-xs text-red-600">{error}</div>}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
          {stage === "setup" && (
            <>
              <button onClick={cancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
              <button onClick={start} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">
                Start discovery
              </button>
            </>
          )}
          {stage === "running" && (
            <button onClick={cancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          )}
          {stage === "review" && (
            <>
              <button onClick={cancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Close</button>
              {candidates.length === 0 ? (
                <button onClick={() => setStage("setup")} className="rounded-lg border border-brand/40 px-3.5 py-1.5 text-sm text-brand hover:bg-brand/5">
                  Try another scope
                </button>
              ) : (
                <button
                  onClick={() => void save()}
                  disabled={saving || selected.size === 0}
                  className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
                >
                  {saving ? "Saving…" : `Save ${selected.size} workload${selected.size === 1 ? "" : "s"}`}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
