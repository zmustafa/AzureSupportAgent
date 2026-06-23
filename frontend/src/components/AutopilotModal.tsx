import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  streamAutopilot,
  type TreeNode,
  type TypeCount,
  type WorkloadCandidate,
} from "../api";
import { formatError } from "../utils/format";
import { AzureIcon } from "./AzureIcon";

const input =
  "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
const label = "mb-1 block text-xs font-medium text-gray-600";

function confidenceTag(c: number): { label: string; cls: string } {
  if (c >= 0.8) return { label: "High confidence", cls: "bg-green-100 text-green-700" };
  if (c >= 0.5) return { label: "Medium confidence", cls: "bg-amber-100 text-amber-700" };
  return { label: "Low confidence", cls: "bg-gray-100 text-gray-600" };
}

// Shared classification badge styling (also reused by WorkloadsView).
export const TYPE_LABELS: Record<string, string> = {
  web_app: "Web app",
  data_pipeline: "Data pipeline",
  ai_ml: "AI / ML",
  networking: "Networking",
  storage: "Data / storage",
  identity: "Identity",
  integration: "Integration",
  other: "Other",
};
export const ENV_STYLE: Record<string, string> = {
  production: "bg-red-50 text-red-700 border-red-200",
  staging: "bg-amber-50 text-amber-700 border-amber-200",
  development: "bg-sky-50 text-sky-700 border-sky-200",
  test: "bg-violet-50 text-violet-700 border-violet-200",
  dr: "bg-orange-50 text-orange-700 border-orange-200",
  shared: "bg-gray-50 text-gray-600 border-gray-200",
  unknown: "bg-gray-50 text-gray-500 border-gray-200",
};
export const CRIT_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  high: "bg-orange-100 text-orange-700",
  medium: "bg-amber-100 text-amber-700",
  low: "bg-gray-100 text-gray-600",
};
export const CRIT_OPTIONS = ["", "critical", "high", "medium", "low"];

