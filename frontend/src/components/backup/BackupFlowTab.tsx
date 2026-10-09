/**
 * Backup Manager — protection flow.
 *
 * One Sankey answering "what flows into what": workload, resource type, policy, vault, and
 * how each item is actually ending up. Unprotected resources are drawn as their own red
 * branch rather than being left out, so coverage and its absence are visible together.
 *
 * Weighting toggles between item count and real money, which changes the question the same
 * picture answers: "where is my estate" versus "where is my backup spend".
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type BackupManagerScope, type BackupSnapshot } from "../../api";
import { SankeyExplorer, type FlowNode } from "../shared/SankeyExplorer";
import {
  DEFAULT_CHAIN, DIMENSION_LABELS, FLOW_PRESETS, NODE_COLORS, OUTCOME_LABELS,
  buildBackupFlowGraph, sanitizePerspective, type FlowDimension, type FlowPerspective,
  type FlowWeight, type Outcome, type WorkloadCatalogEntry,
} from "./backupFlow";

const ALL_DIMENSIONS = Object.keys(DIMENSION_LABELS) as FlowDimension[];
const ICON_KINDS = new Set(["resource_type", "item"]);
const OUTCOME_ORDER: Outcome[] = ["healthy", "rpo_breached", "failing", "stopped", "orphaned", "unprotected"];
const OUTCOME_TONE: Record<Outcome, string> = {
  healthy: "text-emerald-600",
  rpo_breached: "text-amber-600",
  failing: "text-rose-600",
  stopped: "text-amber-600",
  orphaned: "text-rose-600",
  unprotected: "text-rose-600",
};

function money(value: number, currency: string): string {
  const code = (currency || "USD").toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: code, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${code} ${value.toFixed(2)}`;
  }
}

function Chip({ label, value, tone = "text-gray-900", active, onClick }: {
  label: string; value: number | string; tone?: string; active?: boolean; onClick?: () => void;
}) {
  const body = <>
    <div className={`text-base font-semibold leading-4 tabular-nums ${tone}`}>{value}</div>
    <div className="whitespace-nowrap text-[8px] font-medium uppercase leading-3 tracking-wide text-gray-400">{label}</div>
  </>;
  return onClick
    ? <button type="button" onClick={onClick} aria-pressed={!!active}
      className={`h-8 w-max min-w-16 flex-none rounded-lg border px-2 py-px text-left transition ${active ? "border-blue-400 bg-blue-50 ring-1 ring-blue-200" : "bg-white hover:border-blue-300"}`}
      title={`Show only ${label}`}>{body}</button>
    : <div className="h-8 w-max min-w-16 flex-none rounded-lg border bg-white px-2 py-px" title={label}>{body}</div>;
}

/**
 * Named column layouts, mirroring the Change Explorer perspective bar.
 *
 * Stored per browser, like every other saved view in the app. Presets come first and cannot be
 * deleted, so the feature does something useful before anyone has saved anything.
 */
const PERSPECTIVE_KEY = "azsup.backupManager.flowPerspectives.v1";

