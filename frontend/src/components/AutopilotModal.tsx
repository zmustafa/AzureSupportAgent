import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  streamAutopilot,
  streamSurvey,
  type CostEstimate,
  type DiscoveryProfile,
  type FacetCount,
  type FilterPreview,
  type SculptConfig,
  type SurveyResult,
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

type Stage = "setup" | "survey" | "running" | "review";

// ---- Scope Sculptor presets: each maps to a coherent set of controls. ----
type Granularity = "resource" | "resource_group" | "sample";
type PresetId = "fast" | "balanced" | "thorough" | "custom";
const PRESET_DEFAULTS: Record<Exclude<PresetId, "custom">, { granularity: Granularity; excludeNoise: boolean; excludeSystemRgs: boolean; confidenceFloor: number }> = {
  fast: { granularity: "resource_group", excludeNoise: true, excludeSystemRgs: true, confidenceFloor: 0.55 },
  balanced: { granularity: "resource", excludeNoise: true, excludeSystemRgs: true, confidenceFloor: 0 },
  thorough: { granularity: "resource", excludeNoise: false, excludeSystemRgs: false, confidenceFloor: 0 },
};
const PRESET_BLURB: Record<Exclude<PresetId, "custom">, string> = {
  fast: "Coarse resource-group pass on a de-noised estate — fewest AI calls, quickest.",
  balanced: "Per-resource AI grouping with noise filtered out — the best accuracy/speed trade-off.",
  thorough: "Every resource, nothing filtered — most exhaustive, most AI calls.",
};

function fmtSeconds(s: number): string {
  if (s < 60) return `~${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return r ? `~${m}m ${r}s` : `~${m}m`;
}

function fmtTokens(t: number): string {
  if (t >= 1000) return `~${(t / 1000).toFixed(t >= 10000 ? 0 : 1)}k tokens`;
  return `~${t} tokens`;
}

// A horizontal bar of top facet counts (type / RG / region…) with relative-width bars.
function FacetBars({ title, items, max = 6, onToggle, selected }: {
  title: string;
  items: FacetCount[];
  max?: number;
  onToggle?: (label: string) => void;
  selected?: Set<string>;
}) {
  const shown = items.slice(0, max);
  const top = shown.length ? Math.max(...shown.map((i) => i.count)) : 1;
  return (
    <div>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">{title}</div>
      <div className="space-y-1">
        {shown.map((it) => {
          const isSel = selected?.has(it.label);
          return (
            <button
              key={it.label}
              onClick={onToggle ? () => onToggle(it.label) : undefined}
              disabled={!onToggle}
              className={`group flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] ${onToggle ? "cursor-pointer hover:bg-gray-50" : "cursor-default"} ${isSel ? "ring-1 ring-brand/40" : ""}`}
            >
              <span className="w-32 shrink-0 truncate text-gray-600" title={it.label}>{it.label}</span>
              <span className="relative h-2 flex-1 overflow-hidden rounded-full bg-gray-100">
                <span className={`absolute inset-y-0 left-0 rounded-full ${isSel ? "bg-brand" : "bg-brand/40"}`} style={{ width: `${Math.max(4, (it.count / top) * 100)}%` }} />
              </span>
              <span className="w-10 shrink-0 text-right font-semibold text-gray-700">{it.count}</span>
            </button>
          );
        })}
        {items.length > max && <div className="text-[10px] text-gray-400">+{items.length - max} more</div>}
      </div>
    </div>
  );
}

// A multi-select chip row driven by facet counts.
function ChipMultiSelect({ items, selected, onToggle, max = 12 }: {
  items: FacetCount[];
  selected: Set<string>;
  onToggle: (label: string) => void;
  max?: number;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {items.slice(0, max).map((it) => {
        const on = selected.has(it.label);
        return (
          <button
            key={it.label}
            onClick={() => onToggle(it.label)}
            className={`rounded-full border px-2 py-0.5 text-[11px] transition ${on ? "border-brand bg-brand/10 text-brand" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}
          >
            {it.label} <span className="opacity-60">{it.count}</span>
          </button>
        );
      })}
    </div>
  );
}