export function ClassBadges({
  type,
  environment,
  criticality,
}: {
  type?: string;
  environment?: string;
  criticality?: string;
}) {
  return (
    <>
      {type && (
        <span className="rounded-md border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
          {TYPE_LABELS[type] ?? type}
        </span>
      )}
      {environment && environment !== "unknown" && (
        <span className={`rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${ENV_STYLE[environment] ?? ENV_STYLE.unknown}`}>
          {environment}
        </span>
      )}
      {criticality && (
        <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-medium ${CRIT_STYLE[criticality] ?? CRIT_STYLE.low}`}>
          {criticality}
        </span>
      )}
    </>
  );
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
  // Grouping STRATEGY (ai | resource_group | subscription | tag) and discovery MODE
  // (full | delta — skip resources already in a saved workload).
  const [strategy, setStrategy] = useState<"ai" | "resource_group" | "subscription" | "tag">("ai");
  const [mode, setMode] = useState<"full" | "delta">("full");
  const [tagKey, setTagKey] = useState("");

  const [stage, setStage] = useState<Stage>("setup");
  const [log, setLog] = useState<{ phase: string; message: string }[]>([]);
  const [candidates, setCandidates] = useState<WorkloadCandidate[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Free-text filter over the discovered candidates (name / description / type / RG / class).
  const [search, setSearch] = useState("");
  // Per-candidate inline edits (rename + criticality) the user makes while reviewing —
  // applied on save AND recorded into grouping memory so the next run learns from them.
  const [edits, setEdits] = useState<Record<number, { name?: string; criticality?: string }>>({});
  // Discover -> Act: optionally launch a Mission Control sweep + architecture on save.
  const [autoAssess, setAutoAssess] = useState(false);
  const [autoArchitecture, setAutoArchitecture] = useState(false);

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
      .workloadTree({ connection_id: connectionId, group_by: scopeKind === "mg" ? "mg_flat" : scopeKind })
      .then((r) => {
        if (cancelled) return;
        // In MG mode the flat list contains every management group (depth-ordered for
        // indentation); in subscription mode keep only subscription nodes.
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
      { connection_id: connectionId, scope_kind: scopeKind, scope_id: scopeId, scope_name: scopeName, strategy, mode, tag_key: tagKey },
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
    const decisions: { action: string; name?: string; from?: string; to?: string }[] = [];
    const chosen: WorkloadCandidate[] = [];
    candidates.forEach((c, i) => {
      if (!selected.has(i)) {
        decisions.push({ action: "reject", name: c.name });
        return;
      }
      const e = edits[i] || {};
      const finalName = (e.name ?? c.name).trim() || c.name;
      if (finalName !== c.name) decisions.push({ action: "rename", from: c.name, to: finalName });
      chosen.push({ ...c, name: finalName, criticality: e.criticality ?? c.criticality });
    });
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
        decisions,
        auto_assess: autoAssess,
        auto_architecture: autoArchitecture,
      });
      onSaved();
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  // Free-text filter — matches name, description, reasoning, workload type/environment, resource
  // groups and the resource-type labels. Keeps each candidate's ORIGINAL index so selection,
  // edits and save (which key off the index) stay correct while filtered.
  const visibleCandidates = useMemo(() => {
    const q = search.trim().toLowerCase();
    const indexed = candidates.map((c, i) => ({ c, i }));
    if (!q) return indexed;
    return indexed.filter(({ c }) => {
      const hay = [
        c.name, c.description, c.reasoning, c.workload_type, c.environment, c.criticality,
        ...(c.resource_groups || []),
        ...(c.types || []).map((t) => t.label),
        ...(c.evidence || []).map((e) => e.detail),
      ].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [candidates, search]);

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
                      className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition ${
                        scopeKind === k ? "border-brand bg-brand/5 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"
                      }`}
                    >
                      <AzureIcon kind={k === "mg" ? "mg" : "subscription"} className="h-4 w-4" />
                      {k === "subscription" ? "Subscription" : "Management group"}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className={label}>{scopeKind === "mg" ? "Management group" : "Subscription"}</label>
                <div className="relative">
                  <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2">
                    <AzureIcon kind={scopeKind === "mg" ? "mg" : "subscription"} className="h-4 w-4" />
                  </span>
                  <select
                    className={`${input} pl-8`}
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
                      <option key={o.id} value={o.id}>
                        {o.depth ? `${"\u00A0\u00A0".repeat(o.depth)}↳ ${o.name}` : o.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {/* Grouping strategy + delta mode */}
              <div>
                <label className={label}>Grouping strategy</label>
                <div className="flex flex-wrap gap-2">
                  {([
                    ["ai", "✨ AI"], ["resource_group", "Resource group"], ["subscription", "Subscription"], ["tag", "By tag"],
                  ] as const).map(([k, lbl]) => (
                    <button
                      key={k}
                      onClick={() => setStrategy(k)}
                      className={`rounded-lg border px-3 py-1.5 text-sm transition ${strategy === k ? "border-brand bg-brand/5 font-medium text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}
                    >
                      {lbl}
                    </button>
                  ))}
                </div>
                <p className="mt-1 text-[11px] text-gray-400">
                  {strategy === "ai" ? "The AI groups resources into workloads using tags, naming, topology and provenance." : "A deterministic template — fast, predictable, no AI."}
                </p>
                {strategy === "tag" && (
                  <input
                    value={tagKey}
                    onChange={(e) => setTagKey(e.target.value)}
                    placeholder="Tag key (e.g. app, cost-center) — leave blank to auto-pick"
                    className={`${input} mt-2`}
                  />
                )}
              </div>
              <label className="flex items-start gap-2 text-sm text-gray-700">
                <input type="checkbox" checked={mode === "delta"} onChange={(e) => setMode(e.target.checked ? "delta" : "full")} className="mt-0.5" />
                <span>Delta mode — only propose resources <b>not already</b> in a saved workload (incremental reconciliation).</span>
              </label>
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
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-medium text-gray-700">
                      {search.trim()
                        ? `${visibleCandidates.length} of ${candidates.length} candidate workload${candidates.length === 1 ? "" : "s"}`
                        : `${candidates.length} candidate workload${candidates.length === 1 ? "" : "s"}`}
                    </span>
                    <div className="flex items-center gap-2">
                      <div className="relative">
                        <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-xs text-gray-400">⌕</span>
                        <input
                          value={search}
                          onChange={(e) => setSearch(e.target.value)}
                          placeholder="Search name, type, resource group…"
                          className="w-56 rounded-md border py-1 pl-6 pr-6 text-xs"
                        />
                        {search && (
                          <button onClick={() => setSearch("")} title="Clear" className="absolute right-1.5 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-600">✕</button>
                        )}
                      </div>
                      <div className="flex gap-2 text-xs">
                        <button onClick={() => setSelected(new Set(visibleCandidates.map(({ i }) => i)))} className="text-brand hover:underline" title="Select all shown">All</button>
                        <button onClick={() => setSelected(new Set(visibleCandidates.filter(({ c }) => (c.confidence ?? 0) >= 0.8).map(({ i }) => i)))} className="text-green-700 hover:underline" title="Select high-confidence candidates">High&nbsp;only</button>
                        <button onClick={() => setSelected(new Set())} className="text-gray-500 hover:underline">None</button>
                      </div>
                    </div>
                  </div>
                  {visibleCandidates.length === 0 && (
                    <p className="rounded-lg border border-dashed bg-gray-50 p-4 text-center text-xs text-gray-400">
                      No candidate workloads match “{search}”.
                    </p>
                  )}
                  {visibleCandidates.map(({ c, i }) => {
                    const ct = confidenceTag(c.confidence);
                    const e = edits[i] || {};
                    const curName = e.name ?? c.name;
                    const curCrit = e.criticality ?? c.criticality ?? "";
                    return (
                      <div key={i} className="flex gap-3 rounded-xl border bg-white p-3 hover:border-brand/40">
                        <input
                          type="checkbox"
                          className="mt-1.5"
                          checked={selected.has(i)}
                          onChange={(ev) => {
                            setSelected((s) => {
                              const n = new Set(s);
                              if (ev.target.checked) n.add(i);
                              else n.delete(i);
                              return n;
                            });
                          }}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <input
                              value={curName}
                              onChange={(ev) => setEdits((m) => ({ ...m, [i]: { ...m[i], name: ev.target.value } }))}
                              title="Rename this workload (the system learns from your correction)"
                              className="min-w-0 flex-1 rounded border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-gray-800 hover:border-gray-200 focus:border-brand focus:outline-none"
                            />
                            <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${ct.cls}`}>{ct.label}</span>
                            <span className="text-[11px] text-gray-400">{c.resource_count} resources</span>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5">
                            <ClassBadges type={c.workload_type} environment={c.environment} />
                            <span className="text-[10px] text-gray-400">criticality</span>
                            <select
                              value={curCrit}
                              onChange={(ev) => setEdits((m) => ({ ...m, [i]: { ...m[i], criticality: ev.target.value } }))}
                              className="rounded border px-1 py-0.5 text-[10px] text-gray-600"
                            >
                              {CRIT_OPTIONS.map((o) => (
                                <option key={o} value={o}>{o || "—"}</option>
                              ))}
                            </select>
                          </div>
                          {c.description && <p className="mt-1 text-xs text-gray-500">{c.description}</p>}
                          <div className="mt-1.5"><TypeChips types={c.types} /></div>
                          {c.evidence && c.evidence.length > 0 && (
                            <div className="mt-1.5 flex flex-wrap gap-1">
                              {c.evidence.map((ev2, j) => (
                                <span key={j} className="rounded-md bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700" title={ev2.kind}>
                                  🔗 {ev2.detail}
                                </span>
                              ))}
                            </div>
                          )}
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
                      </div>
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
                <div className="space-y-1.5">
                  {typeof meta.organized_pct === "number" && Number(meta.resource_count) > 0 && (
                    <div>
                      <div className="mb-0.5 flex items-center justify-between text-[11px] text-gray-500">
                        <span>Estate organized into workloads</span>
                        <span className="font-semibold text-gray-700">{String(meta.organized_pct)}%</span>
                      </div>
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
                        <div className="h-full rounded-full bg-brand" style={{ width: `${Number(meta.organized_pct)}%` }} />
                      </div>
                    </div>
                  )}
                  <p className="text-[11px] text-gray-400">
                    Scanned {String(meta.resource_count)} resources
                    {meta.subscriptions ? ` across ${String(meta.subscriptions)} subscription(s)` : ""}.
                    {Number(meta.ungrouped) > 0 ? ` ${String(meta.ungrouped)} resource(s) didn't fit a workload.` : ""}
                    {meta.truncated ? " (5,000-resource limit reached — results may be partial.)" : ""}
                  </p>
                  {candidates.length > 0 && (
                    <div className="flex flex-wrap gap-3 rounded-lg border bg-gray-50 px-3 py-2 text-xs text-gray-600">
                      <span className="font-medium text-gray-700">On save:</span>
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={autoAssess} onChange={(e) => setAutoAssess(e.target.checked)} />
                        🚀 Run a Mission Control sweep
                      </label>
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={autoArchitecture} onChange={(e) => setAutoArchitecture(e.target.checked)} />
                        🗺️ Generate architecture diagram
                      </label>
                    </div>
                  )}
                </div>
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