function loadPerspectives(): { name: string; p: FlowPerspective }[] {
  try {
    const raw = JSON.parse(localStorage.getItem(PERSPECTIVE_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((entry) => entry && typeof entry.name === "string")
      .map((entry) => ({ name: String(entry.name), p: sanitizePerspective(entry.p) }));
  } catch {
    return [];
  }
}

function FlowPerspectiveBar({ current, onApply }: {
  current: FlowPerspective; onApply: (perspective: FlowPerspective) => void;
}) {
  const [saved, setSaved] = useState<{ name: string; p: FlowPerspective }[]>(() => loadPerspectives());

  function persist(list: { name: string; p: FlowPerspective }[]) {
    setSaved(list);
    try { localStorage.setItem(PERSPECTIVE_KEY, JSON.stringify(list)); } catch { /* ignore */ }
  }
  function save() {
    const suggested = current.chain.map((dimension) => DIMENSION_LABELS[dimension]).join(" ▸ ");
    const name = window.prompt("Name this perspective (columns + weighting + filters):", suggested);
    if (!name?.trim()) return;
    persist([...saved.filter((entry) => entry.name !== name.trim()), { name: name.trim(), p: current }]);
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      <button type="button" onClick={save} title="Save the current columns, weighting and filters as a named perspective"
        className="rounded-sm border px-1.5 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50">💾 Save view</button>
      {FLOW_PRESETS.map((preset) => (
        <button key={preset.name} type="button" onClick={() => onApply(preset.perspective)}
          title={preset.perspective.chain.map((dimension) => DIMENSION_LABELS[dimension]).join(" ▸ ")}
          className="rounded-full border border-dashed bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:border-brand hover:text-brand">
          {preset.name}
        </button>
      ))}
      {saved.map((entry) => (
        <span key={entry.name} className="inline-flex items-center gap-0.5 rounded-full border bg-white px-1.5 py-0.5 text-[11px]">
          <button type="button" onClick={() => onApply(entry.p)} className="text-gray-700 hover:text-brand">⭐ {entry.name}</button>
          <button type="button" aria-label={`Delete perspective ${entry.name}`}
            onClick={() => persist(saved.filter((other) => other.name !== entry.name))}
            className="text-gray-300 hover:text-red-600">✕</button>
        </span>
      ))}
    </div>
  );
}

export function BackupFlowTab({ snapshot, scope, onGoTab, onFocusGap, onFocusVault }: {
  snapshot: BackupSnapshot;
  scope: BackupManagerScope;
  onGoTab: (tab: string) => void;
  onFocusGap: (gapIds: string[]) => void;
  onFocusVault: (vaultId: string) => void;
}) {
  const [chain, setChain] = useState<FlowDimension[]>(DEFAULT_CHAIN);
  const [weight, setWeight] = useState<FlowWeight>("count");
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [selected, setSelected] = useState<(FlowNode & { value: number }) | null>(null);

  // Workload membership is client-side: the catalog knows which resources belong to which
  // workload, and the snapshot knows what is protected. Neither alone can answer it.
  const workloadsQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, staleTime: 5 * 60_000 });
  const catalog = useMemo<WorkloadCatalogEntry[]>(
    () => (workloadsQ.data?.workloads ?? []).map((workload) => ({
      id: workload.id, name: workload.name, resourceIds: (workload.nodes ?? []).map((node) => node.id),
    })),
    [workloadsQ.data],
  );

  const graph = useMemo(
    () => buildBackupFlowGraph(snapshot, { chain, weight, catalog, filters: { outcomes } }),
    [snapshot, chain, weight, catalog, outcomes],
  );

  const currency = snapshot.cost?.currency || "USD";
  const toggleOutcome = (outcome: Outcome) =>
    setOutcomes((current) => current.includes(outcome) ? current.filter((v) => v !== outcome) : [...current, outcome]);
  const setColumn = (index: number, dimension: FlowDimension | "") => {
    setChain((current) => {
      if (dimension === "") return current.filter((_value, position) => position !== index);
      // Picking a dimension that is already in the chain swaps the two columns. Disabling it
      // instead would make reordering impossible: moving Vault to the front would first
      // require removing it from the back, and duplicate columns would draw a self-loop.
      const existing = current.indexOf(dimension);
      return current.map((value, position) => {
        if (position === index) return dimension;
        if (position === existing) return current[index];
        return value;
      });
    });
  };
  const applyPerspective = (perspective: FlowPerspective) => {
    setChain(perspective.chain);
    setWeight(perspective.weight);
    setOutcomes(perspective.outcomes);
    setSelected(null);
  };

  const selectedOutcome = selected?.meta?.outcome as Outcome | undefined;
  const selectedVaultId = selected?.meta?.vault_id as string | undefined;

  return <div className="space-y-3">
    <SankeyExplorer
      nodes={graph.nodes}
      links={graph.links}
      title="Protection flow"
      subtitle="How every resource reaches a policy, a vault, and an outcome — including the ones that reach nothing. Click any bar or ribbon to trace its complete paths."
      colors={NODE_COLORS}
      iconKinds={ICON_KINDS}
      // Outcome names are long ("Stopped (data retained)"), and node labels now carry their
      // value too, so reserve room rather than drawing them inward over the previous column.
      marginRight={230}
      storageKey="azsup.backupManager.sankeyZoom"
      searchPlaceholder="Search workload, type, policy, vault…"
      emptyMessage="Nothing matches the selected filters."
      onClearFilters={() => { setOutcomes([]); setChain(DEFAULT_CHAIN); }}
      onSelectNode={setSelected}
      // Ribbon thickness is a comparison, not a reading. With money in play the operator needs
      // the actual figure beside each bar, not only on hover.
      showNodeValues
      formatValue={(value) => weight === "cost"
        ? `${money(value, currency)} / month`
        : `${Math.round(value)} item${Math.round(value) === 1 ? "" : "s"}`}
      // Compact form for the on-chart labels: the header already says these are monthly, and
      // repeating "/ month" beside every bar pushes long vault names off the canvas.
      formatNodeValue={(value) => weight === "cost" ? money(value, currency) : `${Math.round(value)}`}
      actions={
        <div role="group" aria-label="Flow weight" className="flex items-center overflow-hidden rounded-sm border bg-white text-xs">
          <button type="button" aria-pressed={weight === "count"} onClick={() => setWeight("count")}
            className={`h-7 px-2.5 font-medium ${weight === "count" ? "bg-gray-900 text-white" : "hover:bg-gray-50"}`}>Items</button>
          <button type="button" aria-pressed={weight === "cost"} onClick={() => setWeight("cost")}
            title={snapshot.cost?.actuals?.available ? "Weight ribbons by real monthly spend" : "Weight by list-price estimate — no billing data in this analysis"}
            className={`h-7 border-l px-2.5 font-medium ${weight === "cost" ? "bg-gray-900 text-white" : "hover:bg-gray-50"}`}>Cost</button>
        </div>
      }
      filterBar={<>
        <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50/60 px-4 py-2">
          <span className="text-[11px] font-medium uppercase tracking-wide text-gray-500">Columns</span>
          {chain.map((dimension, index) => (
            <select key={`${dimension}-${index}`} value={dimension} aria-label={`Flow column ${index + 1}`}
              onChange={(event) => setColumn(index, event.target.value as FlowDimension | "")}
              className="rounded-sm border bg-white px-2 py-1 text-xs">
              {ALL_DIMENSIONS.map((value) => (
                <option key={value} value={value}>{DIMENSION_LABELS[value]}</option>
              ))}
              {chain.length > 2 && <option value="">— remove —</option>}
            </select>
          ))}
          {chain.length < ALL_DIMENSIONS.length && (
            <select value="" aria-label="Add a flow column"
              onChange={(event) => event.target.value && setChain((current) => [...current, event.target.value as FlowDimension])}
              className="rounded-sm border bg-white px-2 py-1 text-xs text-gray-500">
              <option value="">+ add column</option>
              {ALL_DIMENSIONS.filter((value) => !chain.includes(value)).map((value) => (
                <option key={value} value={value}>{DIMENSION_LABELS[value]}</option>
              ))}
            </select>
          )}
          <span className="ml-auto text-[10px] text-gray-500">
            {graph.totals.rows} flow(s) · {graph.totals.count} item(s) · {money(graph.totals.cost, currency)} / month
            {weight === "cost" && !snapshot.cost?.actuals?.available && " · list-price estimate"}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-b bg-gray-50/30 px-4 py-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-gray-500">Views</span>
          <FlowPerspectiveBar current={{ chain, weight, outcomes }} onApply={applyPerspective} />
        </div>
        <div className="flex flex-wrap items-center gap-1 border-b px-4 py-2">
          {OUTCOME_ORDER.map((outcome) => (
            <Chip key={outcome} label={OUTCOME_LABELS[outcome]} value={graph.byOutcome[outcome]}
              tone={OUTCOME_TONE[outcome]} active={outcomes.includes(outcome)} onClick={() => toggleOutcome(outcome)} />
          ))}
          {outcomes.length > 0 && (
            <button type="button" onClick={() => setOutcomes([])} className="ml-2 rounded-sm border bg-white px-2 py-1 text-xs text-blue-700">Show all</button>
          )}
        </div>
      </>}
    />

    {selected && (
      <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-white px-4 py-3 text-sm">
        <div className="mr-auto min-w-0">
          <div className="font-medium text-gray-900">{selected.name}</div>
          <div className="text-xs text-gray-500">
            {DIMENSION_LABELS[selected.meta?.dimension as FlowDimension] ?? selected.kind}
            {" · "}
            {weight === "cost"
              ? `${money(selected.value, currency)} / month`
              : `${Math.round(selected.value)} item${Math.round(selected.value) === 1 ? "" : "s"}`}
          </div>
        </div>
        {selectedOutcome === "unprotected" && (
          <button onClick={() => { onFocusGap(snapshot.gaps?.gaps?.map((gap) => gap.gap_id) ?? []); onGoTab("gaps"); }}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white">Protect these resources →</button>
        )}
        {selectedVaultId && selectedVaultId !== "none" && (
          <button onClick={() => { onFocusVault(selectedVaultId); onGoTab("vaults"); }}
            className="rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">Open vault posture →</button>
        )}
        {selected.meta?.dimension === "policy" && (
          <button onClick={() => onGoTab("policies")}
            className="rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">Model this policy →</button>
        )}
        {(selectedOutcome === "orphaned" || selectedOutcome === "failing" || selectedOutcome === "rpo_breached") && (
          <button onClick={() => onGoTab(selectedOutcome === "orphaned" ? "cost" : "jobs")}
            className="rounded-lg border px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">
            {selectedOutcome === "orphaned" ? "Review waste →" : "Triage jobs →"}
          </button>
        )}
        <button onClick={() => setSelected(null)} className="text-xs text-gray-400 hover:text-gray-700">Clear</button>
      </div>
    )}

    {!scope.workload_id && (
      <p className="px-1 text-[11px] text-gray-500">
        Workload membership comes from the workload catalog. Resources in more than one workload are grouped as
        “Shared across workloads”, and resources in none as “Unmapped”, rather than being dropped.
      </p>
    )}
  </div>;
}