export function AutopilotModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  // Always refetch the connection list when the modal opens — `azureConnections` carries a 5-min
  // staleTime default (it's requested by many screens), which otherwise serves a stale/empty list
  // right after the user adds their first connection.
  const connQ = useQuery({ queryKey: ["azureConnections"], queryFn: api.azureConnections, refetchOnMount: "always" });
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

  // ---- Scope Sculptor state ----
  const [preset, setPreset] = useState<PresetId>("balanced");
  const [granularity, setGranularity] = useState<Granularity>("resource");
  const [excludeNoise, setExcludeNoise] = useState(true);
  const [excludeSystemRgs, setExcludeSystemRgs] = useState(true);
  const [rgGlobs, setRgGlobs] = useState("");                       // newline/comma globs
  const [tagSeedKeys, setTagSeedKeys] = useState<Set<string>>(new Set());
  const [excludeTypes, setExcludeTypes] = useState<Set<string>>(new Set());  // friendly labels
  const [environments, setEnvironments] = useState<Set<string>>(new Set());
  const [regions, setRegions] = useState<Set<string>>(new Set());
  const [subsFilter, setSubsFilter] = useState<Set<string>>(new Set());
  const [nameContains, setNameContains] = useState("");
  const [confidenceFloor, setConfidenceFloor] = useState(0);       // 0..1
  const [maxAiCalls, setMaxAiCalls] = useState(0);                 // 0 = unbounded
  const [useNaming, setUseNaming] = useState(true);

  const [survey, setSurvey] = useState<SurveyResult | null>(null);
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [filterPreview, setFilterPreview] = useState<FilterPreview | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Saved discovery profiles for this connection.
  const profilesQ = useQuery({
    queryKey: ["autopilotProfiles", connectionId],
    queryFn: () => api.autopilotProfiles(connectionId),
    enabled: !!connectionId,
  });
  const profiles = profilesQ.data?.profiles ?? [];

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

  // Map the user's friendly type-label exclusions back to ARM-type substrings is not needed —
  // the backend matches friendly labels poorly, so we keep type filtering to the survey facets
  // (which are friendly labels) by passing them through as-is; the backend exclude_types does a
  // substring match on the lowercased ARM type, and friendly labels rarely collide. For the
  // common case (excluding a noisy *category*) the noise toggle already covers it, so type
  // exclusion here is a best-effort convenience.
  const sculptConfig = useMemo<SculptConfig>(() => {
    const globs = rgGlobs.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    return {
      strategy,
      mode,
      tag_key: tagKey,
      preset: preset === "custom" ? "" : preset,
      granularity,
      exclude_noise: excludeNoise,
      exclude_system_rgs: excludeSystemRgs,
      rg_globs: globs,
      tag_seed_keys: [...tagSeedKeys],
      exclude_types: [...excludeTypes],
      environments: [...environments],
      regions: [...regions],
      subscriptions: [...subsFilter],
      name_contains: nameContains,
      confidence_floor: confidenceFloor,
      max_ai_calls: maxAiCalls,
      naming_hint: useNaming && survey?.facets.naming.pattern ? survey.facets.naming.pattern : "",
    };
  }, [strategy, mode, tagKey, preset, granularity, excludeNoise, excludeSystemRgs, rgGlobs, tagSeedKeys, excludeTypes, environments, regions, subsFilter, nameContains, confidenceFloor, maxAiCalls, useNaming, survey]);

  // Apply a preset's controls (the user can still override any of them afterwards).
  function applyPreset(p: PresetId) {
    setPreset(p);
    if (p === "custom") return;
    const d = PRESET_DEFAULTS[p];
    setGranularity(d.granularity);
    setExcludeNoise(d.excludeNoise);
    setExcludeSystemRgs(d.excludeSystemRgs);
    setConfidenceFloor(d.confidenceFloor);
  }

  // Load a saved profile into the controls.
  function loadProfile(p: DiscoveryProfile) {
    const c = p.config;
    setPreset((c.preset as PresetId) || "custom");
    if (c.granularity) setGranularity(c.granularity as Granularity);
    if (c.strategy) setStrategy(c.strategy as typeof strategy);
    if (c.mode) setMode(c.mode as typeof mode);
    if (typeof c.exclude_noise === "boolean") setExcludeNoise(c.exclude_noise);
    if (typeof c.exclude_system_rgs === "boolean") setExcludeSystemRgs(c.exclude_system_rgs);
    setRgGlobs((c.rg_globs ?? []).join("\n"));
    setTagSeedKeys(new Set(c.tag_seed_keys ?? []));
    setExcludeTypes(new Set(c.exclude_types ?? []));
    setEnvironments(new Set(c.environments ?? []));
    setRegions(new Set(c.regions ?? []));
    setSubsFilter(new Set(c.subscriptions ?? []));
    setNameContains(c.name_contains ?? "");
    setConfidenceFloor(c.confidence_floor ?? 0);
    setMaxAiCalls(c.max_ai_calls ?? 0);
  }

  // Live cost re-estimate against the cached survey whenever the controls change (debounced).
  useEffect(() => {
    if (stage !== "survey" || !connectionId || !scopeId) return;
    let cancelled = false;
    const handle = setTimeout(() => {
      api
        .autopilotEstimate({ connection_id: connectionId, scope_kind: scopeKind, scope_id: scopeId, config: sculptConfig })
        .then((r) => {
          if (cancelled) return;
          if ("needs_survey" in r) return; // survey expired; user can re-survey
          setEstimate(r.estimate);
          setFilterPreview(r.filter_preview);
        })
        .catch(() => {/* keep last estimate */});
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [sculptConfig, stage, connectionId, scopeId, scopeKind]);

  function toggleIn(setter: React.Dispatch<React.SetStateAction<Set<string>>>, label: string) {
    setter((s) => {
      const n = new Set(s);
      if (n.has(label)) n.delete(label);
      else n.add(label);
      return n;
    });
  }

  // Pre-flight survey: enumerate + facet the estate (no AI), then show the sculpt stage.
  function runSurvey() {
    if (!connectionId || !scopeId) {
      setError("Pick a connection and a scope.");
      return;
    }
    setError("");
    setStage("survey");
    setSurvey(null);
    setEstimate(null);
    setFilterPreview(null);
    setLog([]);
    const controller = new AbortController();
    abortRef.current = controller;
    void streamSurvey(
      { connection_id: connectionId, scope_kind: scopeKind, scope_id: scopeId, scope_name: scopeName },
      {
        onStatus: (d) => setLog((l) => [...l, { phase: d.phase, message: d.message }]),
        onSurvey: (d) => {
          setSurvey(d);
          setEstimate(d.estimate);
          setFilterPreview(d.filter_preview);
        },
        onError: (m) => {
          setError(m);
          setStage("setup");
        },
      },
      controller.signal,
    );
  }

  async function saveProfile() {
    const name = window.prompt("Name this discovery profile:", scopeName ? `${scopeName} — ${preset}` : preset);
    if (!name) return;
    try {
      await api.saveAutopilotProfile({
        connection_id: connectionId,
        name,
        config: sculptConfig,
        scope_kind: scopeKind,
        scope_id: scopeId,
        scope_name: scopeName,
      });
      await profilesQ.refetch();
    } catch (e) {
      setError(formatError(e));
    }
  }

  async function removeProfile(id: string) {
    try {
      await api.deleteAutopilotProfile(id, connectionId);
      await profilesQ.refetch();
    } catch (e) {
      setError(formatError(e));
    }
  }

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
      { connection_id: connectionId, scope_kind: scopeKind, scope_id: scopeId, scope_name: scopeName, ...sculptConfig },
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

  // "Stop early" during a streaming run: keep the candidates discovered so far and jump to
  // review without aborting what's already in hand (Tier 4 priority streaming pays off here).
  function stopEarly() {
    abortRef.current?.abort();
    setStage("review");
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

              {/* Saved discovery profiles */}
              {profiles.length > 0 && (
                <div>
                  <label className={label}>Saved profiles</label>
                  <div className="flex flex-wrap gap-1.5">
                    {profiles.map((p) => (
                      <span key={p.id} className="group inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-xs text-gray-700 hover:border-brand/40">
                        <button onClick={() => loadProfile(p)} title={`Load “${p.name}”`} className="hover:text-brand">
                          💾 {p.name}
                        </button>
                        <button onClick={() => void removeProfile(p.id)} title="Delete profile" className="text-gray-300 hover:text-red-500">✕</button>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Speed preset */}
              <div>
                <label className={label}>Preset</label>
                <div className="grid grid-cols-3 gap-2">
                  {(["fast", "balanced", "thorough"] as const).map((p) => (
                    <button
                      key={p}
                      onClick={() => applyPreset(p)}
                      className={`rounded-lg border px-2 py-1.5 text-left text-xs transition ${preset === p ? "border-brand bg-brand/5" : "border-gray-200 hover:bg-gray-50"}`}
                    >
                      <div className={`font-semibold capitalize ${preset === p ? "text-brand" : "text-gray-700"}`}>
                        {p === "fast" ? "⚡ Fast" : p === "balanced" ? "⚖️ Balanced" : "🔬 Thorough"}
                      </div>
                      <div className="mt-0.5 text-[10px] leading-tight text-gray-400">{PRESET_BLURB[p]}</div>
                    </button>
                  ))}
                </div>
                {preset === "custom" && <p className="mt-1 text-[11px] text-amber-600">Custom — you've overridden a preset's controls.</p>}
              </div>


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

          {/* Survey / Sculpt — the pre-flight where the user shapes the input before any AI. */}
          {stage === "survey" && (
            <div className="space-y-4">
              {!survey ? (
                <div className="rounded-lg border bg-gray-50 p-4">
                  <div className="mb-2 flex items-center gap-2 text-xs font-medium text-gray-600">
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                    Surveying the estate (read-only, no AI)…
                  </div>
                  <div className="max-h-32 space-y-0.5 overflow-y-auto font-mono text-[11px] text-gray-500">
                    {log.map((l, i) => (
                      <div key={i}><span className="text-gray-400">›</span> {l.message}</div>
                    ))}
                  </div>
                </div>
              ) : (
                <>
                  {/* Cost / time estimate banner */}
                  <div className="rounded-xl border border-brand/30 bg-brand/5 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-sm font-semibold text-gray-800">
                        {estimate && estimate.ai_calls === 0
                          ? "No AI calls needed — this configuration groups deterministically."
                          : `${estimate?.ai_calls ?? "—"} AI call${estimate?.ai_calls === 1 ? "" : "s"} · ${estimate ? fmtSeconds(estimate.est_seconds) : "—"}`}
                      </div>
                      {estimate?.capped && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-700">budget-capped</span>}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-gray-600">
                      <span><b>{filterPreview?.kept ?? survey.facets.total}</b> of {survey.meta.resource_count} resources to group</span>
                      {filterPreview && filterPreview.removed > 0 && <span>{filterPreview.removed} filtered out</span>}
                      {estimate && estimate.tag_seeded > 0 && <span>{estimate.tag_seeded} tag-seeded (free)</span>}
                      {estimate && estimate.ai_calls > 0 && <span>{fmtTokens(estimate.est_tokens)}</span>}
                      {survey.meta.truncated && <span className="text-amber-600">5,000-resource cap reached</span>}
                    </div>
                    {/* Estate-reduction bar: kept vs filtered. */}
                    {filterPreview && (
                      <div className="mt-2 flex h-2 w-full overflow-hidden rounded-full bg-gray-200">
                        <div className="h-full bg-brand" style={{ width: `${Math.round(100 * (filterPreview.kept / Math.max(1, survey.meta.resource_count)))}%` }} title={`${filterPreview.kept} kept`} />
                      </div>
                    )}
                  </div>

                  {/* Live estate facets */}
                  <div className="grid grid-cols-2 gap-4">
                    <FacetBars title="Resource types" items={survey.facets.types} onToggle={(l) => { toggleIn(setExcludeTypes, l); setPreset("custom"); }} selected={excludeTypes} />
                    <FacetBars title="Top resource groups" items={survey.facets.resource_groups} />
                  </div>
                  <p className="-mt-2 text-[10px] text-gray-400">Click a type to exclude it from grouping.</p>

                  {/* Quick scoping chips from facets */}
                  <div className="space-y-2">
                    <div>
                      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Environment</div>
                      <ChipMultiSelect items={survey.facets.environments} selected={environments} onToggle={(l) => { toggleIn(setEnvironments, l); setPreset("custom"); }} />
                    </div>
                    <div>
                      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Region</div>
                      <ChipMultiSelect items={survey.facets.regions} selected={regions} onToggle={(l) => { toggleIn(setRegions, l); setPreset("custom"); }} />
                    </div>
                    {survey.facets.subscriptions.length > 1 && (
                      <div>
                        <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Subscription</div>
                        <ChipMultiSelect items={survey.facets.subscriptions} selected={subsFilter} onToggle={(l) => { toggleIn(setSubsFilter, l); setPreset("custom"); }} max={8} />
                      </div>
                    )}
                    <p className="text-[10px] text-gray-400">Empty = include all. Selecting any value scopes discovery to just those.</p>
                  </div>

                  {/* Tag-seed: deterministically pre-bucket by authoritative tags. */}
                  {survey.facets.tag_keys.length > 0 && (
                    <div>
                      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">Tag-seed grouping (pre-bucket by tag — only the remainder needs AI)</div>
                      <ChipMultiSelect items={survey.facets.tag_keys} selected={tagSeedKeys} onToggle={(l) => { toggleIn(setTagSeedKeys, l); setPreset("custom"); }} />
                    </div>
                  )}

                  {/* Naming convention */}
                  {survey.facets.naming.pattern && (
                    <label className="flex items-start gap-2 rounded-lg border bg-gray-50 px-3 py-2 text-xs text-gray-700">
                      <input type="checkbox" checked={useNaming} onChange={(e) => setUseNaming(e.target.checked)} className="mt-0.5" />
                      <span>
                        Use the detected naming convention <code className="rounded bg-white px-1 text-[11px]">{survey.facets.naming.pattern}</code>{" "}
                        <span className="text-gray-400">({Math.round(survey.facets.naming.confidence * 100)}% of names · e.g. {survey.facets.naming.examples.slice(0, 2).join(", ")})</span> as a grouping signal.
                      </span>
                    </label>
                  )}

                  {/* Granularity */}
                  <div>
                    <label className={label}>Grouping granularity</label>
                    <div className="grid grid-cols-3 gap-2">
                      {([
                        ["resource", "Resource", "Most precise · most AI"],
                        ["resource_group", "Resource group", "Coarser · far fewer calls"],
                        ["sample", "Name cluster", "Fewest calls · large estates"],
                      ] as const).map(([k, lbl, hint]) => (
                        <button
                          key={k}
                          onClick={() => { setGranularity(k); setPreset("custom"); }}
                          className={`rounded-lg border px-2 py-1.5 text-left text-xs transition ${granularity === k ? "border-brand bg-brand/5" : "border-gray-200 hover:bg-gray-50"}`}
                        >
                          <div className={`font-semibold ${granularity === k ? "text-brand" : "text-gray-700"}`}>{lbl}</div>
                          <div className="mt-0.5 text-[10px] leading-tight text-gray-400">{hint}</div>
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Advanced controls */}
                  <div>
                    <button onClick={() => setShowAdvanced((v) => !v)} className="text-xs font-medium text-brand hover:underline">
                      {showAdvanced ? "▾ Hide advanced" : "▸ Advanced controls"}
                    </button>
                    {showAdvanced && (
                      <div className="mt-2 space-y-3 rounded-lg border bg-gray-50 p-3">
                        <div className="grid grid-cols-2 gap-3">
                          <label className="flex items-center gap-2 text-xs text-gray-700">
                            <input type="checkbox" checked={excludeNoise} onChange={(e) => { setExcludeNoise(e.target.checked); setPreset("custom"); }} />
                            Filter noise ({survey.facets.noise_count} children)
                          </label>
                          <label className="flex items-center gap-2 text-xs text-gray-700">
                            <input type="checkbox" checked={excludeSystemRgs} onChange={(e) => { setExcludeSystemRgs(e.target.checked); setPreset("custom"); }} />
                            Skip system RGs ({survey.facets.system_rg_count})
                          </label>
                        </div>
                        <div>
                          <label className={label}>Confidence floor — hide candidates below {Math.round(confidenceFloor * 100)}%</label>
                          <input type="range" min={0} max={0.9} step={0.05} value={confidenceFloor} onChange={(e) => { setConfidenceFloor(Number(e.target.value)); setPreset("custom"); }} className="w-full" />
                        </div>
                        <div>
                          <label className={label}>Max AI calls (budget cap — 0 = unbounded)</label>
                          <input type="number" min={0} value={maxAiCalls} onChange={(e) => { setMaxAiCalls(Math.max(0, Number(e.target.value))); setPreset("custom"); }} className={`${input} w-32`} />
                        </div>
                        <div>
                          <label className={label}>Name contains</label>
                          <input value={nameContains} onChange={(e) => { setNameContains(e.target.value); setPreset("custom"); }} placeholder="Only resources whose name contains…" className={input} />
                        </div>
                        <div>
                          <label className={label}>System resource-group globs (one per line)</label>
                          <textarea value={rgGlobs} onChange={(e) => { setRgGlobs(e.target.value); setPreset("custom"); }} rows={2} placeholder="MC_*&#10;NetworkWatcherRG" className={`${input} font-mono text-[11px]`} />
                        </div>
                      </div>
                    )}
                  </div>
                  {error && <div className="text-xs text-red-600">{error}</div>}
                </>
              )}
            </div>
          )}

          {/* Running / Review */}
          {(stage === "running" || stage === "review") && (
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
                    {Number(meta.filtered) > 0 ? ` ${String(meta.filtered)} filtered out before grouping;` : ""}
                    {Number(meta.considered) > 0 ? ` ${String(meta.considered)} considered.` : ""}
                    {Number(meta.tag_seeded_workloads) > 0 ? ` ${String(meta.tag_seeded_workloads)} tag-seeded.` : ""}
                    {Number(meta.reattached) > 0 ? ` ${String(meta.reattached)} child resource(s) re-attached.` : ""}
                    {Number(meta.below_floor) > 0 ? ` ${String(meta.below_floor)} hidden below the confidence floor.` : ""}
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
              <button onClick={runSurvey} disabled={!connectionId || !scopeId} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
                Survey estate →
              </button>
            </>
          )}
          {stage === "survey" && (
            <>
              <button onClick={() => { abortRef.current?.abort(); setStage("setup"); }} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">← Back</button>
              {survey && (
                <button onClick={() => void saveProfile()} className="rounded-lg border border-brand/40 px-3.5 py-1.5 text-sm text-brand hover:bg-brand/5" title="Save these settings as a reusable profile">
                  💾 Save profile
                </button>
              )}
              <button onClick={start} disabled={!survey} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
                {estimate && estimate.ai_calls === 0 ? "Group now (no AI)" : `Group with AI${estimate ? ` (${fmtSeconds(estimate.est_seconds)})` : ""}`}
              </button>
            </>
          )}
          {stage === "running" && (
            <>
              <button onClick={cancel} className="rounded-lg border px-3.5 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
              {candidates.length > 0 && (
                <button onClick={stopEarly} className="rounded-lg border border-brand/40 px-3.5 py-1.5 text-sm text-brand hover:bg-brand/5" title="Keep the candidates found so far and review now">
                  Stop &amp; review {candidates.length}
                </button>
              )}
            </>
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
